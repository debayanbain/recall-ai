"""The two lanes themselves, as `RecallChatService` implements them.

Which lane a message takes is no longer decided here -- that moved to
`app/services/chat_engine/engine.py` and is pinned by `tests/chat_engine/test_engine.py`.
What is left is the promise each lane makes on its own: the chat lane never touches the
vault, and neither lane ever lets a provider traceback out.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.ai.chat import chain, history
from app.services.recall_chat import RecallChatService

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def _offline(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """No model and no Redis: record what reached the conversational chain."""
    seen: list[str] = []

    async def _converse(message: str, past: Any) -> str:
        seen.append(message)
        return "Hey! Send me a link and I'll keep it."

    async def _load(session_id: str) -> list[Any]:
        return []

    async def _append(session_id: str, question: str, reply: str) -> None:
        return None

    monkeypatch.setattr(chain, "converse", _converse)
    monkeypatch.setattr(history, "load", _load)
    monkeypatch.setattr(history, "append", _append)
    return seen


async def test_the_chat_lane_never_touches_the_vault(_offline: list[str]) -> None:
    """`repo=None` is the assertion: a single query would raise instead of passing."""
    service = RecallChatService(repo=None)  # type: ignore[arg-type]
    answer = await service.chat("hi", session_id="555000")

    assert _offline == ["hi"]
    assert answer.text == "Hey! Send me a link and I'll keep it."
    assert answer.items == [] and not answer.failed


async def test_a_provider_failure_is_a_failure_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(message: str, past: Any) -> str:
        raise RuntimeError("provider exploded")

    async def _load(session_id: str) -> list[Any]:
        return []

    monkeypatch.setattr(chain, "converse", _boom)
    monkeypatch.setattr(history, "load", _load)

    service = RecallChatService(repo=None)  # type: ignore[arg-type]
    answer = await service.chat("hi", session_id="555000")

    assert answer.failed and answer.text is None
