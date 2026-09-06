"""The agent loop: model, tools, model, words.

`create_react_agent` with `stream_mode="messages"`, which is the same driver the older
tool lane uses and for the same reason -- a loop built on `ainvoke` has to receive a whole
turn before it knows whether that turn was tool calls or the answer, so the surface that
most needs words as they arrive is the one that could not have tools. Checkpointers,
interrupts and persistence stay unused: this graph answers one question and ends, and the
conversation it belongs to is already carried in Redis by `history.py`.

Four rules hold the edges of the loop, and none of them is a precaution:

* **A `ToolMessage` chunk is never emitted.** It carries the fenced memory blocks the
  tools returned -- quoted material meant for the model, not for the reader -- so putting
  it on the page would show someone raw `<memory>` blocks and, worse, show them text the
  model has not read yet as though it were the answer.
* **A failure before any words is silent**, so the caller can answer by the older route.
  A failure *after* words is final: repeating sentences the reader has already seen is
  worse than the shorter answer they have. `GraphRecursionError` -- the loop hitting its
  own step ceiling -- and the wall-clock timeout take exactly the same rule, because from
  the reader's side they are the same event.
* **The turn ends with `FinalAnswer`.** When the model ends with plain prose instead, the
  prose is the answer and `agent_no_final_tool` is logged: the wrapper is a fallback, and
  how often it fires is the measure of whether the instruction is working.
* **Budgets are the toolbox's, not the graph's.** Past its allowance a tool answers
  "budget spent" rather than refusing to run, so every call still gets its `ToolMessage`
  and the conversation stays well-formed. Only rounds are counted here, as
  `recursion_limit`, because a round is a graph step and nothing else can see it.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from app.ai.chat.factory import get_agent_model
from app.ai.chat.harness.prompts import AGENT_SYSTEM
from app.ai.chat.harness.schemas import FinalAnswer
from app.ai.chat.harness.tools import build_tools
from app.ai.chat.usage import UsageLogger
from app.core.logging import get_logger
from app.services.chat_engine.budget import Budget

log = get_logger("recall.agent")


@dataclass(slots=True)
class AgentDelta:
    """A fragment of the answer, as the model writes it."""

    text: str


@dataclass(slots=True)
class AgentToolCall:
    """A tool the model decided to run, announced so a surface can say what is happening.

    Carries the name only. The arguments are the model's own guess at the subject, and
    echoing that back as a status line shows a person a guess as though it were a fact.
    """

    name: str


@dataclass(slots=True)
class AgentEnd:
    """The terminal event. `failed` means the caller should answer by the other route."""

    failed: bool = False
    calls: list[str] = field(default_factory=list)
    rounds: int = 0
    final: FinalAnswer | None = None
    #: Set when the turn ran out of budget or time rather than finishing on its own.
    exhausted: bool = False


AgentEvent = AgentDelta | AgentToolCall | AgentEnd


async def run_agent(
    question: str,
    history: Sequence[BaseMessage],
    executor: Any,
    *,
    context: str = "",
    budget: Budget,
) -> AsyncIterator[AgentEvent]:
    """Run one turn and yield it as it is written. Never raises."""
    calls: list[str] = []
    try:
        from langgraph.prebuilt import create_react_agent

        graph = create_react_agent(get_agent_model(), build_tools(executor))
    except Exception as exc:  # noqa: BLE001 - the caller has a working fallback
        log.warning("agent_unavailable", error=type(exc).__name__)
        yield AgentEnd(failed=True)
        return

    messages: list[BaseMessage] = [
        SystemMessage(content=AGENT_SYSTEM),
        *([SystemMessage(content=context)] if context else []),
        *history,
        ("human", question),  # type: ignore[list-item]
    ]

    spoke = False
    rounds = 0
    return_final: FinalAnswer | None = None
    args_by_call: dict[tuple[int, int], _PartialCall] = {}
    try:
        async with asyncio.timeout(budget.wall_clock_seconds):
            async for chunk, meta in graph.astream(
                {"messages": messages},
                stream_mode="messages",
                config={
                    # Steps, not tool calls: a model turn and a tool batch are one each.
                    "recursion_limit": budget.max_rounds * 2 + 1,
                    "callbacks": [UsageLogger("agent")],
                },
            ):
                # LangGraph numbers its own steps, and a step is exactly what a round is:
                # one model turn or one tool batch. Counting chunks would count fragments
                # of a sentence, and counting tool calls would miss a turn that made two.
                rounds = max(rounds, _step_of(meta))
                for name in _collect(chunk, args_by_call, rounds):
                    calls.append(name)
                    yield AgentToolCall(name=name)
                text = _text_of(chunk)
                if text:
                    spoke = True
                    yield AgentDelta(text=text)
                # `FinalAnswer` is how a turn ends, but to `create_react_agent` it is a
                # tool like any other: it runs, its result goes back to the model, and the
                # model gets another turn. Left alone that spends a round to say nothing
                # and lets the answer be written twice -- both seen live, on a turn that
                # ran to the step ceiling with FinalAnswer in it twice.
                #
                # Parsing is the completeness test. Arguments arrive in fragments, so
                # valid JSON means the call is whole and there is nothing further to wait
                # for. Stopping here leaves that call without its `ToolMessage`, which is
                # fine precisely because there is no next request to malform.
                if (done := _final_of(args_by_call)) is not None:
                    return_final = done
                    break
    except (TimeoutError, Exception) as exc:  # noqa: BLE001 - see the module docstring
        # GraphRecursionError arrives here too. All three -- a provider fault, the step
        # ceiling, the wall clock -- are the same event from the reader's side, and the
        # only question that matters is whether anything has already been shown.
        log.warning(
            "agent_failed",
            error=type(exc).__name__,
            spoke=spoke,
            calls=len(calls),
            elapsed_ms=int(budget.elapsed * 1000),
        )
        yield AgentEnd(
            failed=not spoke,
            calls=calls,
            rounds=rounds,
            final=_final_of(args_by_call),
            exhausted=True,
        )
        return

    final = return_final or _final_of(args_by_call)
    if final is None and spoke:
        # The instruction says to end with FinalAnswer and the model ended with prose.
        # Taken as the answer rather than discarded -- the words are already on their way
        # to the reader -- and counted, because a rise here means the instruction has
        # stopped working and the self-reported flags have gone with it.
        log.info("agent_no_final_tool", calls=len(calls))
    yield AgentEnd(
        failed=not spoke and final is None,
        calls=calls,
        rounds=rounds,
        final=final,
        exhausted=budget.exhausted,
    )


def _step_of(meta: Any) -> int:
    """The graph step this chunk belongs to, or 0 when the driver does not report one."""
    if not isinstance(meta, dict):
        return 0
    try:
        return int(meta.get("langgraph_step") or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class _PartialCall:
    """One tool call being assembled. Streamed arguments arrive in pieces."""

    name: str = ""
    args: str = ""


def _collect(
    chunk: Any, into: dict[tuple[int, int], _PartialCall], step: int
) -> list[str]:
    """Tool calls announced by this chunk, accumulating their arguments as they arrive.

    Read from `tool_call_chunks` rather than `tool_calls`: while streaming, a call's
    arguments arrive in fragments and only the first fragment carries the name. The
    fragments are kept because `FinalAnswer`'s payload *is* its arguments -- there is no
    other copy of it anywhere in the stream.

    **Keyed by `(step, index)`, not by `index`.** A provider numbers the tool calls
    within one model turn, so the numbering restarts at zero on the next one: keyed by
    index alone, a search in round one and the answer in round two shared an accumulator
    and their two JSON documents were concatenated into one unparseable string. The
    effect was quietly bad -- every turn that called any tool before answering lost its
    `FinalAnswer`, so the reply still arrived as prose while the self-reported flags on
    `agent_turn` silently stayed false. A turn where the model answered *immediately* was
    the only shape that worked, which is exactly the shape that hides the bug.

    A name arriving at a key that already holds a different one starts that accumulator
    again, which covers the same collision if a driver ever stops reporting steps.
    """
    if not isinstance(chunk, AIMessage):
        return []
    announced: list[str] = []
    for piece in getattr(chunk, "tool_call_chunks", None) or []:
        if not isinstance(piece, dict):
            continue
        key = (step, int(piece.get("index") or 0))
        name = piece.get("name")
        partial = into.setdefault(key, _PartialCall())
        if name and partial.name and partial.name != str(name):
            partial = _PartialCall()
            into[key] = partial
        if name:
            partial.name = str(name)
            announced.append(str(name))
        partial.args += str(piece.get("args") or "")
    return announced


def _final_of(calls: dict[tuple[int, int], _PartialCall]) -> FinalAnswer | None:
    """The `FinalAnswer` the turn ended with, if it ended with one.

    Validated through the schema rather than trusted as a dict: the arguments were
    assembled from stream fragments, and a truncated stream produces JSON that parses
    into the wrong shape as readily as into none at all.
    """
    for partial in reversed(list(calls.values())):
        if partial.name != FinalAnswer.__name__:
            continue
        try:
            return FinalAnswer.model_validate(json.loads(partial.args or "{}"))
        except Exception as exc:  # noqa: BLE001 - a half-written call is not an answer
            log.info("agent_final_unparsable", error=type(exc).__name__)
            return None
    return None


def _text_of(chunk: Any) -> str:
    """The prose in this chunk, if any. Tool results are not prose and are skipped."""
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
