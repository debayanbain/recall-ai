"""The streaming lane, end to end: it arrives sooner, and it is checked before it arrives.

The engine passes lane events straight through to the surface, so the validation has to
happen in the lane -- it is the only layer that knows which memories were retrieved. That
is the property under test here: a fabricated URL or citation coming out of the model is
never in an event, not merely corrected in one afterwards.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import BaseMessage

from app.ai.chat import chain, history
from app.ai.chat.planner import MemoryQuery
from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id
from app.services.chat_engine.evidence import RetrievedMemory
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.chat_engine.types import (
    Delta,
    ErrorKind,
    ItemsEvent,
    StatusEvent,
    StreamEnd,
)
from app.services.recall_chat import RecallChatService

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_STRONG = 0.9


def _item() -> VaultItem:
    return VaultItem(
        user_id=_USER,
        type=ContentType.article,
        title="Redis persistence",
        summary="RDB snapshots versus the append-only file.",
        source_url="https://example.com/redis",
        content="The append-only file is rewritten when it doubles in size.",
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


@pytest.fixture
def _offline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"stored": []}

    async def _plan(question: str) -> MemoryQuery:
        return MemoryQuery(search_text=seen.get("subject", "redis"))

    async def _load(session_id: str) -> list[BaseMessage]:
        return []

    async def _append(session_id: str, q: str, a: str) -> None:
        seen["stored"].append(a)

    # These tests are about the single-shot streamed lane; the graph driver has its own
    # section below and would otherwise answer every case here.
    monkeypatch.setattr(settings, "RECALL_AGENT_ENABLED", False)
    monkeypatch.setattr("app.services.recall_chat.planner.plan", _plan)
    monkeypatch.setattr(history, "load", _load)
    monkeypatch.setattr(history, "append", _append)
    return seen


def _retrieving(
    monkeypatch: pytest.MonkeyPatch, items: list[VaultItem], score: float = _STRONG
) -> None:
    async def _recall(
        self: MemoryRetriever,
        user_id: uuid.UUID,
        question: str,
        filters: Any = None,
        *,
        limit: int = 8,
    ) -> list[RetrievedMemory]:
        return [RetrievedMemory(item, score) for item in items[:limit]]

    monkeypatch.setattr(MemoryRetriever, "recall", _recall)


def _saying(monkeypatch: pytest.MonkeyPatch, chunks: list[str] | Exception) -> None:
    async def _stream(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        if isinstance(chunks, Exception):
            raise chunks
        for chunk in chunks:
            yield chunk

    monkeypatch.setattr(chain, "answer_stream", _stream)


async def _collect(service: RecallChatService, question: str = "what about redis?") -> Any:
    events = [event async for event in service.stream(_USER, question, "web:1")]
    text = "".join(e.text for e in events if isinstance(e, Delta))
    return events, text


# --- checked before it is displayed ------------------------------------------------------


async def test_a_fabricated_url_is_never_in_an_event(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not "corrected afterwards": the whole point of the URL rule is that a fabricated
    link is one a person is invited to tap."""
    _retrieving(monkeypatch, [_item()])
    _saying(monkeypatch, ["Read it ", "at ", "https://evil", ".test/x", " now."])

    events, text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert "evil.test" not in text
    assert all("evil.test" not in getattr(e, "text", "") for e in events)


async def test_a_citation_of_a_memory_never_retrieved_is_never_in_an_event(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _retrieving(monkeypatch, [_item()])
    _saying(monkeypatch, ["See ", "[dead", "beef]", " for it."])

    _events, text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert "deadbeef" not in text


async def test_a_real_source_url_survives(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _item()
    _retrieving(monkeypatch, [item])
    _saying(monkeypatch, ["It is at ", "https://example.com/redis", " ."])

    _events, text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert "https://example.com/redis" in text


# --- the shape of a stream ------------------------------------------------------------------


async def test_the_answer_arrives_in_pieces(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entire reason for the endpoint: the reader sees words before the model has
    finished writing them."""
    _retrieving(monkeypatch, [_item()])
    _saying(monkeypatch, ["You ", "saved ", "one ", "thing ", "about ", "Redis."])

    events, text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert len([e for e in events if isinstance(e, Delta)]) > 1
    assert text.strip() == "You saved one thing about Redis."


async def test_it_always_ends_with_an_end_event(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _item()
    _retrieving(monkeypatch, [item])
    _saying(monkeypatch, ["ok"])

    events, _text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert isinstance(events[-1], StreamEnd)
    assert events[-1].memory_ids == (short_id(item),)


async def test_a_provider_dying_mid_stream_still_ends(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that simply stops leaves the reader watching a cursor that will never
    move."""
    _retrieving(monkeypatch, [_item()])
    _saying(monkeypatch, TimeoutError("provider gone"))

    events, _text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert isinstance(events[-1], StreamEnd)
    assert events[-1].error is ErrorKind.provider_failure


async def test_nothing_relevant_streams_the_fixed_sentence_and_calls_no_model(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _retrieving(monkeypatch, [_item()], score=0.001)

    async def _never(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        raise AssertionError("no model call is allowed with no evidence")
        yield ""  # pragma: no cover

    monkeypatch.setattr(chain, "answer_stream", _never)

    _events, text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert "couldn't find anything" in text


async def test_a_time_only_question_streams_rows_not_prose(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """No subject to rank against, so no embedding and no answer model -- the reply is a
    listing, and a listing is finished the moment it exists."""
    _offline["subject"] = ""
    items = [_item()]

    class _Repo:
        async def list_filtered(self, *a: Any, **k: Any) -> Any:
            return items, 1

    service = RecallChatService(repo=_Repo())  # type: ignore[arg-type]
    events = [e async for e in service.stream(_USER, "what did I save this week?", "web:1")]

    assert isinstance(events[0], ItemsEvent)
    assert events[0].total == 1


# --- history ------------------------------------------------------------------------------------


async def test_the_checked_text_is_what_reaches_history(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """History is replayed into the next prompt, so storing the raw stream would let a
    stripped fabrication come back as context and be built on."""
    _retrieving(monkeypatch, [_item()])
    _saying(monkeypatch, ["Saved it. ", "[deadbeef]"])

    await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert _offline["stored"] == ["Saved it."]


async def test_a_rejected_stream_is_not_stored(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _retrieving(monkeypatch, [_item()])
    _saying(monkeypatch, ["   "])

    events, _text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert events[-1].error is ErrorKind.provider_failure
    assert _offline["stored"] == []


# --- the graph driver, and falling back off it ---------------------------------------------


def _agent_saying(monkeypatch: pytest.MonkeyPatch, events: list[Any]) -> None:
    from app.ai.chat import agent

    async def _stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for event in events:
            yield event

    monkeypatch.setattr(agent, "stream_with_tools", _stream)
    monkeypatch.setattr(settings, "RECALL_TOOLS_ENABLED", True)
    monkeypatch.setattr(settings, "RECALL_AGENT_ENABLED", True)


async def test_a_tool_call_reaches_the_surface_as_a_status_event(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retrieval is a database round trip and a provider call; a page showing nothing
    during it reads as a hang rather than as work."""
    from app.ai.chat import agent

    _retrieving(monkeypatch, [_item()])
    _agent_saying(
        monkeypatch,
        [
            agent.AgentToolCall(name="SearchMemories"),
            agent.AgentDelta(text="You saved "),
            agent.AgentDelta(text="one thing."),
            agent.AgentEnd(),
        ],
    )

    events = [e async for e in RecallChatService(repo=None).stream(_USER, "redis?", "web:1")]  # type: ignore[arg-type]

    statuses = [e for e in events if isinstance(e, StatusEvent)]
    assert [s.stage for s in statuses] == ["searching your memories"]
    assert "SearchMemories" not in str(statuses)


async def test_the_agents_output_is_validated_too(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The driver changed; the output rules did not."""
    from app.ai.chat import agent

    _retrieving(monkeypatch, [_item()])
    _agent_saying(
        monkeypatch,
        [
            agent.AgentDelta(text="Read it at "),
            agent.AgentDelta(text="https://evil.test/x"),
            agent.AgentDelta(text=" now."),
            agent.AgentEnd(),
        ],
    )

    _events, text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert "evil.test" not in text


async def test_an_unavailable_agent_falls_back_to_the_single_shot_stream(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was said, so nothing has been shown -- the question can still be answered
    the other way."""
    from app.ai.chat import agent

    _retrieving(monkeypatch, [_item()])
    _agent_saying(monkeypatch, [agent.AgentEnd(failed=True)])
    _saying(monkeypatch, ["single ", "shot ", "answer"])

    _events, text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert text.strip() == "single shot answer"


async def test_the_setting_switches_the_driver_off(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.ai.chat import agent

    async def _never(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        raise AssertionError("the graph must not run when disabled")
        yield  # pragma: no cover

    _retrieving(monkeypatch, [_item()])
    monkeypatch.setattr(settings, "RECALL_AGENT_ENABLED", False)
    monkeypatch.setattr(agent, "stream_with_tools", _never)
    _saying(monkeypatch, ["single shot"])

    _events, text = await _collect(RecallChatService(repo=None))  # type: ignore[arg-type]

    assert text.strip() == "single shot"
