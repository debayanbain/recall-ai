"""VaultService behaviour that does not need a database."""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services import vault_service as vs


class _Repo:
    def __init__(self, existing: VaultItem | None = None) -> None:
        self.added: list[VaultItem] = []
        self.existing = existing

    async def add(self, item: VaultItem) -> VaultItem:
        self.added.append(item)
        return item

    async def get_by_source_url(self, _uid: uuid.UUID, _url: str) -> VaultItem | None:
        return self.existing


async def test_save_survives_an_unreachable_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Redis outage must not lose the user's capture.

    The row is persisted before the hand-off, so the queue is an accelerator, not the
    source of truth. Re-raising here would turn a successful save into a 500.
    """
    async def boom(_: Any) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(vs, "enqueue_process_item", boom)
    repo = _Repo()
    item, created = await vs.VaultService(repo).save_url(uuid.uuid4(), "https://example.com/a")

    assert created is True
    assert item.processing_status is ProcessingStatus.pending
    assert repo.added == [item]


async def test_note_also_survives_an_unreachable_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(_: Any) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(vs, "enqueue_process_item", boom)
    repo = _Repo()
    item = await vs.VaultService(repo).create_note(uuid.uuid4(), "t", "body")
    assert item.processing_status is ProcessingStatus.pending


async def test_successful_enqueue_is_passed_the_item_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[uuid.UUID] = []

    async def ok(item_id: uuid.UUID) -> None:
        seen.append(item_id)

    monkeypatch.setattr(vs, "enqueue_process_item", ok)
    item, _ = await vs.VaultService(_Repo()).save_url(uuid.uuid4(), "https://example.com/a")
    assert seen == [item.id]


async def test_a_duplicate_url_is_not_saved_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-saving costs another paid scrape and another round of AI calls."""
    enqueued: list[uuid.UUID] = []

    async def track(item_id: uuid.UUID) -> None:
        enqueued.append(item_id)

    monkeypatch.setattr(vs, "enqueue_process_item", track)
    original = VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.instagram,
        source_url="https://www.instagram.com/reel/AAA",
        processing_status=ProcessingStatus.completed,
    )
    repo = _Repo(existing=original)

    # Same reel, arriving with Instagram's share parameter attached.
    item, created = await vs.VaultService(repo).save_url(
        original.user_id, "https://www.instagram.com/reel/AAA/?igsi=noise"
    )

    assert created is False
    assert item is original
    assert repo.added == [], "no second row"
    assert enqueued == [], "no second scrape, no second AI spend"
