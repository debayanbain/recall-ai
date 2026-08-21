"""Vault use-cases: save URL/note, list, get, search, delete.

Saving NEVER blocks on AI — it stores the item, enqueues a job, returns fast.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.logging import get_logger
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.queue.client import enqueue_process_item
from app.repositories.vault import VaultRepository

log = get_logger("vault.service")


class VaultService:
    def __init__(self, repo: VaultRepository) -> None:
        self.repo = repo

    async def save_url(
        self, user_id: uuid.UUID, url: str, title: str | None = None
    ) -> VaultItem:
        item = VaultItem(
            user_id=user_id,
            type=ContentType.article,  # refined by worker via extractor detection
            source_url=url,
            title=title,
            processing_status=ProcessingStatus.pending,
        )
        item = await self.repo.add(item)
        await enqueue_process_item(item.id)
        log.info("vault_url_saved", item_id=str(item.id), user_id=str(user_id))
        return item

    async def create_note(
        self, user_id: uuid.UUID, title: str, content: str
    ) -> VaultItem:
        item = VaultItem(
            user_id=user_id,
            type=ContentType.note,
            title=title,
            content=content,
            processing_status=ProcessingStatus.pending,
        )
        item = await self.repo.add(item)
        await enqueue_process_item(item.id)  # AI still summarizes/tags/embeds notes
        return item

    async def list(
        self, user_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[Sequence[VaultItem], int]:
        return await self.repo.list_for_user(user_id, limit, offset)

    async def get(self, item_id: uuid.UUID, user_id: uuid.UUID) -> VaultItem | None:
        return await self.repo.get(item_id, user_id)

    async def search(
        self, user_id: uuid.UUID, query: str, limit: int, offset: int
    ) -> tuple[Sequence[VaultItem], int]:
        return await self.repo.search(user_id, query, limit, offset)

    async def delete(self, item_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        item = await self.repo.get(item_id, user_id)
        if item is None:
            return False
        await self.repo.delete(item)
        return True
