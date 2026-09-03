"""A recorded model, and the graph shim that replays it.

`create_react_agent` is stubbed rather than driven, because what the agent tests are about
is the *harness* -- budgets, guards, what is emitted and what is swallowed -- and none of
that is a property of LangGraph. Replaying a recorded turn keeps the tests offline,
deterministic and fast, which is the same reason `_no_provider_calls` exists.

A "turn" here is what the model emits in one step: some prose, some tool calls, or both.
The shim yields them as `AIMessageChunk`s in the shape `stream_mode="messages"` produces,
including the detail that matters -- a streamed tool call's arguments arrive in fragments
and only the first fragment carries the name.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessageChunk, ToolMessage


@dataclass
class Turn:
    """One model step: what it says, and what it calls."""

    text: str = ""
    #: `(tool_name, arguments)`. Arguments are JSON-encoded and split across chunks.
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


@dataclass
class RecordedModel:
    """Replays turns in order, running the executor's tools as it goes.

    It also records which tools it was asked for, so a golden case can assert on the
    plan the model followed rather than only on the words it ended with.
    """

    turns: Sequence[Turn]
    #: Raised instead of finishing, to exercise the before-words / after-words rule.
    fail_after: int | None = None
    error: type[BaseException] = RuntimeError
    called: list[str] = field(default_factory=list)

    def __call__(self, model: Any, tools: Sequence[Any]) -> _Graph:
        return _Graph(self, {tool.name: tool for tool in tools})


class _Graph:
    """The object `create_react_agent` would have returned."""

    def __init__(self, recorded: RecordedModel, tools: dict[str, Any]) -> None:
        self.recorded = recorded
        self.tools = tools

    async def astream(
        self, _state: Any, *, stream_mode: str = "messages", config: Any = None
    ) -> AsyncIterator[tuple[Any, dict[str, Any]]]:
        emitted = 0
        step = 0
        for turn in self.recorded.turns:
            step += 1
            # Checked before the turn is emitted, so `fail_after=0` fails with nothing
            # shown and `fail_after=1` fails after exactly one turn has been. The
            # before-words / after-words rule is the thing under test, so which side of
            # the first word the failure lands on has to be exact.
            if self.recorded.fail_after is not None and emitted >= self.recorded.fail_after:
                raise self.recorded.error("the provider went away")
            if turn.text:
                for piece in _split(turn.text):
                    yield AIMessageChunk(content=piece), {"langgraph_step": step}
            for index, (name, args) in enumerate(turn.calls):
                self.recorded.called.append(name)
                for chunk in _call_chunks(index, name, args):
                    yield chunk, {"langgraph_step": step}
                # The tool really runs: the toolbox is the thing under test in most of
                # these, and its budget and surfaced set only move when it is used.
                tool = self.tools.get(name)
                if tool is not None:
                    result = await _run(tool, args)
                    # Emitted the way a real run emits it, so a driver that forgets to
                    # skip ToolMessage chunks fails here rather than in production.
                    step += 1
                    yield (
                        ToolMessage(content=str(result), tool_call_id=f"c{index}"),
                        {"langgraph_step": step},
                    )
            emitted += 1


async def _run(tool: Any, args: dict[str, Any]) -> Any:
    if tool.coroutine is not None:
        return await tool.coroutine(**args)
    return tool.func(**args)


def _split(text: str, size: int = 7) -> Iterable[str]:
    """Prose in fragments, because a real stream never arrives whole."""
    for start in range(0, len(text), size):
        yield text[start : start + size]


def _call_chunks(index: int, name: str, args: dict[str, Any]) -> Iterable[AIMessageChunk]:
    """A tool call as it streams: the name once, then the arguments in pieces."""
    encoded = json.dumps(args)
    head, tail = encoded[:4], encoded[4:]
    yield AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": name, "args": head, "id": f"c{index}", "index": index}
        ],
    )
    if tail:
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {"name": None, "args": tail, "id": None, "index": index}
            ],
        )
