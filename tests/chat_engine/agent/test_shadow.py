"""Running the agent beside the old router, and never in front of it.

Shadow mode's whole value is that it costs the user nothing, so the properties worth
pinning are about *ordering and side effects* rather than about answers: the run is
queued after the reply has gone out, it writes nothing to the conversation, and it is off
unless someone switched it on.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import settings
from app.services.chat_engine.router import Intent
from app.services.chat_engine.types import InboundMessage
from app.services.telegram import dispatch as dispatch_module

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _msg(text: str = "what did I save?") -> InboundMessage:
    return InboundMessage(
        surface="telegram",
        external_user_id="9",
        external_chat_id="4242",
        text=text,
    )


# --- when it is recorded --------------------------------------------------------------


def test_no_shadow_is_recorded_when_it_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default. A model call per message is not something to acquire by accident."""
    monkeypatch.setattr(settings, "AGENT_SHADOW", False)

    assert dispatch_module._shadow_for(_USER, _msg(), Intent.RECALL) is None


def test_a_shadow_carries_the_same_conversation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It has to read the same history the real turn read.

    An agent given no context is not the agent being evaluated, so the key is built by
    the same function the engine uses rather than spelled out a second time here.
    """
    from app.services.chat_engine.engine import session_id_for

    monkeypatch.setattr(settings, "AGENT_SHADOW", True)
    message = _msg()

    shadow = dispatch_module._shadow_for(_USER, message, Intent.RECALL)

    assert shadow is not None
    assert shadow.session_id == session_id_for(_USER, message)
    assert shadow.question == "what did I save?"
    assert shadow.lane == "recall"


def test_an_empty_message_is_not_shadowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no question to compare two systems on."""
    monkeypatch.setattr(settings, "AGENT_SHADOW", True)

    assert dispatch_module._shadow_for(_USER, _msg("   "), Intent.CHAT) is None


# --- when it runs ---------------------------------------------------------------------


async def test_the_shadow_run_writes_nothing_to_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shadow answer appended to the real history would be replayed into the next real
    prompt -- the experiment would start steering the thing it is measuring."""
    from contextlib import asynccontextmanager

    from app.queue import tasks
    from app.services import recall_agent

    seen: dict[str, Any] = {}

    class FakeSession:
        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def _session() -> Any:
        yield FakeSession()

    class FakeAgent:
        def __init__(self, repo: Any, *_: Any, **__: Any) -> None:
            pass

        async def agent(
            self, user_id: uuid.UUID, question: str, session_id: str, *, store: bool = True
        ) -> Any:
            seen["store"] = store
            seen["session_id"] = session_id
            seen["user_id"] = user_id
            return None

    monkeypatch.setattr(tasks, "task_session", _session)
    monkeypatch.setattr(recall_agent, "RecallAgentService", FakeAgent)

    await tasks._shadow_agent_turn(
        str(_USER), "what did I save?", f"{_USER}:4242", "telegram", "recall"
    )

    assert seen["store"] is False
    assert seen["session_id"] == f"{_USER}:4242"
    assert seen["user_id"] == _USER


def test_a_failing_shadow_run_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """It answers nobody, so it must not be able to page anyone or retry into a bill.

    Synchronous on purpose: the task body calls `asyncio.run`, which raises immediately
    inside an already-running loop. An async test here would pass on that error instead
    of on the one it means to exercise, and would leave a never-awaited coroutine behind
    to say so.
    """
    from app.queue import tasks

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("the provider is down")

    # The inner coroutine, not `asyncio.run`: stubbing the runner leaves a coroutine
    # created and never awaited, which is a warning about the test rather than about the
    # code, and the failure path under test is the one inside the task body anyway.
    monkeypatch.setattr(tasks, "_shadow_agent_turn", _explode)

    # Called the way Celery calls a bound task: `self` is supplied by the decorator, so
    # the callable here already has it applied.
    tasks.shadow_agent_turn(str(_USER), "q", "s", "telegram", "recall")  # must not raise
