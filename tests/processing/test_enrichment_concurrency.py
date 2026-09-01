"""Enrichment issues its provider calls together, and fails exactly as it used to.

Five readings of the same text -- summary, tags, category, label, highlights -- and none
of them reads another's output. Run in sequence they cost the sum of five round trips to
a provider in another region; run together they cost the slowest one. That is the entire
change, and the tests that matter are the two that say it did not cost anything: a
failure still propagates so Celery retries the item, and the results still land on the
right fields.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.processing_service import ProcessingService


class _Repo:
    def __init__(self, item: VaultItem) -> None:
        self.item = item
        self.chunks: list[dict[str, Any]] = []

    async def get_unscoped(self, _item_id: uuid.UUID) -> VaultItem:
        return self.item

    async def add(self, item: VaultItem) -> VaultItem:
        return item

    async def upsert_chunk(self, **kwargs: Any) -> None:
        self.chunks.append(kwargs)


class _AI:
    """Records how many enrichment calls are in flight at once."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.in_flight = 0
        self.peak = 0
        self.started: list[str] = []

    async def _call(self, name: str, result: Any) -> Any:
        self.started.append(name)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            # One event-loop turn each, so a sequential implementation cannot overlap
            # and a concurrent one always does. No wall-clock assertion anywhere -- a
            # timing test on a loaded machine is a flaky test.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if self.fail == name:
                raise RuntimeError(f"{name} is down")
            return result
        finally:
            self.in_flight -= 1

    async def generate_summary(self, _text: str) -> str:
        return await self._call("summary", "A note about Redis.")

    async def generate_tags(self, _text: str) -> list[str]:
        return await self._call("tags", ["redis", "databases"])

    async def generate_category(self, _text: str) -> str:
        return await self._call("category", "Technology")

    async def generate_label(self, _text: str) -> str:
        return await self._call("label", "How Redis persists")

    async def generate_highlights(self, _text: str) -> list[str]:
        return await self._call("highlights", ["Redis persists to an append-only file."])

    async def generate_embedding(self, _text: str) -> list[float]:
        return await self._call("embedding", [0.0] * 8)


def _item(content: str | None = "Redis persists to an append-only file.") -> VaultItem:
    return VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.note,
        title="Redis",
        content=content,
        processing_status=ProcessingStatus.pending,
    )


def _service(item: VaultItem, ai: _AI) -> ProcessingService:
    service = ProcessingService(_Repo(item), None, None)  # type: ignore[arg-type]
    service.ai = ai  # type: ignore[assignment]
    return service


async def test_the_five_readings_run_together() -> None:
    item = _item()
    ai = _AI()

    await _service(item, ai)._enrich(item)

    assert ai.peak == 5


async def test_the_embedding_still_waits_for_the_summary() -> None:
    """It is built from the summary, so it is the one call that genuinely has an order.

    Folding it into the same gather would embed an empty string -- and nothing would
    report it, because an embedding of the wrong text is a vector, not an error.
    """
    item = _item()
    ai = _AI()
    service = _service(item, ai)

    await service._enrich(item)

    assert ai.started[-1] == "embedding"
    chunk = service.repo.chunks[0]  # type: ignore[attr-defined]
    assert "A note about Redis." in chunk["content"]


async def test_every_result_lands_on_its_own_field() -> None:
    """`gather` returns in argument order, not completion order -- the failure mode of
    getting this wrong is a summary stored as a label, which nothing would flag."""
    item = _item()

    await _service(item, _AI())._enrich(item)

    assert item.summary == "A note about Redis."
    assert item.ai_tags == ["redis", "databases"]
    assert item.ai_category == "Technology"
    assert item.ai_label == "How Redis persists"
    assert item.ai_highlights == ["Redis persists to an append-only file."]
    assert item.processing_status is ProcessingStatus.completed


@pytest.mark.parametrize("failing", ["summary", "tags", "category", "label", "highlights"])
async def test_any_failure_still_propagates(failing: str) -> None:
    """`return_exceptions=False` on purpose. `process` records the error and re-raises so
    Celery retries; swallowing one call would store a half-enriched item as completed."""
    item = _item()

    with pytest.raises(RuntimeError):
        await _service(item, _AI(fail=failing))._enrich(item)

    assert item.processing_status is not ProcessingStatus.completed


async def test_an_item_with_no_body_asks_for_no_highlights() -> None:
    """Highlights index into `content`; an item enriched from its title alone has
    nothing for them to index into."""
    item = _item(content=None)
    item.title = "Just a title"
    ai = _AI()

    await _service(item, ai)._enrich(item)

    assert "highlights" not in ai.started
    assert item.ai_highlights == []
