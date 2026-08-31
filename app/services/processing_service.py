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
from app.ai.spans import keep_verbatim
from app.core.config import settings
from app.core.errors import safe_error_text
from app.core.logging import get_logger
from app.extractors import get_extractor
from app.extractors.base import ExtractedContent, PermanentExtractionError
from app.models.base import ContentType, ProcessingStatus
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.vault import VaultItem
from app.repositories.extraction_run import ExtractionRunRepository
from app.repositories.vault import VaultRepository
from app.services import transcription, vision
from app.storage import ObjectStorage, StorageError

log = get_logger("processing")


class ProcessingService:
    def __init__(
        self,
        repo: VaultRepository,
        runs: ExtractionRunRepository | None = None,
        storage: ObjectStorage | None = None,
    ) -> None:
        self.repo = repo
        self.runs = runs
        # Only needed to read an uploaded image back for the vision pass. None when no
        # bucket is configured, which is also the only case in which an image cannot
        # have been stored in the first place.
        self.storage = storage
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

            elif item.type is ContentType.voice and not item.content:
                # A re-transcription. The ordinary path transcribes at save time, so
                # reaching here means the words were cleared on purpose -- `reprocess`
                # does that for a voice note, which is how a misheard language is fixed
                # without asking the user to record the whole thing again.
                if not await self._transcribe(item):
                    await self._skip(item)
                    return None

            elif item.type is ContentType.image and not item.content:
                # An uploaded picture with nothing to read yet. The description becomes
                # `content`, so the ordinary pipeline can summarise, tag and embed it --
                # which is the whole difference between a findable memory and a file.
                if not await self._describe(item):
                    await self._skip(item)
                    return None

            await self._enrich(item)
            return None

        except (transcription.TranscriptionError, vision.VisionError):
            # "This cannot be read" is an answer, not a fault -- an inaudible clip or an
            # unreadable picture. `skipped` keeps the file downloadable and stops Celery
            # paying for three more attempts at the same silence.
            await self._skip(item)
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
        item.ai_label = await self.ai.generate_label(text) or None
        # Highlights index into `content` specifically, so they are only asked for when
        # there is content to index into -- an item enriched from its title alone has
        # nothing for the reader to mark. Each span is then checked against that text
        # before storage: one the model paraphrased would either vanish in the UI or be
        # shown as words the author never wrote.
        item.ai_highlights = (
            keep_verbatim(await self.ai.generate_highlights(item.content), item.content)
            if item.content
            else []
        )

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

    async def _transcribe(self, item: VaultItem) -> bool:
        """Re-read a stored recording into `content`. False means "nothing to enrich".

        Reuses the language the item was saved with (`item_metadata`), so a note the user
        pinned to Bengali stays pinned when it is re-run. Same failure split as the vision
        pass: an unusable clip is `skipped`, a provider or bucket fault is raised.
        """
        if not transcription.transcription_enabled():
            log.info("transcribe_unconfigured", item_id=str(item.id))
            return False
        if self.storage is None or not item.storage_key:
            # The audio was never stored, so there is nothing left to re-read. The words
            # are gone with it -- which is why `reprocess` only clears them when there is
            # an object to go back to.
            log.info("transcribe_no_object", item_id=str(item.id))
            return False

        data = await self.storage.download(item.storage_key)
        language = transcription.normalise_language(
            str(item.item_metadata.get("transcribe_language") or "")
        )
        clip = transcription.inspect(data)
        transcript = await transcription.transcribe(clip, language)

        item.content = transcript.text
        item.item_metadata = {
            **item.item_metadata,
            "transcript_language": transcript.language,
            "transcript_model": transcript.model,
        }
        log.info(
            "transcribe_applied",
            item_id=str(item.id),
            language=transcript.language,
            chars=len(transcript.text),
        )
        return True

    async def _describe(self, item: VaultItem) -> bool:
        """Read an uploaded image into `content`. False means "nothing to enrich".

        The distinction that matters here is between an image that *cannot* be read and
        one that could not be read *this time*. An unsupported format, an oversized file
        or a picture the model found nothing in are answers -- the item is `skipped`, and
        retrying spends another reading to reach the same place. A provider fault or an
        unreachable bucket is a failure, so it is raised and Celery retries.
        """
        if not vision.vision_enabled():
            log.info("vision_unconfigured", item_id=str(item.id))
            return False
        if self.storage is None or not item.storage_key:
            # Nothing to read back. Not an error: an image with no stored object is one
            # that was saved while the bucket was unavailable.
            log.info("vision_no_object", item_id=str(item.id))
            return False

        try:
            data = await self.storage.download(item.storage_key)
        except StorageError:
            raise  # transient by nature; let Celery try again
        description = await vision.describe_image(data, item.mime_type)

        item.content = description
        # The reader shows this: `content` here is a machine's account of a picture, not
        # words the user wrote, and presenting the two identically is the one way this
        # feature can lie.
        item.item_metadata = {
            **item.item_metadata,
            "content_source": "vision",
            "vision_model": settings.OPENAI_VISION_MODEL,
        }
        log.info("vision_applied", item_id=str(item.id), chars=len(description))
        return True

    async def _skip(self, item: VaultItem) -> None:
        item.processing_status = ProcessingStatus.skipped
        item.retry_count = 0
        await self.repo.add(item)
        log.info("process_skipped", item_id=str(item.id), type=item.type.value)

    async def _fail(self, item: VaultItem, exc: Exception) -> None:
        item.processing_status = ProcessingStatus.failed
        # Scrubbed on the way in, not on the way out: this string is read back by the
        # item's owner, copied into support tickets and pasted into issues, and an httpx
        # error carries the whole request URL -- which for Apify holds a live token.
        item.processing_error = safe_error_text(exc)
        item.retry_count = (item.retry_count or 0) + 1
        await self.repo.add(item)
        if isinstance(exc, PermanentExtractionError):
            log.warning(
                "process_failed_permanently", item_id=str(item.id), error=str(exc)[:200]
            )
        else:
            log.exception("process_failed", item_id=str(item.id))
