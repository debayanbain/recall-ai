"""The tools the agent may call, and the rule that decides which of them it gets.

The three read tools are the *same objects* the older lane binds -- imported, not copied.
Two descriptions of `SearchMemories` would be two descriptions the model sees on two code
paths, and the one nobody edited would be the one in production.

**Binding is a capability check, not a list.** Each tool is offered only when the executor
in front of it actually implements the method, so the same builder serves a toolbox with
proposals wired up and one without, and a deployment with Redis down loses its proposal
tools rather than gaining a tool that raises. A model is never told about something it
cannot use: an offered tool that fails is a wasted round and a confused answer.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.ai.chat.harness.schemas import (
    AskUser,
    FinalAnswer,
    GetCaptureStatus,
    GetConnections,
    ProposeConnect,
    ProposeDelete,
    ProposeNote,
    ProposeRetry,
    QueryMemories,
)
from app.ai.chat.tools import GetMemory, ListMemories, SearchMemories
from app.core.logging import get_logger

log = get_logger("recall.agent")

__all__ = [
    "AgentTools",
    "AskUser",
    "FinalAnswer",
    "GetCaptureStatus",
    "GetConnections",
    "GetMemory",
    "ListMemories",
    "ProposeConnect",
    "ProposeDelete",
    "ProposeNote",
    "ProposeRetry",
    "QueryMemories",
    "SearchMemories",
    "build_tools",
]


@runtime_checkable
class AgentTools(Protocol):
    """What the graph needs from an executor.

    A **new** Protocol rather than more methods on `MemoryTools`: that one is structural,
    so widening it would break every fake in the suite at runtime instead of at
    type-check time, and the older lane would start advertising tools it never had.
    """

    async def query_memories(
        self,
        text: str | None = None,
        days: int | None = None,
        content_types: Sequence[str] = (),
        category: str | None = None,
        status: str | None = None,
        tags: Sequence[str] = (),
        limit: int | None = None,
        fields: Sequence[str] = (),
    ) -> str: ...

    async def get_memory(self, memory_id: str) -> str: ...

    async def get_connections(
        self, memory_id: str, relation: str | None = None, limit: int | None = None
    ) -> str: ...

    async def get_capture_status(self, memory_id: str | None = None) -> str: ...

    async def ask_user(self, question: str, options: Sequence[str] = ()) -> str: ...

    async def propose_note(self, text: str) -> str: ...

    async def propose_retry(self, memory_id: str) -> str: ...

    async def propose_delete(self, memory_id: str) -> str: ...

    async def propose_connect(
        self, memory_id: str, other_id: str, relation: str = "related_to"
    ) -> str: ...


def build_tools(executor: Any) -> list[StructuredTool]:
    """The tools this executor can actually serve, each closed over it.

    Closing over the executor is what keeps `user_id` out of every schema: these are the
    only tools the graph can call, and each can only read the one person's rows the
    executor was constructed for. A tool that took the owner as an argument would be a
    tool a prompt injection could aim at someone else.
    """
    tools: list[StructuredTool] = []

    # One composable read where there used to be two fixed ones. `SearchMemories` and
    # `ListMemories` still exist and are still bound by the older lane in
    # `ai/chat/tools.py`; they are not offered here, because two tools that overlap this
    # one are two more ways for the model to pick the narrower answer.
    if hasattr(executor, "query_memories"):
        tools.append(
            _tool(
                QueryMemories,
                coroutine=lambda text=None, days=None, content_types=(), category=None,
                status=None, tags=(), limit=None, fields=(): executor.query_memories(
                    text, days, content_types, category, status, tags, limit, fields
                ),
            )
        )
    if hasattr(executor, "get_memory"):
        tools.append(
            _tool(GetMemory, coroutine=lambda memory_id: executor.get_memory(memory_id))
        )
    # Gated on the reader rather than on the method, like the proposal tools are gated on
    # the store. The method exists on every toolbox; a turn with nowhere to read edges from
    # must not be told about a tool whose only possible answer is an apology.
    if getattr(executor, "connections", None) is not None and hasattr(
        executor, "get_connections"
    ):
        tools.append(
            _tool(
                GetConnections,
                coroutine=lambda memory_id, relation=None, limit=None: (
                    executor.get_connections(memory_id, relation, limit)
                ),
            )
        )
    if hasattr(executor, "get_capture_status"):
        tools.append(
            _tool(
                GetCaptureStatus,
                coroutine=lambda memory_id=None: executor.get_capture_status(memory_id),
            )
        )
    if hasattr(executor, "ask_user"):
        tools.append(
            _tool(
                AskUser,
                coroutine=lambda question, options=(): executor.ask_user(
                    question, options
                ),
            )
        )
    # The write-shaped tools, and the only conditional binding that is about state rather
    # than about capability: with nowhere to park a proposal there is nothing to confirm,
    # so the model is not told about them at all. An offered tool that cannot work is a
    # wasted round and an answer that promises something impossible.
    if getattr(executor, "store", None) is not None:
        if hasattr(executor, "propose_note"):
            tools.append(
                _tool(
                    ProposeNote,
                    coroutine=lambda text: executor.propose_note(text),
                )
            )
        if hasattr(executor, "propose_retry"):
            tools.append(
                _tool(
                    ProposeRetry,
                    coroutine=lambda memory_id: executor.propose_retry(memory_id),
                )
            )
        if hasattr(executor, "propose_delete"):
            tools.append(
                _tool(
                    ProposeDelete,
                    coroutine=lambda memory_id: executor.propose_delete(memory_id),
                )
            )
        # Gated on the connection reader as well as the store: without one there is no
        # way to show the edge afterwards, and offering a write whose result is invisible
        # is worse than not offering it.
        if getattr(executor, "connections", None) is not None and hasattr(
            executor, "propose_connect"
        ):
            tools.append(
                _tool(
                    ProposeConnect,
                    coroutine=lambda memory_id, other_id, relation="related_to": (
                        executor.propose_connect(memory_id, other_id, relation)
                    ),
                )
            )

    # Always offered, and deliberately last: it is how a turn ends rather than something
    # the executor does, so it needs nothing from it. The graph reads the call and stops.
    tools.append(_tool(FinalAnswer, func=lambda **kwargs: _ACKNOWLEDGED))
    return tools


#: The result of `FinalAnswer`. The graph stops on the call itself, so this is only ever
#: seen if a provider insists on a result before it will end the turn.
_ACKNOWLEDGED = "Answer recorded. The turn is over."


def _tool(schema: type[BaseModel], **kwargs: Any) -> StructuredTool:
    """One `StructuredTool` from a schema class.

    The class *name* is the tool name and the class *docstring* is the description the
    provider sees, exactly as in `ai/chat/agent.py` -- so the prose in `schemas.py` is
    written for the model, not for a reader of this file.
    """
    return StructuredTool.from_function(
        name=schema.__name__,
        description=schema.__doc__ or "",
        args_schema=schema,
        **kwargs,
    )
