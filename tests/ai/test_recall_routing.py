"""What the answering lane promises on its own.

This file used to pin the *conversation* lane -- the one with no vault access, which was
deleted when every prompt gained a capability card and a vault snapshot. Its two claims
did not both die with it. "A greeting never touches the vault" is gone on purpose: there
is one lane now and it reads the vault for every message, which is the point. "A provider
failure is a failure, never a traceback" applies to whichever lane survives, and it had no
other home, so it moved here rather than being deleted with the file.

Small talk is covered end to end by `tests/chat_engine/agent/golden/09_small_talk.yaml`,
which shows the agent doing what this lane used to.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from app.ai.chat import chain, history
from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.evidence import RetrievedMemory
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.recall_chat import RecallChatService

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item() -> VaultItem:
    return VaultItem(
        id=uuid.uuid4(),
        user_id=_USER,
        type=ContentType.article,
        title="Redis persistence",
        summary="RDB snapshots versus the append-only file.",
        source_url="https://example.com/redis",
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


class _Repo:
    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        return [_item()], 1


@pytest.fixture
def _lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-shot path, with everything around the provider stubbed."""

    async def _load(session_id: str) -> list[Any]:
        return []

    async def _append(session_id: str, question: str, reply: str) -> None:
        return None

    async def _recall(
        self: MemoryRetriever, user_id: uuid.UUID, question: str, filters: Any = None, **kw: Any
    ) -> list[RetrievedMemory]:
        return [RetrievedMemory(_item(), 0.9)]

    async def _plan(question: str) -> Any:
        from app.ai.chat.planner import MemoryQuery

        return MemoryQuery(search_text="redis")

    monkeypatch.setattr(settings, "RECALL_TOOLS_ENABLED", False)
    monkeypatch.setattr(history, "load", _load)
    monkeypatch.setattr(history, "append", _append)
    monkeypatch.setattr(MemoryRetriever, "recall", _recall)
    monkeypatch.setattr("app.services.recall_chat.planner.plan", _plan)


async def test_a_provider_failure_is_a_failure_not_a_traceback(
    _lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The surface renders `failed` as its own sentence.

    Letting the exception out would put a provider's own wording -- which can name the
    account it rejected -- in front of a person, and on the bot it would fail the Celery
    task, which redelivers.
    """

    async def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(chain, "answer", _boom)

    answer = await RecallChatService(_Repo()).answer(  # type: ignore[arg-type]
        _USER, "what did I save about redis?", "555000"
    )

    assert answer.failed and answer.text is None


async def test_an_answer_carries_the_evidence_it_was_built_from(
    _lane: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong answer stays traceable to the rows that produced it."""

    async def _answer(*args: Any, **kwargs: Any) -> str:
        return "You saved one thing about Redis."

    monkeypatch.setattr(chain, "answer", _answer)

    answer = await RecallChatService(_Repo()).answer(  # type: ignore[arg-type]
        _USER, "what did I save about redis?", "555000"
    )

    assert answer.text == "You saved one thing about Redis."
    assert answer.memory_ids
