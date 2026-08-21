"""URL/content processing pipeline run by the background worker.

Pipeline: detect source -> extract -> summary -> tags -> category -> embedding -> store.
Each AI step is individually retried inside the provider; the whole job is
re-enqueued by ARQ on unhandled failure. Status is persisted at every stage.
"""
from __future__ import annotations

import uuid

from app.ai import get_ai_provider
from app.core.logging import get_logger
from app.extractors import get_extractor
from app.models.base import ProcessingStatus
from app.repositories.vault import VaultRepository

log = get_logger("vault.processing")


class ProcessingService:
    def __init__(self, repo: VaultRepository) -> None:
        self.repo = repo
        self.ai = get_ai_provider()

    async def process(self, item_id: uuid.UUID) -> None:
        item = await self.repo.get_unscoped(item_id)
        if item is None:
            log.warning("process_missing_item", item_id=str(item_id))
            return

        item.processing_status = ProcessingStatus.processing
        item.processing_error = None
        await self.repo.add(item)

        try:
            # 1. Detect source + extract (skip for user notes which already have content)
            text = item.content or ""
            if item.source_url:
                extractor = get_extractor(item.source_url)
                extracted = await extractor.extract(item.source_url)
                item.type = extracted.type
                item.title = item.title or extracted.title
                item.content = extracted.content
                item.thumbnail_url = extracted.thumbnail_url
                item.item_metadata = {**item.item_metadata, **extracted.metadata}
                text = extracted.content or item.title or ""

            if not text.strip():
                raise ValueError("No content extracted to process")

            # 2-4. AI enrichment
            item.summary = await self.ai.generate_summary(text)
            item.ai_tags = await self.ai.generate_tags(text)
            item.ai_category = await self.ai.generate_category(text)

            # 5. Embedding stored as chunk 0 (supports multi-chunk RAG in future)
            embed_input = f"{item.title or ''}\n{item.summary or ''}\n{text}"
            vector = await self.ai.generate_embedding(embed_input)
            await self.repo.upsert_chunk(
                item_id=item.id,
                user_id=item.user_id,
                vector=vector,
                content=embed_input[:8000],
            )

            # 6. Mark complete
            item.processing_status = ProcessingStatus.completed
            item.retry_count = 0
            await self.repo.add(item)
            log.info("process_completed", item_id=str(item_id), type=item.type.value)

        except Exception as exc:  # noqa: BLE001 - record and re-raise for retry
            item.processing_status = ProcessingStatus.failed
            item.processing_error = str(exc)[:500]
            item.retry_count = (item.retry_count or 0) + 1
            await self.repo.add(item)
            log.exception("process_failed", item_id=str(item_id))
            raise
