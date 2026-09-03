"""The messages that arrive *after* the acknowledgement, and the update that arrives twice.

Three claims, and all three are about wiring rather than about wording. Wiring is what
was actually broken: `send_chat_action` sat in the client for months with no caller, and
"the method exists and has a unit test" is precisely what was true the whole time. So
each test below runs the real task body -- the function the Celery task calls -- with the
database, the broker and the Bot API stood in for.

* A capture and its finished card have to carry the **same short id**, because they are
  minutes apart and nothing else ties them together.
* A capture still running after `NUDGE_AFTER_MINUTES` gets **exactly one** message, and
  only if it actually went out.
* A redelivered update runs **once**. Telegram redelivers on any non-2xx and Celery
  redelivers a task whose worker died after the work but before the ack, so the same turn
  can arrive twice through two paths that know nothing about each other.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id
from app.services.telegram.dedupe import claim as real_claim

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")

# Bound here, at import, on purpose. The autouse `_no_shared_redis_state` fixture replaces
# `dedupe.claim` on the module for every test in the suite -- which is what keeps a live
# developer Redis from making the rest of the suite order-dependent, and which would
# otherwise make every test *of* the dedupe pass without running a line of it. This name
# is captured before any fixture runs, so the tests below exercise the real function while
# everything else still gets the stub.


def _item(
    state: ProcessingStatus = ProcessingStatus.processing,
    *,
    title: str | None = "5 games every Cloud Engineer should play",
    age: timedelta = timedelta(minutes=20),
) -> VaultItem:
    return VaultItem(
        id=uuid.uuid4(),
        user_id=_USER,
        type=ContentType.instagram,
        title=title,
        processing_status=state,
        created_at=datetime.now(UTC) - age,
    )


class FakeAccount:
    def __init__(self, chat_id: str = "4242") -> None:
        self.telegram_chat_id = chat_id


class FakeClient:
    """Records what was sent where. Doubles as its own async context manager."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send_message(self, chat_id: str, text: str, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("telegram is unreachable")
        self.sent.append((chat_id, text))


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _stub_session(monkeypatch: pytest.MonkeyPatch, session: FakeSession) -> None:
    from app.queue import tasks

    @asynccontextmanager
    async def _session() -> Any:
        yield session

    monkeypatch.setattr(tasks, "task_session", _session)


# --- the finished card carries the id the acknowledgement showed ----------------------


async def test_the_completion_reply_carries_the_short_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.queue import tasks

    item = _item(ProcessingStatus.completed)
    client = FakeClient()
    _stub_session(monkeypatch, FakeSession())

    class FakeVaultRepo:
        def __init__(self, session: Any) -> None:
            pass

        async def get_unscoped(self, item_id: uuid.UUID) -> VaultItem:
            return item

    class FakeAccounts:
        def __init__(self, session: Any) -> None:
            pass

        async def get_for_user(self, user_id: uuid.UUID) -> FakeAccount:
            assert user_id == _USER
            return FakeAccount()

    monkeypatch.setattr(tasks, "VaultRepository", FakeVaultRepo)
    monkeypatch.setattr(
        "app.repositories.telegram.TelegramAccountRepository", FakeAccounts
    )
    monkeypatch.setattr(
        "app.services.telegram.client.TelegramClient", lambda *a, **k: client
    )

    await tasks._deliver_telegram_result(str(item.id))

    assert len(client.sent) == 1
    chat_id, text = client.sent[0]
    assert chat_id == "4242"
    assert short_id(item) in text, "the ack showed this id; the card has to show it too"


# --- the nudge ------------------------------------------------------------------------


def _stub_nudge(
    monkeypatch: pytest.MonkeyPatch,
    items: list[VaultItem],
    client: FakeClient,
    account: FakeAccount | None,
) -> list[tuple[str, int]]:
    """Wire `_nudge_slow_captures` to fakes and record what it asked the repository for."""
    from app.queue import tasks

    asked: list[tuple[str, int]] = []

    class FakeVaultRepo:
        def __init__(self, session: Any) -> None:
            pass

        async def list_slow_captures(
            self, source: str, older_than_minutes: int, limit: int = 100
        ) -> list[VaultItem]:
            asked.append((source, older_than_minutes))
            return items

    class FakeAccounts:
        def __init__(self, session: Any) -> None:
            pass

        async def get_for_user(self, user_id: uuid.UUID) -> FakeAccount | None:
            return account

    monkeypatch.setattr(tasks, "VaultRepository", FakeVaultRepo)
    monkeypatch.setattr(
        "app.repositories.telegram.TelegramAccountRepository", FakeAccounts
    )
    monkeypatch.setattr(
        "app.services.telegram.client.TelegramClient", lambda *a, **k: client
    )
    return asked


async def test_a_slow_capture_is_told_it_is_still_working(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.queue import tasks

    item = _item()
    client = FakeClient()
    session = FakeSession()
    _stub_session(monkeypatch, session)
    asked = _stub_nudge(monkeypatch, [item], client, FakeAccount())

    sent = await tasks._nudge_slow_captures()

    assert sent == 1
    assert asked == [("telegram", settings.NUDGE_AFTER_MINUTES)]
    chat_id, text = client.sent[0]
    assert chat_id == "4242"
    assert short_id(item) in text
    assert item.item_metadata["nudged_at"], "the flag is what makes it once, ever"
    assert session.commits == 1


async def test_an_already_nudged_item_is_never_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter is SQL, so a nudged row does not come back at all.

    Pinned as an empty result rather than as a second call, because a backlog of nudged
    rows must not be able to crowd the ones still waiting out of the `LIMIT`.
    """
    from app.queue import tasks

    client = FakeClient()
    _stub_session(monkeypatch, FakeSession())
    _stub_nudge(monkeypatch, [], client, FakeAccount())

    assert await tasks._nudge_slow_captures() == 0
    assert client.sent == []


async def test_a_failed_send_does_not_spend_the_one_nudge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag means "this person has been told". A send that never went out told nobody."""
    from app.queue import tasks

    item = _item()
    _stub_session(monkeypatch, FakeSession())
    _stub_nudge(monkeypatch, [item], FakeClient(fail=True), FakeAccount())

    assert await tasks._nudge_slow_captures() == 0
    assert "nudged_at" not in item.item_metadata


async def test_a_disconnected_account_is_stamped_rather_than_messaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise reconnecting delivers a nudge about something finished hours ago."""
    from app.queue import tasks

    item = _item()
    client = FakeClient()
    _stub_session(monkeypatch, FakeSession())
    _stub_nudge(monkeypatch, [item], client, account=None)

    assert await tasks._nudge_slow_captures() == 0
    assert client.sent == []
    assert item.item_metadata["nudged_at"]


# --- one update, one turn -------------------------------------------------------------


async def test_a_redelivered_update_is_dropped_before_any_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claim` is checked before the session, the indicator and the dispatcher.

    The autouse `_no_shared_redis_state` fixture stubs `claim` for every other test; this
    one puts its own stub over the top, which is the only way to assert the behaviour it
    exists to neutralise.
    """
    from app.queue import tasks
    from app.services.telegram import client as client_module
    from app.services.telegram import dispatch as dispatch_module

    seen: set[object] = set()

    async def _claim(update_id: object) -> bool:
        if update_id in seen:
            return False
        seen.add(update_id)
        return True

    handled: list[dict[str, Any]] = []

    class FakeDispatcher:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def handle(self, update: dict[str, Any]) -> Any:
            handled.append(update)
            return dispatch_module.DispatchResult()

    _stub_session(monkeypatch, FakeSession())
    monkeypatch.setattr("app.services.telegram.dedupe.claim", _claim)
    monkeypatch.setattr(client_module, "TelegramClient", lambda *a, **k: FakeClient())
    monkeypatch.setattr(dispatch_module, "TelegramDispatcher", FakeDispatcher)
    monkeypatch.setattr(tasks, "_recall_responder", lambda repo: None)
    monkeypatch.setattr("app.storage.get_storage", lambda: None)

    update = {
        "update_id": 77,
        "message": {"chat": {"id": 4242, "type": "private"}, "text": "hello"},
    }
    await tasks._handle_telegram_update(update)
    await tasks._handle_telegram_update(update)

    assert len(handled) == 1, "the second delivery must not reach the dispatcher"


async def test_an_update_with_no_id_is_still_answered() -> None:
    """Bookkeeping must not decide what gets a reply."""
    assert await real_claim(None) is True
    assert await real_claim("not-an-int") is True


async def test_dedupe_fails_open_when_redis_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis being down is already a queue outage; silence would double its blast radius."""
    from app.services.telegram import dedupe

    class BrokenRedis:
        async def set(self, *args: Any, **kwargs: Any) -> bool:
            raise ConnectionError("no route to host")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(dedupe.redis, "from_url", lambda *a, **k: BrokenRedis())

    assert await real_claim(1) is True


async def test_the_claim_is_a_set_nx_with_a_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SET NX` is the whole mechanism: two workers racing cannot both win the update.

    Asserted on the call rather than against a live Redis, because a developer machine
    has one and a test that writes to it is a test that passes once.
    """
    from app.core.config import settings
    from app.services.telegram import dedupe

    calls: list[dict[str, Any]] = []

    class RecordingRedis:
        def __init__(self, first: bool) -> None:
            self.first = first

        async def set(self, key: str, value: str, **kwargs: Any) -> bool | None:
            calls.append({"key": key, **kwargs})
            return True if self.first else None

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(dedupe.redis, "from_url", lambda *a, **k: RecordingRedis(True))
    assert await real_claim(77) is True

    monkeypatch.setattr(dedupe.redis, "from_url", lambda *a, **k: RecordingRedis(False))
    assert await real_claim(77) is False

    assert calls[0]["key"] == "tg:update:77"
    assert calls[0]["nx"] is True
    assert calls[0]["ex"] == settings.TELEGRAM_UPDATE_DEDUPE_TTL_SECONDS
