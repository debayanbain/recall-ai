"""Running out, without breaking the conversation.

The interesting part of a budget is not the arithmetic, it is what a spent one *does*. A
tool call left without its `ToolMessage` is a malformed exchange and providers reject the
next request outright, so the ceiling cannot be enforced by refusing to answer a call --
it has to be enforced by answering it differently. These tests pin that, and the two
counters whose difference is easy to erase later.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.budget import SPENT, Budget
from app.services.chat_engine.toolbox import MemoryToolbox

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _budget(**kwargs: Any) -> Budget:
    defaults: dict[str, Any] = {
        "max_calls": 6,
        "max_rounds": 4,
        "wall_clock_seconds": 20.0,
        "max_cards": 12,
    }
    return Budget(**{**defaults, **kwargs})


def _item(title: str = "Redis persistence") -> VaultItem:
    return VaultItem(
        id=uuid.uuid4(),
        user_id=_USER,
        type=ContentType.article,
        title=title,
        summary="RDB snapshots versus the append-only file.",
        source_url="https://example.com/redis",
        content="The append-only file is rewritten when it doubles in size.",
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


class FakeRepo:
    """Enough repository for a listing. Records nothing but the calls it was asked for."""

    def __init__(self, items: Sequence[VaultItem]) -> None:
        self.items = list(items)
        self.calls = 0

    async def list_filtered(self, user_id: uuid.UUID, **kwargs: Any) -> Any:
        self.calls += 1
        return self.items, len(self.items)

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 20, **kw: Any) -> Any:
        return self.items[:limit], len(self.items)


# --- the arithmetic -------------------------------------------------------------------


def test_calls_are_counted_even_when_refused() -> None:
    """Otherwise a model that keeps calling past the ceiling does so forever.

    A refused call still cost a model round to produce. Not counting it would make the
    refusal free, and free is exactly what an unbounded loop is made of.
    """
    budget = _budget(max_calls=2)

    assert [budget.spend_call() for _ in range(4)] == [True, True, False, False]
    assert budget.calls_used == 4
    assert budget.exhausted


def test_the_clock_ends_a_turn_that_still_has_calls_left() -> None:
    budget = _budget(max_calls=99, wall_clock_seconds=0.0)

    assert budget.spend_call() is False
    assert budget.out_of_time()


def test_cards_are_taken_until_there_is_no_room() -> None:
    budget = _budget(max_cards=5)

    assert budget.take_cards(3) == 3
    assert budget.take_cards(4) == 2
    assert budget.take_cards(1) == 0


# --- what a spent budget does to a tool -----------------------------------------------


async def test_a_spent_budget_answers_the_call_instead_of_refusing_it() -> None:
    """Seven calls, six executed, and the seventh still gets a result.

    The result is a sentence rather than a search, which is the whole mechanism: the
    conversation stays well-formed, and the model is told which of "you may not search
    again" and "the vault is empty" it is looking at.
    """
    repo = FakeRepo([_item()])
    toolbox = MemoryToolbox(_USER, repo, budget=_budget(max_calls=6))  # type: ignore[arg-type]

    results = [await toolbox.list_memories() for _ in range(7)]

    assert repo.calls == 6, "the seventh must not have reached the database"
    assert results[-1] == SPENT
    assert all(result != SPENT for result in results[:6])


async def test_the_spent_sentence_distinguishes_no_budget_from_no_memories() -> None:
    """A model that cannot tell them apart reports an empty vault when it has one."""
    toolbox = MemoryToolbox(_USER, FakeRepo([]), budget=_budget(max_calls=0))  # type: ignore[arg-type]

    assert "budget" in (await toolbox.list_memories()).lower()


async def test_every_tool_respects_the_same_allowance() -> None:
    """A ceiling one tool ignores is not a ceiling."""
    toolbox = MemoryToolbox(_USER, FakeRepo([_item()]), budget=_budget(max_calls=0))  # type: ignore[arg-type]

    assert await toolbox.list_memories() == SPENT
    assert await toolbox.search_memories("redis") == SPENT
    assert await toolbox.get_memory("deadbeef") == SPENT
    assert await toolbox.get_capture_status() == SPENT


async def test_asking_a_question_is_not_charged_against_searching() -> None:
    """Asking is how a turn ends. A model out of searches must not be out of ways to ask."""
    toolbox = MemoryToolbox(_USER, FakeRepo([]), budget=_budget(max_calls=0))  # type: ignore[arg-type]

    result = await toolbox.ask_user(
        "Which one did you mean?", ["the docker talk", "the reel"]
    )

    assert result != SPENT
    assert toolbox.question is not None
    assert toolbox.question.question == "Which one did you mean?"


# --- the card ceiling -----------------------------------------------------------------


async def test_a_listing_is_truncated_and_says_so() -> None:
    """A silently short list is one the person cannot even notice is short."""
    repo = FakeRepo([_item(f"memory {n}") for n in range(8)])
    toolbox = MemoryToolbox(_USER, repo, budget=_budget(max_cards=3))  # type: ignore[arg-type]

    rendered = await toolbox.list_memories()

    assert rendered.count("<memory ") == 3
    assert "and 5 more" in rendered
    assert len(toolbox.surfaced) == 3, "an unrendered card was never shown to the model"


async def test_an_unbounded_toolbox_is_unchanged() -> None:
    """The older lane passes no budget and must keep the behaviour it always had."""
    repo = FakeRepo([_item(f"memory {n}") for n in range(8)])
    toolbox = MemoryToolbox(_USER, repo)  # type: ignore[arg-type]

    rendered = await toolbox.list_memories()

    assert "and " not in rendered.split("<memory")[0]
    assert rendered.count("<memory ") == 8


# --- the whole turn, when the allowance runs out mid-way -------------------------------


async def test_a_turn_that_runs_out_still_ends_with_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outcome the budget exists to produce, end to end.

    Seven calls against an allowance of six: the seventh is answered with the spent
    sentence instead of a search, the model gets its round anyway, and the person gets a
    reply. A turn that ends with a half-finished plan and no sentence is the one failure
    worth engineering against -- somebody asked something and got nothing.
    """
    from app.ai.chat.harness import graph
    from tests.ai.fakes import RecordedModel, Turn

    repo = FakeRepo([_item()])
    toolbox = MemoryToolbox(_USER, repo, budget=_budget(max_calls=6))  # type: ignore[arg-type]
    recorded = RecordedModel(
        [
            Turn(calls=[("ListMemories", {}) for _ in range(7)]),
            Turn(text="Here are the ones I could get to."),
        ]
    )

    import langgraph.prebuilt

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", recorded)
    monkeypatch.setattr(graph, "get_agent_model", lambda: object())

    events = [
        event
        async for event in graph.run_agent(
            "list everything", [], toolbox, budget=toolbox.budget or _budget()
        )
    ]
    text = "".join(e.text for e in events if isinstance(e, graph.AgentDelta))

    assert repo.calls == 6, "the seventh call must not have reached the database"
    assert text == "Here are the ones I could get to."
    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.failed is False
