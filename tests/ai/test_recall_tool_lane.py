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
        context: str = "",
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


# --- the snapshot, and what it changed about "nothing found" --------------------------


class _SnapshotRepo:
    """Just enough repository for the snapshot read the lane now makes."""

    def __init__(self, items: list[VaultItem], total: int = 47) -> None:
        self.items = items
        self.total = total

    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        return self.items[:limit], self.total


async def test_a_turn_that_called_no_tool_keeps_its_answer(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression the vault snapshot would otherwise have caused.

    "What were my last three?" is answered from the snapshot with no tool call at all --
    that is the whole point of putting it in the prompt. The lane used to treat "no
    memory was surfaced by a tool" as "nothing was found" and replace the reply with the
    fixed no-match sentence, so the better answer was thrown away and the person was told
    their vault held nothing about memories they were looking straight at.
    """
    _model_that(monkeypatch, "Your last three were the Redis article and two reels.", search=False)

    result = await RecallChatService(  # type: ignore[arg-type]
        repo=_SnapshotRepo([_item()])
    ).answer(_USER, "what were my last three?", "555")

    assert result.text == "Your last three were the Redis article and two reels."


async def test_a_search_that_found_nothing_still_gets_the_fixed_sentence(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same distinction, which is why it is a counter and not a flag.

    Having gone looking and come back empty is exactly the state in which a model fills
    the gap from its own knowledge, so that answer is still replaced.
    """
    _retrieving(monkeypatch, [])
    _model_that(monkeypatch, "I think you saved something about Redis clustering.")

    result = await RecallChatService(  # type: ignore[arg-type]
        repo=_SnapshotRepo([_item()])
    ).answer(_USER, "redis clustering?", "555")

    assert result.text is not None
    assert "Redis clustering" not in result.text


async def test_the_snapshot_and_the_card_reach_the_tool_prompt(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wiring, which is the half that silently does not happen.

    The block can be perfect and the lane can still never pass it -- that is the shape of
    every bug this file's sibling `test_telegram_typing.py` was written for.
    """
    captured: dict[str, str] = {}

    async def _loop(
        question: str,
        past: Sequence[BaseMessage],
        executor: Any,
        *,
        max_calls: int = 4,
        max_rounds: int = 3,
        context: str = "",
    ) -> tools.ToolAnswer:
        captured["context"] = context
        return tools.ToolAnswer(text="ok", calls=[], rounds=1)

    monkeypatch.setattr(tools, "answer_with_tools", _loop)

    item = _item()
    await RecallChatService(repo=_SnapshotRepo([item])).answer(  # type: ignore[arg-type]
        _USER, "what did I save?", "555"
    )

    assert "<capability_card>" in captured["context"]
    assert "<vault_snapshot" in captured["context"]
    assert short_id(item) in captured["context"]
    assert 'total="47"' in captured["context"]


async def test_an_unreadable_vault_costs_context_and_not_the_answer(
    _lane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot read that fails must not take the turn down with it.

    The lanes underneath do their own reads and fail honestly if those fail; this one is
    an aid to the prompt, and answering with less context beats answering with an error.
    """

    class _BrokenRepo:
        async def list_for_user(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("the database is unreachable")

    _model_that(monkeypatch, "Here is what I found.", search=False)

    result = await RecallChatService(repo=_BrokenRepo()).answer(  # type: ignore[arg-type]
        _USER, "what did I save?", "555"
    )

    assert result.text == "Here is what I found."
    assert result.failed is False
