"""The read side of RAG: scoped to one user, and stopping short of an answer.

Tenant scoping is the one property here that is worth more than the rest combined. It is
implemented in the repository, so what these tests pin is that the retriever always hands
the repository a user and never reaches for the unscoped door the worker uses.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.retrieval import (
    DEFAULT_LIMIT,
    MemoryFilters,
    MemoryRetriever,
)

_ALICE = uuid.UUID("11111111-1111-1111-1111-111111111111")
_BOB = uuid.UUID("22222222-2222-2222-2222-222222222222")


class FakeRepo:
    """Records exactly what the repository was asked for."""

    def __init__(self, rows: list[tuple[VaultItem, float]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.rows = rows or []

    async def search_semantic(
        self, user_id: uuid.UUID, vector: list[float], **kwargs: Any
    ) -> list[tuple[VaultItem, float]]:
        self.calls.append({"user_id": user_id, "vector": vector, **kwargs})
        return self.rows

    async def get_unscoped(self, item_id: uuid.UUID) -> None:  # pragma: no cover
        raise AssertionError("the chat path must never read an item unscoped")


class FakeProvider:
    def __init__(self) -> None:
        self.embedded: list[str] = []

    async def generate_embedding(self, text: str) -> list[float]:
        self.embedded.append(text)
        return [0.1, 0.2, 0.3]

    async def generate_summary(self, *a: Any, **k: Any) -> str:  # pragma: no cover
        raise AssertionError("retrieval must not call the answer model")


def _item(user_id: uuid.UUID = _ALICE) -> VaultItem:
    return VaultItem(
        user_id=user_id,
        type=ContentType.article,
        title="Redis persistence",
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def _patched(monkeypatch: Any, provider: FakeProvider) -> None:
    import app.services.chat_engine.retrieval as module

    monkeypatch.setattr(module, "get_ai_provider", lambda: provider)


# --- tenant scoping ------------------------------------------------------------------


async def test_the_search_is_always_scoped_to_the_asking_user(monkeypatch: Any) -> None:
    provider, repo = FakeProvider(), FakeRepo()
    _patched(monkeypatch, provider)

    await MemoryRetriever(repo).recall(_ALICE, "redis")  # type: ignore[arg-type]

    assert repo.calls[0]["user_id"] == _ALICE


async def test_two_users_produce_two_differently_scoped_searches(
    monkeypatch: Any,
) -> None:
    provider, repo = FakeProvider(), FakeRepo()
    _patched(monkeypatch, provider)
    retriever = MemoryRetriever(repo)  # type: ignore[arg-type]

    await retriever.recall(_ALICE, "redis")
    await retriever.recall(_BOB, "redis")

    assert [c["user_id"] for c in repo.calls] == [_ALICE, _BOB]


def test_the_chat_path_never_calls_the_unscoped_reader() -> None:
    """`get_unscoped` skips the tenant check and exists only for the worker.

    Matches the *call* (`.get_unscoped(`) rather than the bare name, so a module may
    still explain in prose why it does not use one -- which `retrieval.py` does.
    """
    root = Path(__file__).resolve().parents[2]
    watched = [
        *(root / "app" / "services" / "chat_engine").rglob("*.py"),
        root / "app" / "services" / "recall_chat.py",
        *(root / "app" / "ai" / "chat").rglob("*.py"),
    ]
    offenders = [
        path.name
        for path in watched
        if ".get_unscoped(" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# --- what it does and does not do ----------------------------------------------------


async def test_the_question_is_embedded_by_the_provider_that_wrote_the_vectors(
    monkeypatch: Any,
) -> None:
    """A second embedding stack would rank against a space it was not drawn from."""
    provider, repo = FakeProvider(), FakeRepo()
    _patched(monkeypatch, provider)

    await MemoryRetriever(repo).recall(_ALICE, "redis persistence")  # type: ignore[arg-type]

    assert provider.embedded == ["redis persistence"]
    assert repo.calls[0]["vector"] == [0.1, 0.2, 0.3]


async def test_rows_come_back_as_items_carrying_their_score(
    monkeypatch: Any,
) -> None:
    """The distance travels with the row rather than being dropped here.

    Nothing downstream can reconstruct it -- the query vector is gone by then -- and
    without it "nothing matched" and "eight weak matches" look identical to every later
    stage. Judging what the number *means* is `evidence.assess`, not this module.
    """
    item = _item()
    provider, repo = FakeProvider(), FakeRepo([(item, 0.12)])
    _patched(monkeypatch, provider)

    memories = await MemoryRetriever(repo).recall(_ALICE, "redis")  # type: ignore[arg-type]

    assert [memory.item for memory in memories] == [item]
    assert memories[0].score == pytest.approx(0.88)


async def test_filters_narrow_and_are_passed_through(monkeypatch: Any) -> None:
    provider, repo = FakeProvider(), FakeRepo()
    _patched(monkeypatch, provider)
    cutoff = datetime(2026, 8, 1, tzinfo=UTC)

    await MemoryRetriever(repo).recall(  # type: ignore[arg-type]
        _ALICE,
        "redis",
        MemoryFilters(
            created_after=cutoff,
            content_types=[ContentType.article],
            category="Technology",
        ),
    )

    call = repo.calls[0]
    assert call["created_after"] == cutoff
    assert call["content_types"] == [ContentType.article]
    assert call["category"] == "Technology"


async def test_no_filters_means_no_narrowing_rather_than_a_default(
    monkeypatch: Any,
) -> None:
    provider, repo = FakeProvider(), FakeRepo()
    _patched(monkeypatch, provider)

    await MemoryRetriever(repo).recall(_ALICE, "redis")  # type: ignore[arg-type]

    call = repo.calls[0]
    assert call["created_after"] is None
    assert call["content_types"] is None and call["category"] is None
    assert call["limit"] == DEFAULT_LIMIT


async def test_a_caller_may_ask_for_fewer(monkeypatch: Any) -> None:
    """A detail question reads two memories closely rather than skimming eight."""
    provider, repo = FakeProvider(), FakeRepo()
    _patched(monkeypatch, provider)

    await MemoryRetriever(repo).recall(_ALICE, "redis", limit=2)  # type: ignore[arg-type]

    assert repo.calls[0]["limit"] == 2
