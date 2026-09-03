"""The driver: what reaches the reader, and what a failure does about it.

The graph itself is LangGraph's; what is pinned here is the harness around it. Two of
these are the same properties `tests/ai/test_agent_driver.py` pins for the older driver,
repeated deliberately -- they are the two that cost a person something real when they
break, and a second driver is a second place to break them.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from app.ai.chat.harness import graph
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.budget import Budget
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.chat_engine.toolbox import MemoryToolbox
from tests.ai.fakes import RecordedModel, Turn

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
    def __init__(self, items: Sequence[VaultItem] = ()) -> None:
        self.items = list(items)

    async def list_filtered(self, user_id: uuid.UUID, **kwargs: Any) -> Any:
        return self.items, len(self.items)

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 20, **kw: Any) -> Any:
        return self.items[:limit], len(self.items)


def _retrieving(monkeypatch: pytest.MonkeyPatch, items: Sequence[VaultItem]) -> None:
    """Stub the retriever, not the repository.

    `MemoryRetriever.recall` embeds the question before it queries, so a fake repository
    alone leaves the embedding call live -- and a search that raises inside the graph is
    swallowed as a driver failure, which makes a test that only asserts what is *absent*
    from the reply pass while nothing ever ran. Two of the tests below did exactly that.
    """
    from app.services.chat_engine.evidence import RetrievedMemory

    async def _recall(
        self: MemoryRetriever,
        user_id: uuid.UUID,
        question: str,
        filters: Any = None,
        **kwargs: Any,
    ) -> list[RetrievedMemory]:
        return [RetrievedMemory(item, 0.9) for item in items]

    monkeypatch.setattr(MemoryRetriever, "recall", _recall)


def _install(monkeypatch: pytest.MonkeyPatch, recorded: RecordedModel) -> None:
    """Stand the recorded model in for the real graph builder and the real model."""
    import langgraph.prebuilt

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", recorded)
    monkeypatch.setattr(graph, "get_agent_model", lambda: object())


async def _drain(
    recorded: RecordedModel,
    monkeypatch: pytest.MonkeyPatch,
    *,
    toolbox: MemoryToolbox | None = None,
    budget: Budget | None = None,
) -> tuple[list[graph.AgentEvent], str]:
    _install(monkeypatch, recorded)
    box = toolbox or MemoryToolbox(_USER, FakeRepo([_item()]), budget=budget or _budget())  # type: ignore[arg-type]
    events = [
        event
        async for event in graph.run_agent(
            "what did I save?", [], box, budget=budget or _budget()
        )
    ]
    text = "".join(e.text for e in events if isinstance(e, graph.AgentDelta))
    return events, text


# --- what reaches the reader ----------------------------------------------------------


async def test_prose_is_streamed_as_it_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, text = await _drain(
        RecordedModel([Turn(text="You saved one thing about Redis.")]), monkeypatch
    )

    assert text == "You saved one thing about Redis."
    assert sum(isinstance(e, graph.AgentDelta) for e in events) > 1, "it must arrive in pieces"


async def test_a_tool_result_is_never_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """It carries the fenced memory blocks: quoted material meant for the model.

    Emitting it would put a raw `<memory>` block on the page and, worse, put text the
    model has not read yet in front of the person as though it were the answer.
    """
    _retrieving(monkeypatch, [_item()])
    recorded = RecordedModel(
        [
            Turn(calls=[("SearchMemories", {"query": "redis"})]),
            Turn(text="You saved one thing about Redis."),
        ]
    )

    _events, text = await _drain(recorded, monkeypatch)

    assert text == "You saved one thing about Redis.", "the turn has to have run at all"
    assert "<memory" not in text
    assert "append-only" not in text


async def test_a_tool_call_is_announced_by_name_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The arguments are the model's guess at the subject; a status line must not echo it."""
    _retrieving(monkeypatch, [_item()])
    recorded = RecordedModel(
        [
            Turn(calls=[("SearchMemories", {"query": "my divorce papers"})]),
            Turn(text="Found it."),
        ]
    )

    events, text = await _drain(recorded, monkeypatch)

    assert text == "Found it.", "the turn has to have run at all"

    announced = [e for e in events if isinstance(e, graph.AgentToolCall)]
    assert [e.name for e in announced] == ["SearchMemories"]
    assert all("divorce" not in e.name for e in announced)


# --- how a turn ends ------------------------------------------------------------------


async def test_final_answer_is_parsed_out_of_the_streamed_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its payload *is* its arguments -- there is no other copy of it in the stream."""
    recorded = RecordedModel(
        [
            Turn(
                calls=[
                    (
                        "FinalAnswer",
                        {
                            "text": "You saved the Redis article.",
                            "cited_ids": ["a3f1c920"],
                            "declined_out_of_scope": False,
                            "asked_question": False,
                        },
                    )
                ]
            )
        ]
    )

    events, _text = await _drain(recorded, monkeypatch)

    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.final is not None
    assert end.final.text == "You saved the Redis article."
    assert end.final.cited_ids == ["a3f1c920"]
    assert end.failed is False, "a turn that answered through the tool has not failed"


async def test_the_self_reported_decline_is_carried_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = RecordedModel(
        [
            Turn(
                calls=[
                    (
                        "FinalAnswer",
                        {
                            "text": "I can't help with that here.",
                            "declined_out_of_scope": True,
                        },
                    )
                ]
            )
        ]
    )

    events, _text = await _drain(recorded, monkeypatch)

    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.final is not None
    assert end.final.declined_out_of_scope is True


async def test_plain_prose_is_accepted_as_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper for a model that ignored the instruction to end with FinalAnswer.

    Accepted rather than discarded -- the words are already on their way to the reader --
    and counted as `agent_no_final_tool`, which is how anyone finds out the instruction
    has stopped working.
    """
    events, text = await _drain(
        RecordedModel([Turn(text="You saved one thing.")]), monkeypatch
    )

    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.final is None
    assert end.failed is False
    assert text == "You saved one thing."


async def test_a_half_written_final_answer_is_not_an_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A truncated stream produces JSON that parses into the wrong shape as easily as none."""
    recorded = RecordedModel([Turn(calls=[("FinalAnswer", {"cited_ids": ["a3f1c920"]})])])

    events, _text = await _drain(recorded, monkeypatch)

    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.final is None
    assert end.failed is True, "nothing was said and nothing was answered"


# --- failure, and which side of the first word it lands on ----------------------------


async def test_a_failure_before_any_words_asks_the_caller_to_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events, text = await _drain(
        RecordedModel([Turn(text="never sent")], fail_after=0), monkeypatch
    )

    assert text == ""
    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.failed is True


async def test_a_failure_after_words_is_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeating sentences the reader has already seen is worse than the shorter answer."""
    recorded = RecordedModel(
        [Turn(text="You saved one thing about Redis."), Turn(text="and another")],
        fail_after=1,
    )

    events, text = await _drain(recorded, monkeypatch)

    assert text == "You saved one thing about Redis."
    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.failed is False


async def test_the_step_ceiling_takes_the_same_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GraphRecursionError` is the loop hitting its own ceiling, and from the reader's
    side it is the same event as a provider fault: either words have been shown or they
    have not."""
    from langgraph.errors import GraphRecursionError

    recorded = RecordedModel([Turn(text="x")], fail_after=0, error=GraphRecursionError)

    events, text = await _drain(recorded, monkeypatch)

    assert text == ""
    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.failed is True
    assert end.exhausted is True


async def test_an_unbuildable_graph_fails_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider with no tool support is a silently-off lane, not a user-visible error."""

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("this provider cannot bind tools")

    import langgraph.prebuilt

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", _explode)
    monkeypatch.setattr(graph, "get_agent_model", lambda: object())

    events = [
        event
        async for event in graph.run_agent(
            "hi", [], MemoryToolbox(_USER, FakeRepo()), budget=_budget()  # type: ignore[arg-type]
        )
    ]

    assert events == [graph.AgentEnd(failed=True)]


async def test_the_wall_clock_ends_a_turn_that_will_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typing indicator is bounded and a person's patience is bounded."""
    import asyncio

    class _Slow:
        def __call__(self, model: Any, tools: Any) -> _Slow:
            return self

        async def astream(self, *args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(5)
            yield None, {}

    import langgraph.prebuilt

    monkeypatch.setattr(langgraph.prebuilt, "create_react_agent", _Slow())
    monkeypatch.setattr(graph, "get_agent_model", lambda: object())

    events = [
        event
        async for event in graph.run_agent(
            "hi",
            [],
            MemoryToolbox(_USER, FakeRepo()),  # type: ignore[arg-type]
            budget=_budget(wall_clock_seconds=0.05),
        )
    ]

    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.failed is True
    assert end.exhausted is True


# --- the tenant boundary, restated for the new binder ---------------------------------


def test_no_bound_tool_lets_the_model_name_an_owner() -> None:
    """`user_id` is fixed on the toolbox. A tool that took it would be one to aim."""
    from app.ai.chat.harness.tools import build_tools

    box = MemoryToolbox(_USER, FakeRepo())  # type: ignore[arg-type]

    for tool in build_tools(box):
        fields = set(tool.args_schema.model_json_schema()["properties"])
        assert not fields & {"user_id", "tenant", "owner", "account", "account_id"}


async def test_rounds_are_counted_from_the_graphs_own_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A round is a model turn or a tool batch -- which is exactly a LangGraph step.

    Counting chunks would count fragments of a sentence; counting tool calls would miss a
    turn that made two. It is logged on every `agent_turn`, so a wrong number here is a
    wrong number in the only record anyone reads afterwards.
    """
    _retrieving(monkeypatch, [_item()])
    recorded = RecordedModel(
        [
            Turn(calls=[("SearchMemories", {"query": "redis"})]),
            Turn(text="You saved one thing."),
        ]
    )

    events, text = await _drain(recorded, monkeypatch)

    assert text == "You saved one thing."
    end = events[-1]
    assert isinstance(end, graph.AgentEnd)
    assert end.rounds >= 2, "a search and an answer are two steps, not one"
