"""Which lane a plain-text message takes, and that neither lane writes anything.

`RecallChatService.respond` is the single entry point the Telegram dispatcher calls. It
splits on `looks_like_question`, which costs no tokens: a question about the vault gets
retrieval, anything else gets a conversational reply with no retrieval at all.

The split matters beyond phrasing. Sending "hi" down the retrieval lane runs an embedding
and a vector search over a greeting and then reports that nothing was saved about "hi" --
paying for a reply that reads as a broken bot.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.ai.chat import chain, history
from app.services.recall_chat import RecallAnswer, RecallChatService

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


async def test_a_greeting_is_answered_without_touching_the_vault(
    _offline: list[str],
) -> None:
    service = RecallChatService(repo=None)  # type: ignore[arg-type]
    answer = await service.respond(_USER, "hi", session_id="555000")

    assert _offline == ["hi"]
    assert answer.text == "Hey! Send me a link and I'll keep it."
    assert answer.items == [] and not answer.failed


async def test_a_vault_question_does_not_take_the_chat_lane(
    _offline: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    async def _answer(user_id: uuid.UUID, question: str, session_id: str) -> RecallAnswer:
        return RecallAnswer(text="You saved two things about pasta.")

    monkeypatch.setattr(service, "answer", _answer)
    result = await service.respond(_USER, "any pasta videos?", session_id="555000")

    assert _offline == []
    assert result.text == "You saved two things about pasta."


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
    answer = await service.respond(_USER, "hi", session_id="555000")

    assert answer.failed and answer.text is None
