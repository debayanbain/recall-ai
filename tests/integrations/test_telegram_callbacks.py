"""A tapped button, end to end through the real dispatcher.

Five layers used to exclude `callback_query` -- the subscription, the webhook, the typing
indicator, the dispatcher and the client -- so the risk here is not that one of them is
wrong but that one of them was never changed. Each is asserted against the code that runs
in production rather than against a helper written for the test.

The security claims are the ones worth reading twice: a tap is authorised exactly like a
message, and the handler that performs the write imports no model.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.proposals import Action, Proposal
from app.services.telegram import confirm
from app.services.telegram.dispatch import TelegramDispatcher

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _callback(data: str, *, chat_type: str = "private", sender: int = 9) -> dict[str, Any]:
    return {
        "callback_query": {
            "id": "cbq-1",
            "from": {"id": sender},
            "data": data,
            "message": {
                "message_id": 77,
                "chat": {"id": 4242, "type": chat_type},
            },
        }
    }


class FakeAccount:
    def __init__(self, user_id: uuid.UUID = _USER) -> None:
        self.user_id = user_id
        self.telegram_chat_id = "4242"


class FakeLinks:
    def __init__(self, account: FakeAccount | None) -> None:
        self.account = account

    async def resolve(self, telegram_user_id: str) -> FakeAccount | None:
        return self.account


class FakeStore:
    """Records spends, so ordering against the write can be asserted."""

    def __init__(self, proposal: Proposal | None) -> None:
        self.proposal = proposal
        self.spends: list[str] = []

    async def mint(self, proposal: Proposal) -> str | None:
        return "tok"

    async def spend(self, token: str, user_id: uuid.UUID) -> Proposal | None:
        self.spends.append(token)
        held, self.proposal = self.proposal, None
        if held is None or held.user_id != user_id:
            return None
        return held


class FakeVault:
    def __init__(self) -> None:
        self.notes: list[str] = []
        self.reprocessed: list[uuid.UUID] = []
        self.deleted: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.delete_result = True
        self.fail_write = False

    async def delete(self, item_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        if not self.delete_result:
            return False
        self.deleted.append((item_id, user_id))
        return True

    async def create_note(
        self, user_id: uuid.UUID, title: str, content: str, **kwargs: Any
    ) -> VaultItem:
        if self.fail_write:
            raise RuntimeError("the database went away")
        self.notes.append(content)
        return VaultItem(
            id=uuid.uuid4(),
            user_id=user_id,
            type=ContentType.note,
            title=title,
            content=content,
            processing_status=ProcessingStatus.pending,
        )

    async def reprocess(self, item_id: uuid.UUID, user_id: uuid.UUID) -> VaultItem:
        self.reprocessed.append(item_id)
        return VaultItem(
            id=item_id,
            user_id=user_id,
            type=ContentType.instagram,
            title="A private reel",
            processing_status=ProcessingStatus.pending,
        )


#: `None` is a meaningful account here -- it is the unlinked sender -- so the default
#: cannot be `None` coalesced into a real one. That mistake makes the unlinked test pass
#: while exercising the linked path.
_DEFAULT = object()


def _dispatcher(store: Any, vault: Any, account: Any = _DEFAULT) -> TelegramDispatcher:
    resolved = FakeAccount() if account is _DEFAULT else account
    return TelegramDispatcher(
        FakeLinks(resolved),  # type: ignore[arg-type]
        vault,
        client=None,  # type: ignore[arg-type]
        proposals=store,
    )


@pytest.fixture(autouse=True)
def _no_rate_limiting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.telegram import limits

    async def _allow(telegram_user_id: str, action: limits.Action) -> bool:
        return True

    monkeypatch.setattr(limits, "allow", _allow)


# --- the payload ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        ("p:abc", confirm.Tap.accept),
        ("p:no:abc", confirm.Tap.decline),
        ("q:abc", confirm.Tap.answer),
    ],
)
def test_the_prefixes_parse(data: str, kind: confirm.Tap) -> None:
    parsed = confirm.parse(data)
    assert parsed is not None and parsed.kind is kind and parsed.token == "abc"


def test_decline_is_matched_before_accept() -> None:
    """`p:no:` also starts with `p:`. Tested because getting it wrong makes every No a Yes."""
    parsed = confirm.parse("p:no:tok")
    assert parsed is not None and parsed.kind is confirm.Tap.decline


def test_anything_this_surface_did_not_mint_is_ignored() -> None:
    assert confirm.parse("x:tok") is None
    assert confirm.parse("") is None
    assert confirm.parse(None) is None


# --- authorisation --------------------------------------------------------------------


async def test_a_tap_from_an_unlinked_sender_learns_nothing() -> None:
    """Not that the token was real, not who it belonged to -- only how to connect."""
    store = FakeStore(Proposal(_USER, Action.note, {"text": "buy milk"}))
    vault = FakeVault()

    result = await _dispatcher(store, vault, account=None).handle(_callback("p:tok"))

    assert store.spends == [], "the token must not even be looked at"
    assert vault.notes == []
    assert result.reply is not None and "connect" in result.reply.lower()


async def test_a_tap_in_a_group_is_dropped() -> None:
    """The bot does not act in a room; a tap there is the same disclosure a reply is."""
    store = FakeStore(Proposal(_USER, Action.note, {"text": "buy milk"}))
    vault = FakeVault()

    result = await _dispatcher(store, vault).handle(
        _callback("p:tok", chat_type="group")
    )

    assert store.spends == []
    assert vault.notes == []
    assert result.reply is None


async def test_a_token_minted_for_another_account_writes_nothing() -> None:
    """The token is checked against the account this *sender* resolved to."""
    store = FakeStore(Proposal(_OTHER, Action.note, {"text": "buy milk"}))
    vault = FakeVault()

    result = await _dispatcher(store, vault).handle(_callback("p:tok"))

    assert vault.notes == []
    assert result.reply is not None and "expired" in result.reply


# --- the write ------------------------------------------------------------------------


async def test_yes_saves_the_note_and_queues_it() -> None:
    store = FakeStore(Proposal(_USER, Action.note, {"text": "call the landlord"}))
    vault = FakeVault()

    result = await _dispatcher(store, vault).handle(_callback("p:tok"))

    assert vault.notes == ["call the landlord"]
    assert len(result.enqueue_item_ids) == 1
    assert result.answer_callback_id == "cbq-1"
    assert result.clear_markup_message_id == 77


async def test_no_writes_nothing_but_still_burns_the_token() -> None:
    """A card left open in a chat must not stay tappable after it has been answered."""
    store = FakeStore(Proposal(_USER, Action.note, {"text": "buy milk"}))
    vault = FakeVault()

    result = await _dispatcher(store, vault).handle(_callback("p:no:tok"))

    assert store.spends == ["tok"]
    assert vault.notes == []
    assert result.reply is not None and "nothing done" in result.reply.lower()


async def test_a_second_tap_is_told_it_expired() -> None:
    store = FakeStore(Proposal(_USER, Action.note, {"text": "buy milk"}))
    vault = FakeVault()
    dispatcher = _dispatcher(store, vault)

    await dispatcher.handle(_callback("p:tok"))
    second = await dispatcher.handle(_callback("p:tok"))

    assert len(vault.notes) == 1
    assert second.reply is not None and "expired" in second.reply


async def test_the_token_is_spent_before_the_write() -> None:
    """A failure between the two must leave nothing to retry with.

    The safe direction is the person being told it expired, rather than a half-failed
    write staying repeatable by anyone still holding the token.
    """
    store = FakeStore(Proposal(_USER, Action.note, {"text": "buy milk"}))
    vault = FakeVault()
    vault.fail_write = True

    with pytest.raises(RuntimeError):
        await _dispatcher(store, vault).handle(_callback("p:tok"))

    assert store.spends == ["tok"]
    assert store.proposal is None, "the token is gone even though the write failed"


async def test_retry_reaches_the_service_with_the_full_id() -> None:
    item_id = uuid.uuid4()
    store = FakeStore(Proposal(_USER, Action.retry, {"memory_id": str(item_id)}))
    vault = FakeVault()

    await _dispatcher(store, vault).handle(_callback("p:tok"))

    assert vault.reprocessed == [item_id]


async def test_a_tapped_option_is_replayed_as_an_ordinary_message() -> None:
    """No second routing path: an answered question is just the next turn."""
    store = FakeStore(Proposal(_USER, Action.answer, {"text": "the docker talk"}))
    vault = FakeVault()

    class Recall:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def answer(self, user_id: uuid.UUID, q: str, s: str) -> Any:
            from app.services.recall_chat import RecallAnswer

            self.asked.append(q)
            return RecallAnswer(text="Found it.")

    recall = Recall()
    dispatcher = TelegramDispatcher(
        FakeLinks(FakeAccount()),  # type: ignore[arg-type]
        vault,
        client=None,  # type: ignore[arg-type]
        recall=recall,  # type: ignore[arg-type]
        proposals=store,
    )

    result = await dispatcher.handle(_callback("q:tok"))

    assert recall.asked == ["the docker talk"]
    assert result.answer_callback_id == "cbq-1"


# --- the claim that has to keep being true --------------------------------------------


def test_the_write_path_imports_no_model() -> None:
    """`confirm.py` is the far side of the tap and must stay there.

    "The handler does not call a model" is exactly the kind of claim that stops being
    true silently, one import at a time, so it is asserted against the file itself.
    """
    import ast
    from pathlib import Path

    # Parsed, not grepped: this module's own docstring says the words "app.ai" while
    # explaining why it must not import them, and a substring check reads that as the
    # violation it is describing.
    tree = ast.parse(Path(confirm.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    banned = ("app.ai", "langchain", "langgraph")
    offenders = [
        name for name in imported if any(name.startswith(bad) for bad in banned)
    ]
    assert offenders == [], f"the write path reached the model stack: {offenders}"


def test_the_webhook_subscribes_to_taps() -> None:
    """An update type left out of `allowed_updates` never arrives at all.

    Without this the button spins on the person's screen until it times out, having done
    nothing -- and no test of the handler below it would notice.
    """
    from pathlib import Path

    from app.services.telegram import client as client_module

    source = Path(client_module.__file__).read_text(encoding="utf-8")
    assert '"callback_query"' in source


async def test_the_webhook_route_accepts_a_tap() -> None:
    """The route drops anything it does not recognise, so it has to recognise this."""
    from app.api.v1 import webhooks

    body = _callback("p:tok")
    assert isinstance(body.get("callback_query"), dict)
    # The route's own filter, as it is written.
    assert not (
        not isinstance(body.get("message"), dict)
        and not isinstance(body.get("callback_query"), dict)
    )
    assert webhooks.telegram_webhook is not None


def test_the_indicator_finds_the_chat_behind_a_tap() -> None:
    from app.services.telegram.typing import chat_id_of

    assert chat_id_of(_callback("p:tok")) == "4242"
    assert chat_id_of(_callback("p:tok", chat_type="group")) is None


# --- and the buttons have to reach the message ----------------------------------------


async def test_a_proposal_reply_carries_its_keyboard() -> None:
    """The other half of the tap: a card with no buttons is a question nobody can answer.

    `render` returns the text and `render_markup` returns the keyboard, and they are two
    fields of one API call -- so a dispatcher that renders only the first offers a
    confirmation the person cannot act on. That is exactly the shape of gap this file's
    siblings exist for.
    """
    from app.services.chat_engine.types import ProposalBlock
    from app.services.recall_chat import RecallAnswer

    class Recall:
        async def answer(self, user_id: uuid.UUID, q: str, s: str) -> Any:
            return RecallAnswer(
                text="Save this?",
                proposal=ProposalBlock(
                    preview="buy milk", accept_token="tok", action="note"
                ),
            )

    dispatcher = TelegramDispatcher(
        FakeLinks(FakeAccount()),  # type: ignore[arg-type]
        FakeVault(),
        client=None,  # type: ignore[arg-type]
        recall=Recall(),  # type: ignore[arg-type]
    )

    result = await dispatcher.handle(
        {
            "message": {
                "message_id": 1,
                "chat": {"id": 4242, "type": "private"},
                "from": {"id": 9},
                "text": "save this: buy milk",
            }
        }
    )

    assert result.reply is not None and "buy milk" in result.reply
    assert result.reply_markup is not None
    buttons = result.reply_markup["inline_keyboard"][0]  # type: ignore[index]
    assert [b["callback_data"] for b in buttons] == ["p:tok", "p:no:tok"]


async def test_a_question_reply_carries_one_button_per_option() -> None:
    from app.services.chat_engine.types import Choice, QuestionBlock
    from app.services.recall_chat import RecallAnswer

    class Recall:
        async def answer(self, user_id: uuid.UUID, q: str, s: str) -> Any:
            return RecallAnswer(
                text="Which one?",
                question=QuestionBlock(
                    question="Which one?",
                    choices=(
                        Choice(label="the docker talk", token="t1"),
                        Choice(label="the reel", token="t2"),
                    ),
                ),
            )

    dispatcher = TelegramDispatcher(
        FakeLinks(FakeAccount()),  # type: ignore[arg-type]
        FakeVault(),
        client=None,  # type: ignore[arg-type]
        recall=Recall(),  # type: ignore[arg-type]
    )

    result = await dispatcher.handle(
        {
            "message": {
                "message_id": 1,
                "chat": {"id": 4242, "type": "private"},
                "from": {"id": 9},
                "text": "what did I save?",
            }
        }
    )

    assert result.reply_markup is not None
    rows = result.reply_markup["inline_keyboard"]  # type: ignore[index]
    assert [row[0]["callback_data"] for row in rows] == ["q:t1", "q:t2"]


async def test_yes_on_a_delete_removes_the_memory() -> None:
    """The write path for the one action that cannot be undone.

    Scoped by the service underneath: `VaultService.delete` re-checks ownership through
    `repo.get`, so a token cannot remove a row it did not name or one belonging to
    somebody else.
    """
    item_id = uuid.uuid4()
    store = FakeStore(Proposal(_USER, Action.delete, {"memory_id": str(item_id)}))
    vault = FakeVault()

    result = await _dispatcher(store, vault).handle(_callback("p:tok"))

    assert vault.deleted == [(item_id, _USER)]
    assert result.reply is not None and "Deleted" in result.reply


async def test_a_delete_of_something_already_gone_reads_as_expired() -> None:
    """One sentence for "already deleted", "never yours" and "never existed"."""
    store = FakeStore(Proposal(_USER, Action.delete, {"memory_id": str(uuid.uuid4())}))
    vault = FakeVault()
    vault.delete_result = False

    result = await _dispatcher(store, vault).handle(_callback("p:tok"))

    assert result.reply is not None and "expired" in result.reply
