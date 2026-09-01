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

import asyncio
import uuid
from typing import Any

from app.ai import enrichment, get_ai_provider
from app.ai.spans import keep_verbatim
from app.core.config import settings
from app.core.errors import safe_error_text
from app.core.links import collect_links
from app.core.logging import get_logger
from app.core.net import UnsafeUrlError
from app.extractors import get_extractor
from app.extractors.base import ExtractedContent, PermanentExtractionError
from app.models.base import ContentType, ProcessingStatus
from app.models.extraction_run import ExtractionRun, RunStatus
from app.models.vault import VaultItem
from app.repositories.extraction_run import ExtractionRunRepository
from app.repositories.vault import VaultRepository
from app.services import transcription, video, video_doc, vision
from app.storage import ObjectStorage, StorageError

#: Ceiling on a body assembled from more than one source (a caption plus a video
#: reading). Each half is already clipped by whoever produced it; this bounds the
#: sum, which is what actually reaches a prompt and the embedding input.
_MAX_COMBINED_CONTENT_CHARS = 16_000

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
                if extracted.enrich:
                    await self._read_video(item)
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
            # Before `_enrich`, so the summary, tags, label and embedding are all drawn
            # from what the video actually said rather than from the caption alone.
            await self._read_video(item)
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

        # The card fields and the highlights are independent readings of the same text,
        # so they are issued together. Highlights index into `content` specifically and
        # are checked against it afterwards, which is why they are never folded into the
        # combined call below: a quote that was paraphrased would either vanish in the UI
        # or be shown as words the author never wrote.
        fields, highlights = await asyncio.gather(
            self._card_fields(text),
            self._highlights_for(item),
        )
        item.summary = fields.summary
        item.ai_tags = fields.tags
        item.ai_category = fields.category
        item.ai_label = fields.label or None
        item.ai_highlights = highlights

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

    async def _card_fields(self, text: str) -> enrichment.Enrichment:
        """Summary, tags, category and label -- in one call where that is possible.

        The combined call ships the item once instead of four times, which is where the
        cost of enrichment actually lives, and its answer is schema-checked rather than
        recovered from prose. It is never the only path: unconfigured, or failed, and
        this drops to the four `AIProvider` calls, which is the behaviour that existed
        before it and is what every provider implements.

        A failure here is logged and swallowed *once*. That is deliberate and narrow: the
        fallback is a complete implementation of the same job, so paying for it beats
        failing an item over a capability that is an optimisation.
        """
        if enrichment.enrichment_available():
            try:
                return await enrichment.enrich(text)
            except Exception as exc:  # noqa: BLE001 - the fallback below does the job
                log.warning("enrichment_combined_failed", error=type(exc).__name__)

        # Four calls, issued together. They were sequential, which made enrichment cost
        # the sum of four provider round trips when it only ever needed the slowest --
        # none of them reads another's output. `return_exceptions=False` keeps the
        # original failure behaviour: the first exception propagates, `process` records
        # it and re-raises, and Celery retries the whole item.
        summary, tags, category, label = await asyncio.gather(
            self.ai.generate_summary(text),
            self.ai.generate_tags(text),
            self.ai.generate_category(text),
            self.ai.generate_label(text),
        )
        return enrichment.Enrichment(
            summary=summary, tags=list(tags), category=category, label=label
        )

    async def _highlights_for(self, item: VaultItem) -> list[str]:
        """Verbatim quotes from the item's own body, or nothing when there is no body.

        Its own coroutine so the empty case is still awaitable and can sit in the
        `gather` above beside the other four -- a conditional there would either be a
        branch around the whole call or an `if` inside an argument list.
        """
        if not item.content:
            return []
        spans = await self.ai.generate_highlights(item.content)
        return keep_verbatim(spans, item.content)

    async def _read_video(self, item: VaultItem) -> None:
        """Read a source's video, when it gave us one, and fold it into the item.

        A caption is not a video. The link a creator wants followed is usually burned into
        a frame or spoken aloud, and neither reaches the caption -- so without this the
        useful half of every reel is missing and nothing reports it.

        Appends rather than replaces, because the caption is what its author chose to
        write and the reading is a machine's account of what it saw. Both are kept, both
        are labelled, and `content_source` records that a model wrote part of this.

        The failure policy is the interesting part, and it turns on one question: was the
        video the *only* content?

        * Unreadable, oversized, expired, over-long, or pointing somewhere we refuse to
          fetch -- all answers, not faults. The item keeps its caption and completes.
        * A provider or network fault when a caption exists -- degrade. Failing an item
          whose caption is a perfectly good memory, to retry an enhancement, trades a
          working memory for a spinner.
        * The same fault with no caption at all -- re-raised, so Celery retries. There is
          nothing to degrade *to*, and enriching an empty item is how a memory ends up
          summarising its own URL.
        """
        raw_url = item.item_metadata.get("video_url")
        if not isinstance(raw_url, str) or not raw_url.strip():
            return
        if not video.video_understanding_enabled():
            log.info("video_unconfigured", item_id=str(item.id))
            return

        # Captured before anything is appended: this is the author's own text, and it is
        # the most trustworthy source of links on the item.
        caption = (item.content or "").strip()
        reading: video.VideoReading | None = None
        error: str | None = None

        try:
            data = await video.fetch_video(raw_url)
            reading = await video.read_video(data)
        except (video.VideoError, video.VideoUnavailable, UnsafeUrlError) as exc:
            error = type(exc).__name__
            log.info("video_not_read", item_id=str(item.id), error=error)
        except video.VideoFailed:
            if not caption:
                raise
            error = "VideoFailed"
            log.warning("video_read_degraded", item_id=str(item.id))

        # Ordered by trust, most trusted first, and `collect_links` keeps the first
        # source to yield each URL. A link the creator typed is a fact; the same link
        # read off a blurry frame is a guess that happens to be right, and the reader has
        # to be able to tell them apart.
        links = collect_links(
            [
                ("caption", caption),
                ("video", reading.frames_text if reading else None),
                ("speech", reading.speech if reading else None),
            ],
            limit=settings.MAX_EXTRACTED_LINKS,
        )

        metadata: dict[str, Any] = {
            **item.item_metadata,
            "links": links,
            "video_read": bool(reading and reading.text),
        }
        if error:
            metadata["video_read_error"] = error

        if reading and reading.text:
            body = "\n\n".join(part for part in (caption, reading.text) if part)
            item.content = body[:_MAX_COMBINED_CONTENT_CHARS]
            # The reading has real structure -- a caption, a transcript, an itemised list
            # of what was on screen -- which the flattening above throws away. Rebuilt as
            # a block document so the page can render five title cards as five list items
            # rather than as one paragraph.
            #
            # Stored under its OWN key. `editor_doc` means "a person edited this": the
            # Edit button seeds from it, and a machine-written document landing there
            # would be indistinguishable from the user's own work and would be silently
            # overwritten by the next re-read.
            document = video_doc.build(item.content, links)
            if document:
                metadata["video_doc"] = document
            # The reader shows this. Part of this body is a model's account of a video,
            # not words a person wrote, and rendering the two identically is the one way
            # this feature can lie.
            metadata["content_source"] = "video"
            metadata["video_model"] = settings.OPENAI_VISION_MODEL
            metadata["video_frames_read"] = reading.frames_read
            if reading.duration_seconds is not None:
                metadata["video_duration_seconds"] = round(reading.duration_seconds, 2)

        item.item_metadata = metadata
        log.info(
            "video_pass_done",
            item_id=str(item.id),
            read=bool(reading and reading.text),
            links=len(links),
        )

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
