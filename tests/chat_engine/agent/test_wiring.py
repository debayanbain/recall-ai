"""Does anything actually call it.

The dullest tests here and the ones most worth having. `send_chat_action` sat in the
Telegram client for months with a passing unit test and no caller at all, and the whole
agent lane is the same shape of risk: a graph, a toolbox and a guard can all be correct
while the engine still routes every message to the old path.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from app.core.config import settings
from app.services.chat_engine.engine import ChatEngine, _has_agent
from app.services.chat_engine.types import Delta, InboundMessage, StreamEnd, StreamEvent
from app.services.recall_chat import RecallAnswer

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _msg(text: str) -> InboundMessage:
    return InboundMessage(
        surface="web", external_user_id="9", external_chat_id="c1", text=text
    )


class _Lanes:
    """Every lane, each recording that it was the one chosen."""

    def __init__(self) -> None:
        self.answered: list[str] = []
        self.agented: list[str] = []

    async def answer(self, user_id: uuid.UUID, question: str, session_id: str) -> RecallAnswer:
        self.answered.append(question)
        return RecallAnswer(text="from the old lane")

    async def agent(self, user_id: uuid.UUID, question: str, session_id: str) -> RecallAnswer:
        self.agented.append(question)
        return RecallAnswer(text="from the agent")

    async def stream_agent(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> AsyncIterator[StreamEvent]:
        self.agented.append(question)
        yield Delta(text="from the agent")
        yield StreamEnd()


class _OldLanes:
    """The lanes as they were before the agent existed."""

    def __init__(self) -> None:
        self.answered: list[str] = []

    async def answer(self, user_id: uuid.UUID, question: str, session_id: str) -> RecallAnswer:
        self.answered.append(question)
        return RecallAnswer(text="from the old lane")


# --- detection ------------------------------------------------------------------------


def test_the_agent_is_detected_structurally() -> None:
    """A capability check, like `_streams` -- adding methods to a Protocol would break
    every fake in the suite at runtime rather than at type-check time."""
    assert _has_agent(_Lanes()) is True  # type: ignore[arg-type]
    assert _has_agent(_OldLanes()) is False  # type: ignore[arg-type]


# --- and does the engine use it -------------------------------------------------------


async def test_a_question_reaches_the_agent_when_one_is_wired_up() -> None:
    lanes = _Lanes()

    reply = await ChatEngine(lanes, _USER).handle(_msg("what did I save?"))  # type: ignore[arg-type]

    assert lanes.agented == ["what did I save?"]
    assert lanes.answered == []
    assert "from the agent" in str(reply.blocks)


async def test_the_same_question_falls_to_the_old_lane_without_one() -> None:
    """Switching the agent off is a supported state, not a broken one."""
    lanes = _OldLanes()

    await ChatEngine(lanes, _USER).handle(_msg("what did I save?"))  # type: ignore[arg-type]

    assert lanes.answered == ["what did I save?"]


async def test_the_streamed_path_reaches_the_agent_too() -> None:
    """Two entry points, one decision. The web surface streams and the bot does not."""
    lanes = _Lanes()

    events = [
        event async for event in ChatEngine(lanes, _USER).stream(_msg("what did I save?"))  # type: ignore[arg-type]
    ]

    assert lanes.agented == ["what did I save?"]
    assert any(isinstance(e, Delta) and "agent" in e.text for e in events)


async def test_a_greeting_takes_the_agent_too() -> None:
    """Deliberately reversed at the flip, and worth stating rather than quietly changing.

    While the no-vault conversation lane existed, sending "hello" to the agent was pure
    cost -- a loop of model calls to produce a greeting. That lane is gone, because it was
    also where every unrecognised phrasing landed and it could only deflect. So a greeting
    costs a turn now and buys something with it: the snapshot is in the prompt, so the
    reply can greet *and* say what is actually saved. The router still labels it `chat`,
    which is what keeps that cost visible in `agent_turn.router_lane`.
    """
    lanes = _Lanes()

    await ChatEngine(lanes, _USER).handle(_msg("hello"))  # type: ignore[arg-type]

    assert lanes.agented == ["hello"]


# --- and does the builder build it ----------------------------------------------------


def test_the_builder_returns_the_agent_only_when_it_is_switched_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting is the switch; a lane nobody can turn on is a lane nobody has."""
    from app.services import recall_chat
    from app.services.recall_agent import RecallAgentService

    monkeypatch.setattr(recall_chat, "chat_available", lambda: True)

    monkeypatch.setattr(settings, "AGENT_ENABLED", False)
    plain = recall_chat.build_recall_responder(None)  # type: ignore[arg-type]
    assert plain is not None and not isinstance(plain, RecallAgentService)

    monkeypatch.setattr(settings, "AGENT_ENABLED", True)
    built = recall_chat.build_recall_responder(None)  # type: ignore[arg-type]
    assert isinstance(built, RecallAgentService)
    assert _has_agent(built)


def test_no_chat_model_means_no_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lane that needs a provider and has none must not be handed out."""
    from app.services import recall_chat

    monkeypatch.setattr(recall_chat, "chat_available", lambda: False)
    monkeypatch.setattr(settings, "AGENT_ENABLED", True)

    assert recall_chat.build_recall_responder(None) is None  # type: ignore[arg-type]


def test_any_new_tool_is_reachable_from_the_real_toolbox() -> None:
    """The binder offers a tool only when the executor implements it.

    So a schema added without its method is a tool the model is never told about, which
    is silent. This asserts the real toolbox serves every tool the harness declares.
    """
    from app.ai.chat.harness.tools import build_tools
    from app.services.chat_engine.toolbox import MemoryToolbox

    names = {tool.name for tool in build_tools(MemoryToolbox(_USER, None))}  # type: ignore[arg-type]

    assert names == {
        "SearchMemories",
        "ListMemories",
        "GetMemory",
        "GetCaptureStatus",
        "AskUser",
        "FinalAnswer",
    }
