"""The loop that lets the model run its own searches, and everything that bounds it.

The value of this lane is one thing -- a question that needs *two* lookups ("did I save
the docker talk, and what did the speaker claim?") is find-then-read, and one planned
search cannot express it. The risk is the other thing: a model choosing what happens next
inside a context that contains scraped text somebody else wrote. So the tests here are
mostly about ceilings and failure, not capability.

No provider is reachable: `tests/conftest.py` makes `get_chat_model` raise, and each test
that wants a model scripts one over the top.
"""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.ai.chat import tools


class ScriptedModel:
    """A chat model that says exactly what a test tells it to, in order."""

    def __init__(self, turns: list[AIMessage]) -> None:
        self.turns = list(turns)
        self.bound: list[Any] | None = None
        self.calls: list[list[BaseMessage]] = []

    def bind_tools(self, tool_list: list[Any]) -> ScriptedModel:
        self.bound = tool_list
        return self

    async def ainvoke(self, messages: Any, config: Any = None) -> AIMessage:
        self.calls.append(list(messages))
        if not self.turns:
            return AIMessage(content="done")
        return self.turns.pop(0)


class FakeExecutor:
    """Records what the model asked for; returns fixed, obviously-fake blocks."""

    def __init__(self, result: str = "<memory id=\"aa11\" title=\"x\">card</memory>") -> None:
        self.result = result
        self.searched: list[tuple[str, int | None]] = []
        self.listed: list[int | None] = []
        self.opened: list[str] = []

    async def search_memories(
        self, query: str, days: Any = None, content_types: Any = (), category: Any = None
    ) -> str:
        self.searched.append((query, days))
        return self.result

    async def list_memories(
        self, days: Any = None, content_types: Any = (), category: Any = None
    ) -> str:
        self.listed.append(days)
        return self.result

    async def get_memory(self, memory_id: str) -> str:
        self.opened.append(memory_id)
        return self.result


def _call(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _scripted(monkeypatch: pytest.MonkeyPatch, turns: list[AIMessage]) -> ScriptedModel:
    model = ScriptedModel(turns)
    monkeypatch.setattr(tools, "get_chat_model", lambda: model)
    return model


# --- the ordinary path ------------------------------------------------------------------


async def test_the_model_searches_then_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(
        monkeypatch,
        [
            AIMessage(content="", tool_calls=[_call("SearchMemories", {"query": "redis"})]),
            AIMessage(content="You saved two things about Redis."),
        ],
    )
    executor = FakeExecutor()

    result = await tools.answer_with_tools("what about redis?", [], executor)

    assert result is not None
    assert executor.searched == [("redis", None)]
    assert result.text == "You saved two things about Redis."
    assert result.calls == ["SearchMemories"]


async def test_the_model_can_search_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reason for this lane: the first guess at what to search for is allowed
    to be wrong, which one planner call and one fixed search can never recover from."""
    _scripted(
        monkeypatch,
        [
            AIMessage(content="", tool_calls=[_call("SearchMemories", {"query": "docker"})]),
            AIMessage(
                content="", tool_calls=[_call("SearchMemories", {"query": "containers"}, "c2")]
            ),
            AIMessage(content="Found it."),
        ],
    )
    executor = FakeExecutor()

    result = await tools.answer_with_tools("the docker talk?", [], executor)

    assert result is not None
    assert [q for q, _ in executor.searched] == ["docker", "containers"]


async def test_find_then_read_is_expressible(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(
        monkeypatch,
        [
            AIMessage(content="", tool_calls=[_call("SearchMemories", {"query": "redis"})]),
            AIMessage(content="", tool_calls=[_call("GetMemory", {"memory_id": "aa11"}, "c2")]),
            AIMessage(content="It argued for the append-only file."),
        ],
    )
    executor = FakeExecutor()

    await tools.answer_with_tools("what exactly did the redis one say?", [], executor)

    assert executor.searched and executor.opened == ["aa11"]


# --- ceilings ------------------------------------------------------------------------------


async def test_the_call_budget_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent loop with no ceiling is an unbounded bill reachable through a text box."""
    forever = [
        AIMessage(content="", tool_calls=[_call("SearchMemories", {"query": f"q{n}"}, f"c{n}")])
        for n in range(20)
    ]
    _scripted(monkeypatch, forever)
    executor = FakeExecutor()

    result = await tools.answer_with_tools(
        "x", [], executor, max_calls=2, max_rounds=5
    )

    assert result is not None
    assert len(executor.searched) == 2


async def test_the_round_budget_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    forever = [
        AIMessage(content="", tool_calls=[_call("SearchMemories", {"query": f"q{n}"}, f"c{n}")])
        for n in range(20)
    ]
    model = _scripted(monkeypatch, forever)

    result = await tools.answer_with_tools(
        "x", [], FakeExecutor(), max_calls=99, max_rounds=2
    )

    assert result is not None
    assert result.rounds == 2
    # Two tool rounds plus the final unbound turn.
    assert len(model.calls) == 3


async def test_every_tool_call_is_answered_even_past_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool call left without its ToolMessage is a malformed conversation, and
    providers reject the *next* request outright -- so the budget cannot simply skip."""
    _scripted(
        monkeypatch,
        [
            AIMessage(
                content="",
                tool_calls=[
                    _call("SearchMemories", {"query": "a"}, "c1"),
                    _call("SearchMemories", {"query": "b"}, "c2"),
                    _call("SearchMemories", {"query": "c"}, "c3"),
                ],
            ),
            AIMessage(content="done"),
        ],
    )
    model = tools.get_chat_model()  # the scripted one

    await tools.answer_with_tools("x", [], FakeExecutor(), max_calls=1, max_rounds=2)

    sent = model.calls[-1]  # type: ignore[attr-defined]
    answered = {m.tool_call_id for m in sent if isinstance(m, ToolMessage)}
    assert answered == {"c1", "c2", "c3"}


# --- failure degrades, never escalates -------------------------------------------------------


async def test_a_provider_without_tool_support_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NoTools:
        def bind_tools(self, tool_list: list[Any]) -> Any:
            raise NotImplementedError

    monkeypatch.setattr(tools, "get_chat_model", NoTools)
    assert await tools.answer_with_tools("x", [], FakeExecutor()) is None


async def test_a_raising_model_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def bind_tools(self, tool_list: list[Any]) -> Boom:
            return self

        async def ainvoke(self, messages: Any, config: Any = None) -> Any:
            raise TimeoutError("provider down")

    monkeypatch.setattr(tools, "get_chat_model", Boom)
    assert await tools.answer_with_tools("x", [], FakeExecutor()) is None


async def test_an_unknown_tool_is_answered_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model reaching for a tool that does not exist has been confused -- possibly by a
    memory telling it one does. Saying so lets it recover; raising loses the answer."""
    _scripted(
        monkeypatch,
        [
            AIMessage(content="", tool_calls=[_call("DeleteEverything", {})]),
            AIMessage(content="I could not do that."),
        ],
    )
    executor = FakeExecutor()

    result = await tools.answer_with_tools("x", [], executor)

    assert result is not None
    assert result.text == "I could not do that."
    assert executor.searched == [] and executor.opened == []


async def test_bad_arguments_are_answered_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scripted(
        monkeypatch,
        [
            AIMessage(content="", tool_calls=[_call("SearchMemories", {"days": 7})]),
            AIMessage(content="ok"),
        ],
    )
    result = await tools.answer_with_tools("x", [], FakeExecutor())
    assert result is not None and result.text == "ok"


async def test_a_malformed_args_payload_is_survivable() -> None:
    """`AIMessage` validates `tool_calls[].args` as a dict, so this shape cannot arrive
    through a provider today. Checked at the dispatcher rather than trusted: the loop
    reads `tool_calls` off whatever the model returned, and one provider adapter
    returning a looser shape would otherwise be a crash inside a reply."""
    executor = FakeExecutor()
    result = await tools._run(executor, {"name": "SearchMemories", "args": "nope"})
    assert "malformed" in result
    assert executor.searched == []


# --- what the model is given -------------------------------------------------------------------


async def test_only_the_three_read_tools_are_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _scripted(monkeypatch, [AIMessage(content="hello")])
    await tools.answer_with_tools("x", [], FakeExecutor())
    assert model.bound is not None
    assert {t.__name__ for t in model.bound} == {
        "SearchMemories",
        "ListMemories",
        "GetMemory",
    }
