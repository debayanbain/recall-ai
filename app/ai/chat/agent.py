"""The tool lane, driven by a graph so it can be streamed.

`tools.answer_with_tools` works and is the fallback under this, so the honest question is
what a graph buys. One thing: **streaming a lane that uses tools**. A loop built on
`ainvoke` has to receive a whole turn before it knows whether that turn was tool calls or
the answer, so the answer can only be delivered after it is finished -- which is exactly
backwards, because the surface that most needs the words as they arrive is the web page
where a reader is watching the spot they will appear in. `stream_mode="messages"` yields
the model's tokens as it writes them while still running the tool rounds underneath.

Everything that makes the tool lane safe is unchanged, because none of it lives in the
driver:

* **The tools are read-only and the user is not an argument.** Both are properties of
  `MemoryToolbox`, which is constructed with an already-resolved `user_id` and closed
  over here. There is no field in any schema a model could put someone else's id in.
* **The relevance gate and the per-turn id registry** are the toolbox's, so a search run
  by the graph is filtered exactly as one run by the loop.
* **The answer is still validated on the way out** by the caller, against the ids the
  toolbox actually surfaced.

What is deliberately *not* used: checkpointers, interrupts, human-in-the-loop and
persistence. This graph answers one question and ends; a checkpointer would add a store to
operate for a conversation that is already carried in Redis by `history.py`.

The bound is `recursion_limit`, which counts graph steps rather than tool calls -- a model
turn and a tool batch are one step each, so the limit is roughly twice the number of tool
rounds. Hitting it raises, and the caller falls back.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.tools import StructuredTool

from app.ai.chat.factory import get_chat_model
from app.ai.chat.tools import (
    TOOL_SYSTEM,
    GetMemory,
    ListMemories,
    MemoryTools,
    SearchMemories,
)
from app.ai.chat.usage import UsageLogger
from app.core.logging import get_logger

log = get_logger("recall.chat")


@dataclass(slots=True)
class AgentDelta:
    """A fragment of the answer, as the model writes it."""

    text: str


@dataclass(slots=True)
class AgentToolCall:
    """A tool the model decided to run. Emitted so a surface can say what is happening.

    A reader waiting on a search is waiting on a database and a provider, and "searching
    your memories" is the difference between a pause and a hang. It carries the tool's
    name only -- never its arguments, which are model output derived from the person's
    own question and have no business being echoed back as a status line.
    """

    name: str


@dataclass(slots=True)
class AgentEnd:
    """The terminal event. `failed` means the caller should use the other driver."""

    failed: bool = False
    calls: list[str] = field(default_factory=list)


AgentEvent = AgentDelta | AgentToolCall | AgentEnd


def _build_tools(executor: MemoryTools) -> list[StructuredTool]:
    """The three read tools, each closed over one already-authorised executor.

    Closing over the executor is what keeps `user_id` out of every schema: the graph can
    only call these, and these can only read one person's rows. A tool that took the user
    as an argument would be a tool a prompt injection could aim.
    """
    return [
        StructuredTool.from_function(
            coroutine=lambda query, days=None, content_types=(), category=None: (
                executor.search_memories(query, days, content_types, category)
            ),
            name=SearchMemories.__name__,
            description=SearchMemories.__doc__ or "",
            args_schema=SearchMemories,
        ),
        StructuredTool.from_function(
            coroutine=lambda days=None, content_types=(), category=None: (
                executor.list_memories(days, content_types, category)
            ),
            name=ListMemories.__name__,
            description=ListMemories.__doc__ or "",
            args_schema=ListMemories,
        ),
        StructuredTool.from_function(
            coroutine=lambda memory_id: executor.get_memory(memory_id),
            name=GetMemory.__name__,
            description=GetMemory.__doc__ or "",
            args_schema=GetMemory,
        ),
    ]


async def stream_with_tools(
    question: str,
    history: Sequence[BaseMessage],
    executor: MemoryTools,
    *,
    max_rounds: int = 3,
) -> AsyncIterator[AgentEvent]:
    """Run the tool lane and yield the answer as it is written.

    Never raises. A provider that cannot bind tools, a graph that hits its step limit, a
    stream that dies -- all end with `AgentEnd(failed=True)`, and the caller answers the
    question by another route rather than showing the reader a failure.
    """
    calls: list[str] = []
    try:
        from langgraph.prebuilt import create_react_agent

        graph = create_react_agent(get_chat_model(), _build_tools(executor))
    except Exception as exc:  # noqa: BLE001 - the caller has a working fallback
        log.warning("recall_agent_unavailable", error=type(exc).__name__)
        yield AgentEnd(failed=True)
        return

    messages: list[BaseMessage] = [
        SystemMessage(content=TOOL_SYSTEM),
        *history,
        ("human", question),  # type: ignore[list-item]
    ]
    spoke = False
    try:
        async for chunk, _meta in graph.astream(
            {"messages": messages},
            stream_mode="messages",
            config={
                # Steps, not tool calls: a model turn and a tool batch are one each.
                "recursion_limit": max_rounds * 2 + 1,
                "callbacks": [UsageLogger("answer_agent")],
            },
        ):
            for name in _tool_names(chunk):
                calls.append(name)
                yield AgentToolCall(name=name)
            text = _text_of(chunk)
            if text:
                spoke = True
                yield AgentDelta(text=text)
    except Exception as exc:  # noqa: BLE001 - see above
        log.warning("recall_agent_failed", error=type(exc).__name__, spoke=spoke)
        # A failure *after* the reader has seen words cannot be retried by another route
        # without repeating them, so it is reported as a plain end rather than as a
        # fallback request. Whatever arrived is what the answer is.
        yield AgentEnd(failed=not spoke, calls=calls)
        return

    yield AgentEnd(failed=not spoke, calls=calls)


def _tool_names(chunk: Any) -> list[str]:
    """Tool calls announced by this chunk, if any.

    Read from `tool_call_chunks` rather than `tool_calls`: while streaming, a call's
    arguments arrive in pieces and only the first piece carries the name.
    """
    if not isinstance(chunk, AIMessage):
        return []
    pieces = getattr(chunk, "tool_call_chunks", None) or []
    return [
        str(piece["name"])
        for piece in pieces
        if isinstance(piece, dict) and piece.get("name")
    ]


def _text_of(chunk: Any) -> str:
    """The prose in this chunk, if any. Tool results are not prose and are skipped.

    A `ToolMessage` chunk carries the fenced memory blocks the tools returned -- quoted
    material meant for the model, not for the reader. Emitting it would put a raw
    `<memory>` block on the page and, worse, put text the model has not yet read in front
    of the person as though it were the answer.
    """
    if not isinstance(chunk, AIMessage):
        return ""
    content = chunk.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return ""
