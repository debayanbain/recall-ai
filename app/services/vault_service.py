"""Vault use-cases: save URL/note, list, get, search, delete.

Saving NEVER blocks on AI — it stores the item, enqueues a job, returns fast.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.logging import get_logger
from app.core.urls import canonical_url
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.queue.client import enqueue_process_item
from app.repositories.vault import VaultRepository
from app.services.pdf import extract_text

log = get_logger("vault.service")


class VaultService:
    def __init__(self, repo: VaultRepository) -> None:
        self.repo = repo

    async def save_url(
        self, user_id: uuid.UUID, url: str, title: str | None = None
    ) -> tuple[VaultItem, bool]:
        """Save a URL. Returns (item, created) — `created` is False for a duplicate.

        Re-saving a link someone already has is almost always an accident (a second
        share, a re-paste), and each one otherwise costs another paid scrape and another
        round of AI calls. The URL is canonicalised first so `?igsi=…` share noise does
        not make the same reel look like a new link.
        """
        canonical = canonical_url(url)
        existing = await self.repo.get_by_source_url(user_id, canonical)
        if existing is not None:
            log.info(
                "vault_url_duplicate", item_id=str(existing.id), user_id=str(user_id)
            )
            return existing, False

        item = VaultItem(
            user_id=user_id,
            type=ContentType.article,  # refined by worker via extractor detection
            source_url=canonical,
            title=title,
            processing_status=ProcessingStatus.pending,
        )
        item = await self.repo.add(item)
        await self._enqueue(item)
        log.info("vault_url_saved", item_id=str(item.id), user_id=str(user_id))
        return item, True

    async def save_pdf(
        self, user_id: uuid.UUID, data: bytes, filename: str | None
    ) -> VaultItem:
        """Store an uploaded PDF as text and let the normal pipeline enrich it.

        The binary is deliberately discarded: the text is what gets summarized, tagged
        and embedded, so keeping the file would add a storage dependency for no gain.
        """
        text, meta = extract_text(data, filename)
        item = VaultItem(
            user_id=user_id,
            type=ContentType.pdf,
            source_url=None,
            title=str(meta.get("pdf_title") or filename or "Uploaded PDF"),
            content=text,
            item_metadata=meta,
            processing_status=ProcessingStatus.pending,
        )
        item = await self.repo.add(item)
        await self._enqueue(item)
        log.info(
            "vault_pdf_saved",
            item_id=str(item.id),
            user_id=str(user_id),
            pages=meta.get("pages"),
        )
        return item

    async def _enqueue(self, item: VaultItem) -> None:
        """Best-effort hand-off to the worker.

        The row is already persisted with `processing_status=pending`, so the queue is an
        accelerator, not the source of truth. A Redis outage must not turn a successful
        save into a 500 that loses the user's capture -- the item simply stays `pending`
        until a worker picks it up.

        Note this still fires before the request session commits (see "Known rough edges"
        in CLAUDE.md): a fast worker can dequeue before the row is visible and log
        `process_missing_item`. Failing soft here does not fix that race, it only stops a
        missing queue from breaking capture entirely.
        """
        try:
            await enqueue_process_item(item.id)
        except Exception as exc:  # noqa: BLE001 - never let the queue break a save
            log.warning(
                "vault_enqueue_failed",
                item_id=str(item.id),
                error=type(exc).__name__,
                detail="saved as pending; nothing processes it until the queue is reachable",
            )


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
        await self._enqueue(item)  # AI still summarizes/tags/embeds notes
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
