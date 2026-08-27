"""Account linking: the one-shot token and the bindings it is allowed to create.

The link token is a bearer credential that travels through a chat message and lands on a
second device, so it is the softest part of the flow -- it can be screenshotted, quoted,
or forwarded. Everything here pins a property that makes that survivable: it works once,
it dies quickly, and it can never rebind a Telegram identity that already belongs to
somebody else.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.security import hash_link_token
from app.models.user import User
from app.repositories.telegram import (
    TelegramAccountRepository,
    TelegramLinkTokenRepository,
)
from app.services.telegram.linking import (
    LinkResult,
    TelegramIdentity,
    TelegramLinkService,
)


def _service(session: AsyncSession) -> TelegramLinkService:
    return TelegramLinkService(
        TelegramAccountRepository(session), TelegramLinkTokenRepository(session)
    )


def _identity(telegram_id: str = "555000") -> TelegramIdentity:
    return TelegramIdentity(
        telegram_user_id=telegram_id, chat_id=telegram_id, username="ada", first_name="Ada"
    )


def _raw_from(deep_link: str) -> str:
    return deep_link.split("start=", 1)[1]


@pytest.fixture(autouse=True)
def _bot_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "recallai_bot")


async def test_link_round_trip(session: AsyncSession, alice: User) -> None:
    service = _service(session)
    issued = await service.create_link(alice.id)
    await session.commit()

    assert issued.deep_link.startswith("https://t.me/recallai_bot?start=")
    # Telegram caps the /start payload at 64 characters; 43 leaves margin.
    assert len(_raw_from(issued.deep_link)) <= 64

    outcome = await service.consume(_raw_from(issued.deep_link), _identity())
    await session.commit()

    assert outcome.result is LinkResult.linked
    assert outcome.account is not None
    assert outcome.account.user_id == alice.id
    assert outcome.account.telegram_user_id == "555000"


async def test_raw_token_is_never_stored(session: AsyncSession, alice: User) -> None:
    service = _service(session)
    issued = await service.create_link(alice.id)
    await session.commit()

    raw = _raw_from(issued.deep_link)
    tokens = TelegramLinkTokenRepository(session)
    assert await tokens.get_by_hash(raw) is None
    assert await tokens.get_by_hash(hash_link_token(raw)) is not None


async def test_token_works_exactly_once(session: AsyncSession, alice: User) -> None:
    service = _service(session)
    issued = await service.create_link(alice.id)
    await session.commit()
    raw = _raw_from(issued.deep_link)

    assert (await service.consume(raw, _identity())).result is LinkResult.linked
    await session.commit()

    replay = await service.consume(raw, _identity("999111"))
    await session.commit()
    assert replay.result is LinkResult.invalid_token


async def test_expired_token_is_refused(session: AsyncSession, alice: User) -> None:
    service = _service(session)
    issued = await service.create_link(alice.id)
    await session.commit()
    raw = _raw_from(issued.deep_link)

    row = await TelegramLinkTokenRepository(session).get_by_hash(hash_link_token(raw))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(row)
    await session.commit()

    assert (await service.consume(raw, _identity())).result is LinkResult.invalid_token


async def test_minting_a_link_kills_the_previous_one(
    session: AsyncSession, alice: User
) -> None:
    """Pressing Connect twice must not leave a live credential behind."""
    service = _service(session)
    first = await service.create_link(alice.id)
    await session.commit()
    await service.create_link(alice.id)
    await session.commit()

    outcome = await service.consume(_raw_from(first.deep_link), _identity())
    await session.commit()
    assert outcome.result is LinkResult.invalid_token


async def test_telegram_id_cannot_be_stolen_from_another_account(
    session: AsyncSession, alice: User, bob: User
) -> None:
    """The attack this exists to stop: Bob binding Alice's Telegram to his vault.

    Bob generates a perfectly valid link of his own; it is his Telegram identity that is
    not his to claim.
    """
    service = _service(session)
    alice_link = await service.create_link(alice.id)
    await session.commit()
    await service.consume(_raw_from(alice_link.deep_link), _identity("555000"))
    await session.commit()

    bob_link = await service.create_link(bob.id)
    await session.commit()
    outcome = await service.consume(_raw_from(bob_link.deep_link), _identity("555000"))
    await session.commit()

    assert outcome.result is LinkResult.taken_by_other_user
    still_alice = await TelegramAccountRepository(session).get_by_telegram_user_id("555000")
    assert still_alice is not None
    assert still_alice.user_id == alice.id


async def test_a_refused_link_still_spends_its_token(
    session: AsyncSession, alice: User, bob: User
) -> None:
    """A rejected redemption must not leave a token to retry with."""
    service = _service(session)
    alice_link = await service.create_link(alice.id)
    await session.commit()
    await service.consume(_raw_from(alice_link.deep_link), _identity("555000"))
    await session.commit()

    bob_link = await service.create_link(bob.id)
    await session.commit()
    raw = _raw_from(bob_link.deep_link)
    await service.consume(raw, _identity("555000"))
    await session.commit()

    row = await TelegramLinkTokenRepository(session).get_by_hash(hash_link_token(raw))
    assert row is not None and row.used_at is not None


async def test_relinking_replaces_the_users_previous_chat(
    session: AsyncSession, alice: User
) -> None:
    """One binding per user: two live chats would both receive her memories."""
    service = _service(session)
    first = await service.create_link(alice.id)
    await session.commit()
    await service.consume(_raw_from(first.deep_link), _identity("555000"))
    await session.commit()

    second = await service.create_link(alice.id)
    await session.commit()
    await service.consume(_raw_from(second.deep_link), _identity("777222"))
    await session.commit()

    accounts = TelegramAccountRepository(session)
    assert await accounts.get_by_telegram_user_id("555000") is None
    current = await accounts.get_for_user(alice.id)
    assert current is not None and current.telegram_user_id == "777222"


async def test_disconnect_is_scoped_to_the_owner(
    session: AsyncSession, alice: User, bob: User
) -> None:
    service = _service(session)
    issued = await service.create_link(alice.id)
    await session.commit()
    await service.consume(_raw_from(issued.deep_link), _identity())
    await session.commit()

    assert await service.disconnect(bob.id) is False
    assert await service.disconnect(alice.id) is True
    await session.commit()
    assert await service.get_for_user(alice.id) is None
