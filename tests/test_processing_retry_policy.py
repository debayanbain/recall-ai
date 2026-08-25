"""Which failures ARQ should retry, and which cost money to retry.

ARQ retries a failed job four times. That is right for a timeout or a 5xx and wrong for a
deleted Instagram post: the answer never changes and every attempt spends a paid actor
run. `ProcessingService` therefore swallows `PermanentExtractionError` after recording it.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.extractors.base import ExtractedContent, PermanentExtractionError
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services import processing_service as ps


class _Repo:
    def __init__(self, item: VaultItem) -> None:
        self.item = item

    async def get_unscoped(self, _id: uuid.UUID) -> VaultItem:
        return self.item

    async def add(self, item: VaultItem) -> VaultItem:
        return item

    async def upsert_chunk(self, **_: Any) -> None: ...


def _item() -> VaultItem:
    return VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.article,
        source_url="https://www.instagram.com/reel/C1x/",
        processing_status=ProcessingStatus.pending,
    )


def _extractor_raising(exc: Exception) -> Any:
    class _E:
        content_type = ContentType.instagram

        def can_handle(self, _url: str) -> bool:
            return True

        async def extract(self, _url: str) -> ExtractedContent:
            raise exc

    return _E()


async def test_permanent_failure_is_recorded_and_not_re_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    monkeypatch.setattr(
        ps, "get_extractor", lambda _u: _extractor_raising(PermanentExtractionError("gone"))
    )
    service = ps.ProcessingService(_Repo(item))

    await service.process(uuid.uuid4())  # must NOT raise -> ARQ will not retry

    assert item.processing_status is ProcessingStatus.failed
    assert "gone" in (item.processing_error or "")


async def test_transient_failure_is_re_raised_so_arq_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    monkeypatch.setattr(
        ps, "get_extractor", lambda _u: _extractor_raising(TimeoutError("slow"))
    )
    service = ps.ProcessingService(_Repo(item))

    with pytest.raises(TimeoutError):
        await service.process(uuid.uuid4())

    assert item.processing_status is ProcessingStatus.failed
    assert item.retry_count == 1
