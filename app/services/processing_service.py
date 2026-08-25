"""The enrichment pipeline: extract -> summarize -> tag -> categorize -> embed.

Two shapes, one pipeline:

* **Immediate** — an article or a YouTube oEmbed answers in well under a second, so it is
  fetched inline and the whole item completes in one task.
* **Deferred** — an Apify crawl can run for minutes. Holding a worker for that is the
  thing this design exists to avoid, so `process` only *triggers* the run and returns.
  The provider calls back later (or the sweeper notices) and `finalize` finishes the job
  with the delivered payload.

`processing_status` deliberately stays on the existing enum: a deferred item sits in
`processing` while the provider works, so the API contract and the frontend's polling are
unchanged by the split.
"""
from __future__ import annotations

import uuid

from app.ai import get_ai_provider
from app.core.logging import get_logger
from app.extractors import get_extractor
from app.extractors.base import ExtractedContent, PermanentExtractionError
from app.models.base import ProcessingStatus
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.vault import VaultItem
from app.repositories.extraction_run import ExtractionRunRepository
from app.repositories.vault import VaultRepository

log = get_logger("processing")


class ProcessingService:
    def __init__(
        self, repo: VaultRepository, runs: ExtractionRunRepository | None = None
    ) -> None:
        self.repo = repo
        self.runs = runs
        self.ai = get_ai_provider()

    # --- phase 1 -------------------------------------------------------------------

    async def process(self, item_id: uuid.UUID) -> str | None:
        """Run the pipeline, or trigger a deferred extraction.

        Returns the provider's run id when extraction was handed off, else None.
        """
        item = await self.repo.get_unscoped(item_id)
        if item is None:
            log.warning("process_missing_item", item_id=str(item_id))
            return None

        item.processing_status = ProcessingStatus.processing
        item.processing_error = None
        await self.repo.add(item)

        try:
            if item.source_url:
                extractor = get_extractor(item.source_url)
                if getattr(extractor, "deferred", False):
                    run_id = await extractor.start(item.source_url)  # type: ignore[union-attr]
                    await self._record_run(item, run_id)
                    log.info(
                        "extraction_deferred", item_id=str(item_id), provider_run_id=run_id
                    )
                    # Returning here is the point: the worker is free again while the
                    # provider crawls. `finalize` picks the item back up on callback.
                    return run_id

                extracted = await extractor.extract(item.source_url)  # type: ignore[union-attr]
                self._apply(item, extracted)
                if not extracted.enrich:
                    # Deliberately unenriched (an Instagram post we do not pay to scrape).
                    # `skipped`, not `failed`: nothing went wrong, there is just nothing
                    # for the model to read, and calling it anyway would spend tokens
                    # hallucinating about a URL.
                    await self._skip(item)
                    return None

            await self._enrich(item)
            return None

        except Exception as exc:  # noqa: BLE001 - record, then decide about retrying
            await self._fail(item, exc)
            if isinstance(exc, PermanentExtractionError):
                return None
            raise

    # --- phase 2 -------------------------------------------------------------------

    async def finalize(self, item_id: uuid.UUID, items: list[dict[str, object]]) -> None:
        """Complete a deferred item from the payload its run produced."""
        item = await self.repo.get_unscoped(item_id)
        if item is None:
            log.warning("finalize_missing_item", item_id=str(item_id))
            return
        if item.processing_status is ProcessingStatus.completed:
            # The webhook is at-least-once and the sweeper races it; doing the AI work
            # twice would double the bill for an identical result.
            log.info("finalize_already_complete", item_id=str(item_id))
            return

        try:
            extractor = get_extractor(item.source_url or "")
            self._apply(item, extractor.build(items))  # type: ignore[union-attr]
            await self._enrich(item)
        except Exception as exc:  # noqa: BLE001 - same policy as phase 1
            await self._fail(item, exc)
            if isinstance(exc, PermanentExtractionError):
                return
            raise

    async def fetch_payload(
        self, item_id: uuid.UUID, dataset_id: str
    ) -> list[dict[str, object]]:
        """Read a finished run's payload using whichever extractor owns the URL.

        Resolved from the registry rather than hardcoded: the moment there are two
        deferred sources, a hardcoded extractor silently reads the wrong one's dataset.
        """
        item = await self.repo.get_unscoped(item_id)
        if item is None or not item.source_url:
            raise RuntimeError("cannot fetch a payload for an item with no source URL")
        extractor = get_extractor(item.source_url)
        return await extractor.fetch_dataset(dataset_id)  # type: ignore[union-attr]

    async def fail_item(self, item_id: uuid.UUID, reason: str) -> None:
        """Mark a deferred item failed when its run never produced anything."""
        item = await self.repo.get_unscoped(item_id)
        if item is None or item.processing_status is ProcessingStatus.completed:
            return
        await self._fail(item, RuntimeError(reason))

    # --- shared --------------------------------------------------------------------

    async def _record_run(self, item: VaultItem, provider_run_id: str) -> None:
        if self.runs is None:
            raise RuntimeError("deferred extraction needs an ExtractionRunRepository")
        await self.runs.add(
            ExtractionRun(
                vault_item_id=item.id,
                provider="apify",
                provider_run_id=provider_run_id,
                status=RunStatus.running,
            )
        )

    @staticmethod
    def _apply(item: VaultItem, extracted: ExtractedContent) -> None:
        item.type = extracted.type
        item.title = item.title or extracted.title
        item.content = extracted.content
        item.thumbnail_url = extracted.thumbnail_url
        item.item_metadata = {**item.item_metadata, **extracted.metadata}

    async def _enrich(self, item: VaultItem) -> None:
        text = item.content or item.title or ""
        if not text.strip():
            raise ValueError("No content extracted to process")

        item.summary = await self.ai.generate_summary(text)
        item.ai_tags = await self.ai.generate_tags(text)
        item.ai_category = await self.ai.generate_category(text)

        embed_input = f"{item.title or ''}\n{item.summary or ''}\n{text}"
        vector = await self.ai.generate_embedding(embed_input)
        await self.repo.upsert_chunk(
            item_id=item.id,
            user_id=item.user_id,
            vector=vector,
            content=embed_input[:8000],
        )

        item.processing_status = ProcessingStatus.completed
        item.retry_count = 0
        await self.repo.add(item)
        log.info("process_completed", item_id=str(item.id), type=item.type.value)

    async def _skip(self, item: VaultItem) -> None:
        item.processing_status = ProcessingStatus.skipped
        item.retry_count = 0
        await self.repo.add(item)
        log.info("process_skipped", item_id=str(item.id), type=item.type.value)

    async def _fail(self, item: VaultItem, exc: Exception) -> None:
        item.processing_status = ProcessingStatus.failed
        item.processing_error = str(exc)[:500]
        item.retry_count = (item.retry_count or 0) + 1
        await self.repo.add(item)
        if isinstance(exc, PermanentExtractionError):
            log.warning(
                "process_failed_permanently", item_id=str(item.id), error=str(exc)[:200]
            )
        else:
            log.exception("process_failed", item_id=str(item.id))
