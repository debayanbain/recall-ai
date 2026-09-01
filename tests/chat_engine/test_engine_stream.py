"""Routing a streamed reply: the same lanes, the same gates, different delivery.

The engine hands lane events straight to the surface, so what is pinned here is that
*routing* did not change shape when streaming was added -- a status question is still a
database read with no model call, an out-of-scope message is still declined before one,
and lanes that cannot stream still produce a working answer.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine import scope
from app.services.chat_engine.engine import ChatEngine
from app.services.chat_engine.types import (
    Delta,
    ErrorKind,
    InboundMessage,
    ItemsEvent,
    StreamEnd,
    StreamEvent,
)
from app.services.recall_chat import RecallAnswer

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


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


class BlockingLanes:
    """Lanes with no streaming methods at all -- the fallback case."""

    def __init__(self, answer: RecallAnswer | None = None) -> None:
        self.answered: list[str] = []
        self.chatted: list[str] = []
        self._reply = answer or RecallAnswer(text="a whole reply")

    async def answer(self, user_id: uuid.UUID, q: str, s: str) -> RecallAnswer:
        self.answered.append(q)
        return self._reply

    async def chat(self, message: str, s: str) -> RecallAnswer:
        self.chatted.append(message)
        return self._reply


class StreamingLanes(BlockingLanes):
    def __init__(self, chunks: list[str] | None = None) -> None:
        super().__init__()
        self.chunks = chunks or ["one ", "two ", "three"]
        self.streamed: list[str] = []
        self.stream_chatted: list[str] = []

    async def stream(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> AsyncIterator[StreamEvent]:
        self.streamed.append(question)
        for chunk in self.chunks:
            yield Delta(text=chunk)
        yield StreamEnd(memory_ids=("aa11bb22",))

    async def stream_chat(self, message: str, session_id: str) -> AsyncIterator[StreamEvent]:
        self.stream_chatted.append(message)
        yield Delta(text="hello back")
        yield StreamEnd()


class Saves:
    async def recent_saves(self, user_id: uuid.UUID, limit: int) -> Any:
        return [_item()], 1


async def _drain(engine: ChatEngine, msg: InboundMessage) -> tuple[list[StreamEvent], str]:
    events = [event async for event in engine.stream(msg)]
    return events, "".join(e.text for e in events if isinstance(e, Delta))


# --- the lanes ---------------------------------------------------------------------------


async def test_a_vault_question_streams_from_the_retrieval_lane() -> None:
    lanes = StreamingLanes()
    events, text = await _drain(
        ChatEngine(lanes, _USER), _msg("show me my cooking videos")
    )
    assert lanes.streamed == ["show me my cooking videos"]
    assert text == "one two three"
    assert isinstance(events[-1], StreamEnd)


async def test_small_talk_streams_from_the_conversation_lane() -> None:
    lanes = StreamingLanes()
    _events, text = await _drain(ChatEngine(lanes, _USER), _msg("Hii"))
    assert lanes.stream_chatted == ["Hii"]
    assert text == "hello back"


async def test_a_status_question_calls_no_model_and_ends_immediately() -> None:
    """Finished the moment it exists: a row read and a sentence."""
    lanes = StreamingLanes()
    events, text = await _drain(
        ChatEngine(lanes, _USER, saves=Saves()), _msg("Is it saved?")
    )
    assert lanes.streamed == [] and lanes.stream_chatted == []
    assert "Pasta" in text
    assert isinstance(events[-1], StreamEnd)


async def test_an_out_of_scope_message_is_declined_before_any_model() -> None:
    lanes = StreamingLanes()
    _events, text = await _drain(
        ChatEngine(lanes, _USER), _msg("write me a python function")
    )
    assert text == scope.DECLINE
    assert lanes.stream_chatted == []


async def test_no_chat_model_ends_with_the_reason() -> None:
    events, _text = await _drain(ChatEngine(None, _USER), _msg("Hii"))
    assert isinstance(events[-1], StreamEnd)
    assert events[-1].error is ErrorKind.chat_unavailable


async def test_a_group_chat_streams_nothing() -> None:
    lanes = StreamingLanes()
    events, text = await _drain(
        ChatEngine(lanes, _USER), _msg("show me my notes", is_private=False)
    )
    assert text == ""
    assert lanes.streamed == []
    assert isinstance(events[0], StreamEnd)


# --- lanes that cannot stream --------------------------------------------------------------


async def test_non_streaming_lanes_still_produce_an_answer() -> None:
    """A surface should not have to know which capability its injected lanes happen to
    have: the reply is the same reply, it simply arrives at once."""
    lanes = BlockingLanes()
    events, text = await _drain(ChatEngine(lanes, _USER), _msg("show me my notes"))
    assert text == "a whole reply"
    assert lanes.answered == ["show me my notes"]
    assert isinstance(events[-1], StreamEnd)


async def test_a_listing_from_non_streaming_lanes_becomes_an_items_event() -> None:
    lanes = BlockingLanes(RecallAnswer(items=[_item()], total=1))
    events, _text = await _drain(ChatEngine(lanes, _USER), _msg("what did I save this week?"))
    assert isinstance(events[0], ItemsEvent)
    assert events[0].total == 1


async def test_a_failure_from_non_streaming_lanes_becomes_an_end_error() -> None:
    lanes = BlockingLanes(RecallAnswer(failed=True))
    events, _text = await _drain(ChatEngine(lanes, _USER), _msg("show me my notes"))
    assert isinstance(events[-1], StreamEnd)
    assert events[-1].error is ErrorKind.provider_failure
