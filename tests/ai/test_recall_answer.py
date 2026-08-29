"""The recall lane: when it spends a model call, and how much of a memory it shows.

Two rules carry the cost argument for the whole feature. Zero hits must not reach the
answer model -- an empty context invites it to invent a memory and charges for the
privilege. And a memory's body is shown only when the question asked for the words
rather than for which memory it was.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.ai.chat import chain, history
from app.ai.chat.planner import MemoryQuery
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.recall_chat import RecallChatService

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_BODY = "Redis persists with RDB snapshots and an append-only file. " * 30


def _item(n: int = 0) -> VaultItem:
    return VaultItem(
        user_id=_USER,
        type=ContentType.article,
        title=f"Redis persistence {n}",
        ai_label=f"How Redis persistence works, part {n}",
        summary="RDB snapshots versus the append-only file.",
        content=_BODY,
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


@pytest.fixture
def _offline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """No provider, no Redis. Records whether the answer model was reached."""
    seen: dict[str, Any] = {"answers": [], "contexts": [], "recalls": []}

    async def _plan(question: str) -> MemoryQuery:
        return MemoryQuery(search_text="redis")

    async def _answer(question: str, documents: Any, past: Any) -> str:
        seen["answers"].append(question)
        seen["contexts"].append(chain.format_context(documents))
        return "Two memories cover it."

    async def _load(session_id: str) -> list[Any]:
        return []

    async def _append(session_id: str, q: str, a: str) -> None:
        return None

    monkeypatch.setattr("app.services.recall_chat.planner.plan", _plan)
    monkeypatch.setattr(chain, "answer", _answer)
    monkeypatch.setattr(history, "load", _load)
    monkeypatch.setattr(history, "append", _append)
    return seen


def _with_memories(monkeypatch: pytest.MonkeyPatch, items: list[VaultItem]) -> list[int]:
    """Stub retrieval and record the limit it was asked for."""
    limits: list[int] = []

    async def _recall(
        self: MemoryRetriever,
        user_id: uuid.UUID,
        question: str,
        filters: Any = None,
        *,
        limit: int = 8,
    ) -> list[VaultItem]:
        limits.append(limit)
        return items[:limit]

    monkeypatch.setattr(MemoryRetriever, "recall", _recall)
    return limits


# --- zero hits -----------------------------------------------------------------------


async def test_zero_hits_never_reaches_the_answer_model(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_memories(monkeypatch, [])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    answer = await service.answer(_USER, "what did I save about redis?", "555000")

    assert _offline["answers"] == []
    assert answer.text is not None and "couldn't find" in answer.text
    assert not answer.failed


# --- cards by default ----------------------------------------------------------------


async def test_the_default_answer_sees_cards_and_not_bodies(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = _with_memories(monkeypatch, [_item(n) for n in range(8)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what did I save about redis?", "555000")

    context = _offline["contexts"][0]
    assert "How Redis persistence works, part 0" in context
    assert "RDB snapshots and an append-only file" not in context
    assert limits == [8]


# --- bodies when asked ---------------------------------------------------------------


async def test_a_detail_question_sees_the_body_of_fewer_memories(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = _with_memories(monkeypatch, [_item(n) for n in range(8)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what exactly did that redis article say?", "555000")

    context = _offline["contexts"][0]
    assert "RDB snapshots and an append-only file" in context
    assert "full text:" in context
    assert limits == [2], "a detail answer reads fewer memories, more closely"


async def test_a_detail_answer_still_names_the_memory(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card stays on top: an answer that cannot say *which* memory is useless."""
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what exactly did that article say?", "555000")

    assert "How Redis persistence works, part 0" in _offline["contexts"][0]


async def test_every_block_is_still_fenced_as_quoted_material(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what exactly did that article say?", "555000")

    context = _offline["contexts"][0]
    assert context.startswith("<memory id=") and context.endswith("</memory>")
