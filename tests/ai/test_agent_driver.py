"""The graph driver: the tool lane, streamed.

It exists for one reason -- a loop built on `ainvoke` has to receive a whole turn before
it knows whether that turn was tool calls or the answer, so the surface that most needs
the words as they arrive was the one surface that could not have the tools. Everything
that makes the lane safe lives in the executor, not the driver, so what is pinned here is
delivery: tool results never reach the reader, a status line never carries the model's
own guess, and a failure before any words is silent so the caller can answer another way.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import langgraph.prebuilt as prebuilt
import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from app.ai.chat import agent


class FakeGraph:
    """Replays a script of chunks the way `stream_mode="messages"` would."""

    def __init__(self, chunks: Sequence[Any], *, boom: Exception | None = None) -> None:
        self.chunks = list(chunks)
        self.boom = boom
        self.configs: list[Any] = []

    async def astream(
        self, _input: Any, *, stream_mode: str = "", config: Any = None
    ) -> AsyncIterator[tuple[Any, dict[str, Any]]]:
        self.configs.append(config)
        for chunk in self.chunks:
            yield chunk, {}
        if self.boom is not None:
            raise self.boom


class FakeExecutor:
    def __init__(self) -> None:
        self.searched: list[tuple[str, Any]] = []
        self.listed: list[Any] = []
        self.opened: list[str] = []

    async def search_memories(
        self, query: str, days: Any = None, content_types: Any = (), category: Any = None
    ) -> str:
        self.searched.append((query, days))
        return "<memory id=\"aa11\" title=\"x\">card</memory>"

    async def list_memories(
        self, days: Any = None, content_types: Any = (), category: Any = None
    ) -> str:
        self.listed.append(days)
        return "<memory id=\"aa11\" title=\"x\">card</memory>"

    async def get_memory(self, memory_id: str) -> str:
        self.opened.append(memory_id)
        return "<memory id=\"aa11\" title=\"x\">body</memory>"


def _graph(monkeypatch: pytest.MonkeyPatch, graph: FakeGraph) -> FakeGraph:
    monkeypatch.setattr(prebuilt, "create_react_agent", lambda *a, **k: graph)
    monkeypatch.setattr(agent, "get_chat_model", lambda: object())
    return graph


def _tool_chunk(name: str) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": name, "args": "{}", "id": "c1", "index": 0}],
    )


async def _drain(executor: Any, **kwargs: Any) -> list[Any]:
    return [e async for e in agent.stream_with_tools("q", [], executor, **kwargs)]


# --- delivery -------------------------------------------------------------------------


async def test_the_answer_arrives_as_it_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _graph(
        monkeypatch,
        FakeGraph([AIMessageChunk(content=w) for w in ["You ", "saved ", "two."]]),
    )
    events = await _drain(FakeExecutor())
    assert [e.text for e in events if isinstance(e, agent.AgentDelta)] == [
        "You ",
        "saved ",
        "two.",
    ]
    assert isinstance(events[-1], agent.AgentEnd) and not events[-1].failed


async def test_a_tool_call_becomes_a_status_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _graph(
        monkeypatch,
        FakeGraph([_tool_chunk("SearchMemories"), AIMessageChunk(content="Found it.")]),
    )
    events = await _drain(FakeExecutor())
    calls = [e for e in events if isinstance(e, agent.AgentToolCall)]
    assert [c.name for c in calls] == ["SearchMemories"]


async def test_a_status_event_carries_no_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the model decided to search for is its own output derived from the person's
    question; echoing it back as a status line shows them a guess as though it were a
    fact."""
    assert set(agent.AgentToolCall.__dataclass_fields__) == {"name"}


async def test_tool_results_never_reach_the_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ToolMessage carries the fenced memory blocks -- quoted material meant for the
    model. Emitting it would put a raw <memory> block on the page, and put text the model
    has not read yet in front of the person as though it were the answer."""
    _graph(
        monkeypatch,
        FakeGraph(
            [
                _tool_chunk("SearchMemories"),
                ToolMessage(content="<memory id=\"aa11\">secret card</memory>", tool_call_id="c1"),
                AIMessageChunk(content="One memory."),
            ]
        ),
    )
    events = await _drain(FakeExecutor())
    text = "".join(e.text for e in events if isinstance(e, agent.AgentDelta))
    assert text == "One memory."
    assert "<memory" not in text


# --- failure ----------------------------------------------------------------------------


async def test_an_unavailable_graph_fails_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silence, not an error event: nothing has been shown, so the caller can still
    answer the question by the other route."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise NotImplementedError("no tool support")

    monkeypatch.setattr(prebuilt, "create_react_agent", _boom)
    monkeypatch.setattr(agent, "get_chat_model", lambda: object())

    events = await _drain(FakeExecutor())
    assert events == [agent.AgentEnd(failed=True)]


async def test_dying_before_any_words_asks_for_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _graph(monkeypatch, FakeGraph([], boom=TimeoutError("gone")))
    events = await _drain(FakeExecutor())
    assert events[-1].failed


async def test_dying_after_words_does_not_ask_for_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeating words the reader has already seen is worse than the shorter answer they
    already have."""
    _graph(
        monkeypatch,
        FakeGraph([AIMessageChunk(content="You saved")], boom=TimeoutError("gone")),
    )
    events = await _drain(FakeExecutor())
    assert not events[-1].failed


async def test_the_step_limit_is_passed_to_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent loop with no ceiling is an unbounded bill reachable through a text box."""
    graph = _graph(monkeypatch, FakeGraph([AIMessageChunk(content="hi")]))
    await _drain(FakeExecutor(), max_rounds=2)
    assert graph.configs[0]["recursion_limit"] == 5


# --- the tools the graph is given ----------------------------------------------------------


def test_only_the_three_read_tools_are_built() -> None:
    tools = agent._build_tools(FakeExecutor())
    assert {t.name for t in tools} == {"SearchMemories", "ListMemories", "GetMemory"}


def test_no_tool_takes_a_user() -> None:
    """Closing over the executor is what keeps `user_id` out of every schema: a tool that
    took the user as an argument would be a tool a prompt injection could aim."""
    for tool in agent._build_tools(FakeExecutor()):
        schema = tool.args_schema
        assert not {"user_id", "user", "owner", "account_id"} & set(schema.model_fields)


async def test_the_tools_reach_the_executor() -> None:
    executor = FakeExecutor()
    by_name = {t.name: t for t in agent._build_tools(executor)}

    await by_name["SearchMemories"].ainvoke({"query": "redis", "days": 7})
    await by_name["GetMemory"].ainvoke({"memory_id": "aa11"})

    assert executor.searched == [("redis", 7)]
    assert executor.opened == ["aa11"]
