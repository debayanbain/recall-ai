"""The tool lane end to end: same guarantees as the single-shot lane, more capability.

The point of these tests is that letting the model choose the searches did not quietly
relax anything. A turn that surfaced no memory still gets the fixed sentence with no
model wording in it; a citation of something never retrieved is still stripped; history
still stores the checked text; and a lane that fails still hands the question to the
older path rather than to the user as an error.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import BaseMessage

from app.ai.chat import chain, history, tools
from app.ai.chat.planner import MemoryQuery
from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id
from app.services.chat_engine.evidence import RetrievedMemory
from app.services.chat_engine.retrieval import MemoryRetriever
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
def _lane(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Tool lane on, history stubbed, nothing reaching a provider."""
    seen: dict[str, Any] = {"stored": [], "planned": 0}

    async def _load(session_id: str) -> list[BaseMessage]:
        return []

    async def _append(session_id: str, q: str, a: str) -> None:
        seen["stored"].append(a)

    async def _plan(question: str) -> MemoryQuery:
        seen["planned"] += 1
        return MemoryQuery(search_text="redis")

    async def _answer(*args: Any, **kwargs: Any) -> str:
        return "single-shot answer"

    monkeypatch.setattr(settings, "RECALL_TOOLS_ENABLED", True)
    monkeypatch.setattr(history, "load", _load)
    monkeypatch.setattr(history, "append", _append)
    monkeypatch.setattr("app.services.recall_chat.planner.plan", _plan)
    monkeypatch.setattr(chain, "answer", _answer)
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


def _model_that(monkeypatch: pytest.MonkeyPatch, reply: str, *, search: bool = True) -> None:
    """Stand in for the loop: optionally run one real search, then say `reply`."""

    async def _loop(
        question: str,
        past: Sequence[BaseMessage],
        executor: Any,
        *,
        max_calls: int = 4,
        max_rounds: int = 3,
    ) -> tools.ToolAnswer:
        if search:
            await executor.search_memories("redis")
        return tools.ToolAnswer(text=reply, calls=["SearchMemories"], rounds=2)

    monkeypatch.setattr(tools, "answer_with_tools", _loop)


# --- the ordinary path -----------------------------------------------------------------


async def test_the_tool_lane_answers_and_cites_what_it_retrieved(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    item = _item()
    _retrieving(monkeypatch, [item])
    _model_that(monkeypatch, "You saved one thing about Redis persistence.")

    result = await RecallChatService(repo=None).answer(  # type: ignore[arg-type]
        _USER, "what about redis?", "555"
    )

    assert result.text == "You saved one thing about Redis persistence."
    assert result.memory_ids == (short_id(item),)
    assert _lane["planned"] == 0  # the single-shot planner was never reached


async def test_the_checked_answer_is_what_reaches_history(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """History is replayed into the next prompt, so storing a raw reply would let a
    fabrication come back as context and be built on."""
    _retrieving(monkeypatch, [_item()])
    _model_that(monkeypatch, "Saved it. [deadbeef]")

    await RecallChatService(repo=None).answer(_USER, "redis?", "555")  # type: ignore[arg-type]

    assert _lane["stored"] == ["Saved it."]


async def test_a_citation_of_a_memory_never_retrieved_is_stripped(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clearest fabrication signal the system has, and the tool lane is checked for
    it exactly like the single-shot lane is -- the model chose the query, not the rules."""
    _retrieving(monkeypatch, [_item()])
    _model_that(monkeypatch, "Also see [ffffffff].")

    result = await RecallChatService(repo=None).answer(_USER, "redis?", "555")  # type: ignore[arg-type]

    assert result.text is not None
    assert "ffffffff" not in result.text


async def test_a_url_that_appears_in_no_block_is_replaced(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _retrieving(monkeypatch, [_item()])
    _model_that(monkeypatch, "Read it at https://evil.example/phish")

    result = await RecallChatService(repo=None).answer(_USER, "redis?", "555")  # type: ignore[arg-type]

    assert result.text is not None
    assert "evil.example" not in result.text


# --- nothing found ----------------------------------------------------------------------


async def test_a_turn_that_surfaced_nothing_gets_the_fixed_sentence(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not the model's phrasing of "nothing found". An answer with no evidence behind it
    is the exact input a model fills in from its own knowledge."""
    _retrieving(monkeypatch, [_item()], score=0.001)  # below the floor
    _model_that(monkeypatch, "I think you saved something about Redis clustering.")

    result = await RecallChatService(repo=None).answer(  # type: ignore[arg-type]
        _USER, "anything on redis?", "555"
    )

    assert result.text is not None
    assert "couldn't find anything" in result.text
    assert "clustering" not in result.text
    assert _lane["stored"] == []


async def test_the_fixed_sentence_echoes_the_models_own_search_term(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _retrieving(monkeypatch, [_item()], score=0.001)
    _model_that(monkeypatch, "nothing")

    result = await RecallChatService(repo=None).answer(_USER, "anything on redis?", "555")  # type: ignore[arg-type]

    assert result.text is not None and "redis" in result.text


# --- failure hands the question over ---------------------------------------------------------


async def test_a_failed_tool_loop_falls_back_to_the_single_shot_lane(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback is the whole reason this can be on by default: the older path is
    better tested, and a user gets an answer rather than an error."""
    _retrieving(monkeypatch, [_item()])

    async def _unavailable(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tools, "answer_with_tools", _unavailable)

    result = await RecallChatService(repo=None).answer(_USER, "redis?", "555")  # type: ignore[arg-type]

    assert result.text == "single-shot answer"
    assert _lane["planned"] == 1


async def test_the_setting_switches_the_lane_off(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _retrieving(monkeypatch, [_item()])

    async def _never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the tool lane must not run when disabled")

    monkeypatch.setattr(settings, "RECALL_TOOLS_ENABLED", False)
    monkeypatch.setattr(tools, "answer_with_tools", _never)

    result = await RecallChatService(repo=None).answer(_USER, "redis?", "555")  # type: ignore[arg-type]

    assert result.text == "single-shot answer"


async def test_an_empty_reply_is_a_failure_rather_than_a_blank_message(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _retrieving(monkeypatch, [_item()])
    _model_that(monkeypatch, "   ")

    result = await RecallChatService(repo=None).answer(_USER, "redis?", "555")  # type: ignore[arg-type]

    assert result.failed
