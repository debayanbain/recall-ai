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
        self.chatted: list[str] = []
        self.sessions: list[str] = []
        self._reply = answer or RecallAnswer(text="a reply")

    async def answer(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> RecallAnswer:
        self.answered.append(question)
        self.sessions.append(session_id)
        return self._reply

    async def chat(self, message: str, session_id: str) -> RecallAnswer:
        self.chatted.append(message)
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


async def test_a_greeting_takes_the_chat_lane() -> None:
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("Hii"))

    assert recall.chatted == ["Hii"] and recall.answered == []


async def test_a_question_about_the_bot_takes_the_chat_lane_not_retrieval() -> None:
    """META must never reach the answer prompt: it has no memory to answer from."""
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("Who are you?"))

    assert recall.chatted == ["Who are you?"] and recall.answered == []


async def test_general_knowledge_is_chat_not_retrieval() -> None:
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("what is the capital of France?"))

    assert recall.chatted == ["what is the capital of France?"]
    assert recall.answered == []


async def test_the_chat_id_is_the_conversation_key() -> None:
    recall = FakeRecall()
    await ChatEngine(recall, _USER).handle(_msg("Hii", external_chat_id="42"))

    assert recall.sessions == ["42"]


# --- what should never arrive here ---------------------------------------------------


async def test_an_attachment_is_answered_rather_than_raising() -> None:
    """Claimed by the caller before this runs; if one slips through it must not 500."""
    recall = FakeRecall()
    reply = await ChatEngine(recall, _USER).handle(
        _msg("look", attachments=[Attachment(kind="document", file_id="abc")])
    )

    assert recall.chatted == ["look"] and recall.answered == []
    assert reply.blocks


async def test_a_command_is_answered_rather_than_raising() -> None:
    recall = FakeRecall()
    reply = await ChatEngine(recall, _USER).handle(_msg("/recent"))

    assert recall.chatted == ["/recent"] and reply.blocks


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

    assert recall.chatted == [""] and recall.answered == []
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
        ("Who are you?", Intent.META, "chatted"),
        ("Hii", Intent.CHAT, "chatted"),
        ("/froobulate", Intent.COMMAND, "chatted"),
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
