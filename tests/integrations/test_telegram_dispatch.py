"""Dispatch: who is allowed to make the bot do anything, and what each message becomes.

An update carries no session. The only thing that turns a Telegram sender into a RecallAI
user is the `telegram_accounts` lookup in here, which makes this module the whole of the
bot's authorisation. The first three tests are the ones that matter: an unknown sender, a
group chat, and a sender whose id belongs to a different account all have to come away
with nothing.

Offline by design -- repositories, the Bot API and Redis are all stood in for, so this
runs without a database, a broker or a network.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.telegram import TelegramAccount
from app.models.vault import VaultItem
from app.services.recall_chat import RecallAnswer
from app.services.telegram import limits
from app.services.telegram.dispatch import TelegramDispatcher
from app.services.telegram.linking import LinkOutcome, LinkResult

_ALICE = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item(**overrides: Any) -> VaultItem:
    values: dict[str, Any] = {
        "user_id": _ALICE,
        "type": ContentType.article,
        "title": "A saved thing",
        "processing_status": ProcessingStatus.pending,
    }
    values.update(overrides)
    return VaultItem(**values)


class FakeLinks:
    def __init__(self, account: TelegramAccount | None = None) -> None:
        self.account = account
        self.consumed: list[str] = []
        self.disconnected: list[uuid.UUID] = []
        self.outcome = LinkOutcome(LinkResult.linked, account)

    async def resolve(self, telegram_user_id: str) -> TelegramAccount | None:
        return self.account

    async def consume(self, raw_token: str, identity: Any) -> LinkOutcome:
        self.consumed.append(raw_token)
        return self.outcome

    async def disconnect(self, user_id: uuid.UUID) -> bool:
        self.disconnected.append(user_id)
        return True


class FakeVault:
    def __init__(self) -> None:
        self.saved_urls: list[tuple[uuid.UUID, str]] = []
        self.notes: list[tuple[uuid.UUID, str, str]] = []
        self.listed: list[uuid.UUID] = []
        self.duplicate = False

    async def save_url(
        self, user_id: uuid.UUID, url: str, title: str | None = None, **kwargs: Any
    ) -> tuple[VaultItem, bool]:
        self.saved_urls.append((user_id, url))
        return _item(source_url=url), not self.duplicate

    async def create_note(
        self, user_id: uuid.UUID, title: str, content: str, **kwargs: Any
    ) -> VaultItem:
        self.notes.append((user_id, title, content))
        return _item(type=ContentType.note, title=title, content=content)

    async def list_recent(self, user_id: uuid.UUID, limit: int, **kwargs: Any) -> Any:
        self.listed.append(user_id)
        return [_item(title="Older thing")], 1


class FakeClient:
    """The dispatcher never sends directly; it returns text for the task to send."""


class FakeRecall:
    """The real split between chat and retrieval lives in `RecallChatService.respond`.

    The dispatcher's contract is only that plain text reaches it and nothing is written,
    so the fake records rather than classifies.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def respond(
        self, user_id: uuid.UUID, text: str, session_id: str
    ) -> RecallAnswer:
        self.asked.append(text)
        return RecallAnswer(text="You saved three things about pasta.")


@pytest.fixture(autouse=True)
def _no_rate_limiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis is not available offline; the limiter has its own tests."""

    async def _allow(telegram_user_id: str, action: limits.Action) -> bool:
        return True

    monkeypatch.setattr(limits, "allow", _allow)


def _account() -> TelegramAccount:
    return TelegramAccount(
        user_id=_ALICE, telegram_user_id="555000", telegram_chat_id="555000"
    )


def _update(text: str | None = None, chat_type: str = "private", **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 1,
        "chat": {"id": 555000, "type": chat_type},
        "from": {"id": 555000, "first_name": "Ada"},
    }
    if text is not None:
        message["text"] = text
    message.update(extra)
    return {"update_id": 1, "message": message}


def _dispatcher(
    links: FakeLinks, vault: FakeVault | None = None, recall: Any = None
) -> TelegramDispatcher:
    return TelegramDispatcher(links, vault or FakeVault(), FakeClient(), recall)  # type: ignore[arg-type]


async def test_unlinked_sender_learns_nothing() -> None:
    links = FakeLinks(account=None)
    vault = FakeVault()
    result = await _dispatcher(links, vault).handle(_update("what did I save?"))

    assert result.reply is not None
    assert "connected" in result.reply.lower()
    # No count, no title, no confirmation that an account exists at all.
    assert vault.listed == [] and vault.saved_urls == [] and vault.notes == []


async def test_group_chat_is_ignored_entirely() -> None:
    """A bot added to a group must not read one member's vault aloud to the room."""
    links = FakeLinks(_account())
    vault = FakeVault()
    result = await _dispatcher(links, vault).handle(
        _update("/recent", chat_type="supergroup")
    )

    assert result.reply is None
    assert vault.listed == []


async def test_actions_run_against_the_bound_user_not_the_sender() -> None:
    links = FakeLinks(_account())
    vault = FakeVault()
    await _dispatcher(links, vault).handle(_update("https://example.com/post"))

    assert vault.saved_urls == [(_ALICE, "https://example.com/post")]


async def test_url_is_saved_and_queued_for_processing() -> None:
    vault = FakeVault()
    result = await _dispatcher(FakeLinks(_account()), vault).handle(
        _update("look at https://example.com/reel")
    )

    assert vault.saved_urls == [(_ALICE, "https://example.com/reel")]
    # Enqueued by the caller, after the commit -- never from inside the transaction.
    assert len(result.enqueue_item_ids) == 1


async def test_duplicate_url_is_not_reprocessed() -> None:
    vault = FakeVault()
    vault.duplicate = True
    result = await _dispatcher(FakeLinks(_account()), vault).handle(
        _update("https://example.com/reel")
    )

    assert result.enqueue_item_ids == []
    assert result.reply is not None and "Already in your vault" in result.reply


async def test_hyperlinked_label_resolves_to_its_target() -> None:
    """A `text_link` entity is the only place the real URL appears."""
    vault = FakeVault()
    await _dispatcher(FakeLinks(_account()), vault).handle(
        _update(
            "this one",
            entities=[{"type": "text_link", "offset": 0, "length": 8, "url": "https://real.example/x"}],
        )
    )
    assert vault.saved_urls == [(_ALICE, "https://real.example/x")]


async def test_plain_text_is_answered_and_never_saved() -> None:
    """"hi" is talk. A bot that filed every greeting would fill the vault with rubbish."""
    vault = FakeVault()
    recall = FakeRecall()
    result = await _dispatcher(FakeLinks(_account()), vault, recall).handle(_update("hi"))

    assert recall.asked == ["hi"]
    assert vault.notes == [] and vault.saved_urls == []
    assert result.reply == "You saved three things about pasta."
    assert result.enqueue_item_ids == []


async def test_a_question_goes_to_recall_not_to_capture() -> None:
    vault = FakeVault()
    recall = FakeRecall()
    result = await _dispatcher(FakeLinks(_account()), vault, recall).handle(
        _update("any pasta videos?")
    )

    assert recall.asked == ["any pasta videos?"]
    assert vault.notes == []
    assert result.reply == "You saved three things about pasta."


async def test_without_a_recall_service_plain_text_is_still_not_saved() -> None:
    """The inverse of the old rule, and deliberately so.

    Filing unanswerable text as a note was the "safe" default only if a stray note is
    cheaper than a lost one. With `/note` there is an explicit way to keep a thought, so
    the silent save has no job left -- and the junk it accumulates is discovered in bulk,
    long after the user could connect it to anything they typed.
    """
    vault = FakeVault()
    result = await _dispatcher(FakeLinks(_account()), vault, recall=None).handle(
        _update("any pasta videos?")
    )

    assert vault.notes == []
    assert result.reply is not None and "can't chat" in result.reply


async def test_a_link_is_saved_even_when_the_message_reads_as_a_question() -> None:
    """Shape beats phrasing. Someone pasting a reel is not opening a negotiation."""
    vault = FakeVault()
    recall = FakeRecall()
    await _dispatcher(FakeLinks(_account()), vault, recall).handle(
        _update("what is this? https://www.instagram.com/reel/abc/")
    )

    assert vault.saved_urls == [(_ALICE, "https://www.instagram.com/reel/abc/")]
    assert recall.asked == []


async def test_note_command_saves_the_argument_not_the_command() -> None:
    vault = FakeVault()
    recall = FakeRecall()
    result = await _dispatcher(FakeLinks(_account()), vault, recall).handle(
        _update("/note ring the dentist on Monday")
    )

    assert vault.notes and vault.notes[0][2] == "ring the dentist on Monday"
    assert recall.asked == []
    assert result.enqueue_item_ids  # it still goes through the AI pipeline


async def test_note_command_keeps_a_link_as_a_thought() -> None:
    """`/note https://…` is explicit: the person wants the link kept, not fetched."""
    vault = FakeVault()
    await _dispatcher(FakeLinks(_account()), vault).handle(
        _update("/note https://example.com/read-later")
    )

    assert vault.saved_urls == []
    assert vault.notes and vault.notes[0][2] == "https://example.com/read-later"


async def test_bare_note_command_explains_itself_and_saves_nothing() -> None:
    vault = FakeVault()
    result = await _dispatcher(FakeLinks(_account()), vault).handle(_update("/note"))

    assert vault.notes == []
    assert result.reply is not None and "/note" in result.reply


async def test_voice_note_is_refused_out_loud() -> None:
    vault = FakeVault()
    result = await _dispatcher(FakeLinks(_account()), vault).handle(
        _update(voice={"file_id": "abc", "duration": 3})
    )

    assert result.reply is not None and "voice notes" in result.reply
    assert result.enqueue_item_ids == []


async def test_start_with_a_token_links_before_any_lookup() -> None:
    links = FakeLinks(account=None)
    links.outcome = LinkOutcome(LinkResult.linked, _account())
    result = await _dispatcher(links).handle(_update("/start abc123"))

    assert links.consumed == ["abc123"]
    assert result.reply is not None and "Connected" in result.reply


async def test_a_stolen_link_is_refused_with_a_distinct_message() -> None:
    links = FakeLinks(account=None)
    links.outcome = LinkOutcome(LinkResult.taken_by_other_user)
    result = await _dispatcher(links).handle(_update("/start abc123"))

    assert result.reply is not None
    assert "different RecallAI account" in result.reply


async def test_every_failed_redemption_reads_the_same() -> None:
    """Unknown, spent and expired are one message: distinguishing them leaks."""
    links = FakeLinks(account=None)
    links.outcome = LinkOutcome(LinkResult.invalid_token)
    result = await _dispatcher(links).handle(_update("/start abc123"))

    assert result.reply is not None and "isn't valid any more" in result.reply


async def test_rate_limited_sender_costs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _deny(telegram_user_id: str, action: limits.Action) -> bool:
        return False

    monkeypatch.setattr(limits, "allow", _deny)
    vault = FakeVault()
    recall = FakeRecall()
    result = await _dispatcher(FakeLinks(_account()), vault, recall).handle(
        _update("any pasta videos?")
    )

    assert recall.asked == [] and vault.notes == []
    assert result.reply is not None and "give it an hour" in result.reply


async def test_disconnect_unbinds_the_owner() -> None:
    links = FakeLinks(_account())
    result = await _dispatcher(links).handle(_update("/disconnect"))

    assert links.disconnected == [_ALICE]
    assert result.reply is not None and "Disconnected" in result.reply


async def test_command_with_bot_suffix_is_recognised() -> None:
    links = FakeLinks(_account())
    vault = FakeVault()
    await _dispatcher(links, vault).handle(_update("/recent@recallai_bot"))
    assert vault.listed == [_ALICE]


async def test_unlinked_sender_gets_a_one_tap_connect_button(monkeypatch: Any) -> None:
    """The only affordance an unidentified sender is offered, and it reveals nothing.

    The button target comes from `FRONTEND_URL`, never from the update -- an inline
    button is a link the user is being invited to trust.
    """
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    result = await _dispatcher(FakeLinks(account=None)).handle(_update("/start"))

    assert result.reply_markup == {
        "inline_keyboard": [
            [{"text": "Connect my account", "url": "https://app.example.com/capture"}]
        ]
    }


async def test_linked_sender_is_offered_no_connect_button(monkeypatch: Any) -> None:
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com")
    result = await _dispatcher(FakeLinks(_account())).handle(_update("/start"))

    assert result.reply_markup is None
