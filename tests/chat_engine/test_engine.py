"""Which lane a message takes, and that what comes back is structure rather than markup.

The engine is the piece a second surface reuses whole, so two properties matter more than
the routing itself: it never returns a formatted string, and it never needs to know which
surface asked.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.engine import ChatEngine, classify
from app.services.chat_engine.router import Intent
from app.services.chat_engine.types import (
    Attachment,
    ErrorBlock,
    ErrorKind,
    InboundMessage,
    ItemListBlock,
    OutboundReply,
    TextBlock,
)
from app.services.recall_chat import RecallAnswer

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


class FakeRecall:
    """Records which lane was taken; the lanes have their own tests."""

    def __init__(self, answer: RecallAnswer | None = None) -> None:
        self.answered: list[str] = []
        #: Kept as a permanently-empty list so the assertions below can still say "and
        #: not the other lane". There is no other lane now, and that is the claim.
        self.chatted: list[str] = []
        self.sessions: list[str] = []
        self._reply = answer or RecallAnswer(text="a reply")

    async def answer(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> RecallAnswer:
        self.answered.append(question)
        self.sessions.append(session_id)
        return self._reply




def _msg(text: str | None, **overrides: Any) -> InboundMessage:
    values: dict[str, Any] = {
        "surface": "unit-test",
        "external_user_id": "9001",
        "external_chat_id": "555000",
        "text": text,
    }
    values.update(overrides)
    return InboundMessage(**values)


def _item() -> VaultItem:
    return VaultItem(
        user_id=_USER,
        type=ContentType.article,
        title="Pasta",
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


# --- routing -------------------------------------------------------------------------


async def test_a_vault_question_takes_the_retrieval_lane() -> None:
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("what did I save this week?"))

    assert recall.answered == ["what did I save this week?"] and recall.chatted == []


async def test_a_greeting_is_answered_by_the_one_remaining_lane() -> None:
    """There is no lane without vault access any more.

    A greeting used to take the conversation lane precisely because it needed no vault --
    but that lane was also where every unrecognised phrasing ended up, and it could only
    deflect. With a capability card and a snapshot in the prompt, the answering lane
    handles "Hii" and can say what is actually in the vault in the same breath.
    """
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("Hii"))

    assert recall.answered == ["Hii"] and recall.chatted == []


async def test_a_question_about_the_bot_takes_the_chat_lane_not_retrieval() -> None:
    """META now takes the answering lane, and the reason it could not used to is gone.

    It was kept away from the answer prompt because that prompt had nothing but retrieved
    memories to speak from, so "who are you?" would have come back "I could not find
    anything about you in your vault". The prompt now carries a capability card, so the
    lane can say what the product is *and* show what the person has -- and one lane fewer
    is one fewer copy of the rules to keep in step.
    """
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("Who are you?"))

    assert recall.answered == ["Who are you?"] and recall.chatted == []


async def test_general_knowledge_goes_to_retrieval_rather_than_a_canned_decline() -> None:
    """The reversal, and the reasoning behind it.

    World trivia must still not be answered in this assistant's own voice -- that part is
    unchanged, and the answer prompt is what enforces it: it may speak only from the
    blocks it was given. What changed is where an unrecognised message goes. A closed
    gate in front of the conversation lane declined general knowledge correctly and
    declined "give me the list of my last saved" with exactly the same sentence, because
    neither matched a pattern. Retrieval cannot make that mistake: it answers from the
    person's own memories, and with nothing to answer from it says it found nothing.
    """
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("what is the capital of France?"))

    assert recall.answered == ["what is the capital of France?"]
    assert recall.chatted == []


async def test_ordinary_conversation_is_answered_rather_than_declined() -> None:
    """The scope gate blocks nothing now. A greeting was never an off-topic request."""
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("Hii, thanks for that"))

    assert recall.answered == ["Hii, thanks for that"]


async def test_asking_what_the_bot_can_do_is_never_declined() -> None:
    """META is this assistant being asked about itself -- in scope by definition."""
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("what can you do?"))

    assert recall.answered == ["what can you do?"]


async def test_the_conversation_key_is_scoped_to_the_account() -> None:
    """Not the chat alone: history is replayed into the next prompt."""
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("Hii", external_chat_id="42"))

    assert recall.sessions == [f"{_USER}:42"]


async def test_two_accounts_on_one_chat_do_not_share_a_conversation() -> None:
    """The same messaging identity, relinked to another account, must start clean.

    Chat history carries the titles and summaries already spoken aloud, so a key made of
    the chat alone would hand the second account the first one's memories.
    """
    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    first, second = FakeRecall(), FakeRecall()

    await ChatEngine(first, _USER).handle(_msg("Hii", external_chat_id="42"))
    await ChatEngine(second, other).handle(_msg("Hii", external_chat_id="42"))

    assert first.sessions != second.sessions


# --- what should never arrive here ---------------------------------------------------


async def test_an_attachment_is_answered_rather_than_raising() -> None:
    """Claimed by the caller before this runs; if one slips through it must not 500."""
    recall = FakeRecall()
    reply = await ChatEngine(recall, _USER).handle(
        _msg("look", attachments=[Attachment(kind="document", file_id="abc")])
    )

    assert recall.answered == ["look"] and recall.chatted == []
    assert reply.blocks


async def test_a_command_is_answered_rather_than_raising() -> None:
    recall = FakeRecall()
    reply = await ChatEngine(recall, _USER).handle(_msg("/recent"))

    assert recall.answered == ["/recent"] and reply.blocks


async def test_a_group_message_is_answered_with_nothing_at_all() -> None:
    """One member's vault must never be read aloud to a room."""
    recall = FakeRecall()
    reply = await ChatEngine(recall, _USER).handle(_msg("Hii", is_private=False))

    assert reply == OutboundReply()
    assert recall.chatted == [] and recall.answered == []


async def test_a_message_with_no_text_is_answered_conversationally() -> None:
    """`text` is None on an attachment-only message, which is ordinary, not an edge."""
    recall = FakeRecall()
    reply = await ChatEngine(recall, _USER).handle(_msg(None))

    assert recall.answered == [""] and recall.chatted == []
    assert reply.blocks


# --- blocks, never strings -----------------------------------------------------------


async def test_prose_comes_back_as_a_text_block() -> None:
    recall = FakeRecall(RecallAnswer(text="You saved two things about pasta."))
    reply = await ChatEngine(recall, _USER).handle(_msg("Hii"))

    assert reply.blocks == [TextBlock(text="You saved two things about pasta.")]


async def test_a_listing_comes_back_as_rows_not_lines() -> None:
    item = _item()
    recall = FakeRecall(RecallAnswer(items=[item], total=7))
    reply = await ChatEngine(recall, _USER).handle(_msg("what did I save this week?"))

    assert reply.blocks == [ItemListBlock(items=[item], total=7)]


async def test_a_failure_comes_back_as_an_error_kind_not_a_sentence() -> None:
    """The wording belongs to the surface; the engine names the situation."""
    recall = FakeRecall(RecallAnswer(failed=True))
    reply = await ChatEngine(recall, _USER).handle(_msg("Hii"))

    assert reply.blocks == [ErrorBlock(kind=ErrorKind.provider_failure)]


async def test_no_block_carries_markup() -> None:
    """The whole contract: a second surface must not have to strip another's markup."""
    recall = FakeRecall(RecallAnswer(text="plain <b>text</b> from a model"))
    reply = await ChatEngine(recall, _USER).handle(_msg("Hii"))

    block = reply.blocks[0]
    assert isinstance(block, TextBlock)
    # Whatever the model said is passed through untouched -- not escaped, not wrapped.
    assert block.text == "plain <b>text</b> from a model"


# --- classify: the one shape decision, for callers that must serve some of it ---------


def test_classify_answers_all_five_intents() -> None:
    """A caller asks this what it is holding; it never works that out for itself."""
    assert classify(_msg("/recent")) is Intent.COMMAND
    assert classify(_msg("https://x.com/a", url="https://x.com/a")) is Intent.CAPTURE
    assert classify(_msg("Who are you?")) is Intent.META
    assert classify(_msg("what did I save this week?")) is Intent.RECALL
    assert classify(_msg("Hii")) is Intent.CHAT


def test_classify_prefers_the_url_the_surface_parsed() -> None:
    """A hyperlinked label hides its target; only the surface that parsed it knows."""
    msg = _msg("click here for the recipe", url="https://insta.com/reel/9")
    assert classify(msg) is Intent.CAPTURE


def test_classify_falls_back_to_scanning_when_no_url_was_parsed() -> None:
    """A surface with no entity markup still gets capture routing."""
    assert classify(_msg("look at https://x.com/a")) is Intent.CAPTURE


def test_classify_sees_an_attachment_with_no_text() -> None:
    msg = _msg(None, attachments=[Attachment(kind="photo", file_id="p")])
    assert classify(msg) is Intent.CAPTURE


@pytest.mark.parametrize(
    ("text", "intent", "lane"),
    [
        ("what did I save this week?", Intent.RECALL, "answered"),
        ("Who are you?", Intent.META, "answered"),
        ("Hii", Intent.CHAT, "answered"),
        ("/froobulate", Intent.COMMAND, "answered"),
    ],
)
async def test_handle_takes_the_lane_classify_predicts(
    text: str, intent: Intent, lane: str
) -> None:
    """Two entry points, one decision -- or the caller and the engine disagree silently."""
    recall = FakeRecall()
    assert classify(_msg(text)) is intent

    await ChatEngine(recall, _USER).handle(_msg(text))

    assert getattr(recall, lane) == [text]
    other = "chatted" if lane == "answered" else "answered"
    assert getattr(recall, other) == []


# --- the status lane -------------------------------------------------------------------


class FakeSaves:
    def __init__(self) -> None:
        self.reads: list[tuple[uuid.UUID, int]] = []

    async def recent_saves(
        self, user_id: uuid.UUID, limit: int
    ) -> tuple[list[VaultItem], int]:
        self.reads.append((user_id, limit))
        return [_item()], 1


async def test_a_status_question_calls_no_model_at_all() -> None:
    """The lane's whole justification. "Did that save?" is a question about a row, and
    a generator asked it will answer plausibly whatever the row says."""
    recall = FakeRecall()
    saves = FakeSaves()
    reply = await ChatEngine(recall, _USER, saves=saves).handle(_msg("Is it saved?"))
    assert recall.answered == []
    assert recall.chatted == []
    assert saves.reads == [(_USER, 5)]
    assert isinstance(reply.blocks[0], TextBlock)


async def test_the_status_lane_reads_the_engines_own_user() -> None:
    """`user_id` comes from the constructor -- the caller's resolved account -- and
    never from the message."""
    saves = FakeSaves()
    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    await ChatEngine(FakeRecall(), other, saves=saves).handle(_msg("did it save?"))
    assert saves.reads == [(other, 5)]


async def test_status_works_with_no_chat_model_configured() -> None:
    saves = FakeSaves()
    reply = await ChatEngine(None, _USER, saves=saves).handle(_msg("Is it saved?"))
    assert saves.reads == [(_USER, 5)]
    assert isinstance(reply.blocks[0], TextBlock)


async def test_status_without_a_reader_degrades_to_retrieval_not_to_chat() -> None:
    """The safe direction. Retrieval answers from the vault or says it found nothing;
    the conversation lane has no vault access and would say it cannot check -- which is
    the exact answer this whole lane exists to stop."""
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("Is it saved?"))
    assert recall.answered == ["Is it saved?"]
    assert recall.chatted == []


async def test_a_lane_that_needs_a_model_says_so_when_there_is_none() -> None:
    reply = await ChatEngine(None, _USER, saves=FakeSaves()).handle(_msg("Hii"))
    assert reply == OutboundReply([ErrorBlock(ErrorKind.chat_unavailable)])
