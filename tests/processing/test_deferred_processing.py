"""Two-phase extraction: trigger, then finish on callback.

The point of the split is that `process` returns while the provider is still crawling.
These tests pin that it really does return, that the correlation row is written (without
it a callback has no way home), and that finishing twice does not bill twice.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.extractors.base import ExtractedContent
from app.models.base import ContentType, ProcessingStatus
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.vault import VaultItem
from app.services import processing_service as ps


class _VaultRepo:
    def __init__(self, item: VaultItem) -> None:
        self.item = item
        self.chunks: list[Any] = []

    async def get_unscoped(self, _id: uuid.UUID) -> VaultItem:
        return self.item

    async def add(self, item: VaultItem) -> VaultItem:
        return item

    async def upsert_chunk(self, **kw: Any) -> None:
        self.chunks.append(kw)


class _RunRepo:
    def __init__(self) -> None:
        self.added: list[ExtractionRun] = []

    async def add(self, run: ExtractionRun) -> ExtractionRun:
        self.added.append(run)
        return run


class _DeferredExtractor:
    content_type = ContentType.instagram
    deferred = True

    def __init__(self) -> None:
        self.started: list[str] = []

    def can_handle(self, _url: str) -> bool:
        return True

    async def start(self, url: str) -> str:
        self.started.append(url)
        return "apify-run-1"

    def build(self, items: list[dict[str, Any]]) -> ExtractedContent:
        return ExtractedContent(
            type=ContentType.instagram,
            title="A reel",
            content=items[0]["caption"],
            metadata={"owner": "someone"},
        )


class _AI:
    async def generate_summary(self, _t: str) -> str:
        return "a summary"

    async def generate_tags(self, _t: str) -> list[str]:
        return ["tag"]

    async def generate_category(self, _t: str) -> str:
        return "Education"

    async def generate_label(self, _t: str) -> str:
        return "a distinctive label"

    async def generate_highlights(self, t: str) -> list[str]:
        # Verbatim on purpose: the service filters out anything not present in the text,
        # so a stub returning invented prose would silently store nothing.
        return [t[:60]]

    async def generate_embedding(self, _t: str) -> list[float]:
        return [0.0] * 1536


def _item() -> VaultItem:
    return VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.article,
        source_url="https://www.instagram.com/reel/x/",
        processing_status=ProcessingStatus.pending,
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    extractor = _DeferredExtractor()
    monkeypatch.setattr(ps, "get_extractor", lambda _u: extractor)
    monkeypatch.setattr(ps, "get_ai_provider", lambda: _AI())
    return extractor


async def test_process_returns_without_waiting_for_the_crawl(
    wired: _DeferredExtractor,
) -> None:
    """The whole design: the worker is free again while Apify is still running."""
    item = _item()
    runs = _RunRepo()
    service = ps.ProcessingService(_VaultRepo(item), runs)

    run_id = await service.process(uuid.uuid4())

    assert run_id == "apify-run-1"
    assert wired.started == ["https://www.instagram.com/reel/x/"]
    # Not completed and not failed — genuinely in flight.
    assert item.processing_status is ProcessingStatus.processing
    # No AI was called: there is nothing to summarize yet.
    assert item.summary is None


async def test_the_correlation_row_is_written(wired: _DeferredExtractor) -> None:
    """Without it, a webhook naming a run id has no way back to the item."""
    item = _item()
    runs = _RunRepo()
    await ps.ProcessingService(_VaultRepo(item), runs).process(uuid.uuid4())

    assert len(runs.added) == 1
    run = runs.added[0]
    assert run.provider_run_id == "apify-run-1"
    assert run.vault_item_id == item.id
    assert run.status == RunStatus.running


async def test_finalize_completes_the_item_from_the_payload(
    wired: _DeferredExtractor,
) -> None:
    item = _item()
    item.processing_status = ProcessingStatus.processing
    repo = _VaultRepo(item)
    service = ps.ProcessingService(repo, _RunRepo())

    await service.finalize(item.id, [{"caption": "3 ways to take better notes"}])

    assert item.processing_status is ProcessingStatus.completed
    assert item.content == "3 ways to take better notes"
    assert item.summary == "a summary"
    assert item.ai_tags == ["tag"]
    assert item.ai_category == "Education"
    assert len(repo.chunks) == 1


async def test_finalizing_twice_does_not_bill_twice(wired: _DeferredExtractor) -> None:
    """The webhook is at-least-once and the sweeper races it."""
    item = _item()
    item.processing_status = ProcessingStatus.completed
    repo = _VaultRepo(item)

    await ps.ProcessingService(repo, _RunRepo()).finalize(item.id, [{"caption": "x"}])

    assert repo.chunks == []  # no second embedding, no second AI spend


async def test_a_run_that_produced_nothing_marks_the_item_failed(
    wired: _DeferredExtractor,
) -> None:
    item = _item()
    item.processing_status = ProcessingStatus.processing
    service = ps.ProcessingService(_VaultRepo(item), _RunRepo())

    await service.fail_item(item.id, "Apify run ended as ABORTED")

    assert item.processing_status is ProcessingStatus.failed
    assert "ABORTED" in (item.processing_error or "")
