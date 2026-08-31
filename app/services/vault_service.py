"""Vault use-cases: save URL/note, list, get, search, delete.

Saving NEVER blocks on AI — it stores the item, enqueues a job, returns fast.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.ai.spans import keep_verbatim
from app.core.config import settings
from app.core.logging import get_logger
from app.core.urls import canonical_url
from app.models.base import ContentType, ProcessingStatus, utcnow
from app.models.vault import VaultItem
from app.queue.client import enqueue_process_item
from app.repositories.vault import VaultRepository
from app.services import documents, editor_doc, transcription, vision
from app.services.documents import DocumentError
from app.storage import ObjectStorage

log = get_logger("vault.service")


class ItemNotFound(LookupError):
    """No such item for this user. The route cannot tell that from "not yours"."""


class ReprocessError(ValueError):
    """A retry that will not be run. The message is written for the person who asked."""


#: `VaultItem.title` is a 512-char column; clip rather than let the insert fail.
_TITLE_MAX = 512


class VaultService:
    def __init__(self, repo: VaultRepository, storage: ObjectStorage | None = None) -> None:
        self.repo = repo
        # None when no bucket is configured. Uploads then still work for PDFs and text
        # files (their *text* is the memory), and are refused for anything binary.
        self.storage = storage

    async def save_url(
        self,
        user_id: uuid.UUID,
        url: str,
        title: str | None = None,
        *,
        enqueue: bool = True,
        extra_metadata: dict[str, Any] | None = None,
    ) -> tuple[VaultItem, bool]:
        """Save a URL. Returns (item, created) — `created` is False for a duplicate.

        `enqueue=False` is for callers that own their own transaction and must commit
        before the worker can see the row -- see `_enqueue` for the race that avoids.
        `extra_metadata` records where the save came from; a duplicate keeps the metadata
        of the original capture rather than being relabelled by the second one.

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
            item_metadata=dict(extra_metadata or {}),
            processing_status=ProcessingStatus.pending,
        )
        item = await self.repo.add(item)
        if enqueue:
            await self._enqueue(item)
        log.info("vault_url_saved", item_id=str(item.id), user_id=str(user_id))
        return item, True

    async def save_document(
        self,
        user_id: uuid.UUID,
        data: bytes,
        filename: str | None,
        *,
        enqueue: bool = True,
        extra_metadata: dict[str, Any] | None = None,
    ) -> VaultItem:
        """Store an uploaded file in the bucket and index whatever text it carries.

        Two things happen and only one of them is guaranteed: the *file* is kept in B2 so
        the user can download it later, and the *text* — PDFs and plain-text formats only —
        goes through the normal AI pipeline. An image or a .docx is stored and retrievable
        but marked `skipped`, because there is nothing for the model to read and calling it
        anyway would spend tokens hallucinating about a filename.

        The object is uploaded *before* the row is inserted. A failed upload then leaves no
        row at all, which is the honest outcome; the reverse order leaves a vault item
        pointing at a file that was never stored.
        """
        document = documents.inspect(data, filename)
        text, meta = documents.extract_text(document)
        meta.update(extra_metadata or {})

        # An image carries no text, but it is not therefore unreadable: the worker hands
        # it to a vision model, and the description becomes the body that gets summarised,
        # tagged and embedded. Decided here rather than in the worker so a format nothing
        # can read is marked `skipped` immediately, instead of making a round trip through
        # the queue to be skipped there.
        readable_image = document.is_image and vision.can_describe(
            document.mime_type, document.size
        )

        item = VaultItem(
            user_id=user_id,
            type=self._content_type(document),
            source_url=None,
            title=str(meta.get("pdf_title") or document.display_name),
            content=text,
            item_metadata=meta,
            file_name=document.display_name,
            file_size=document.size,
            mime_type=document.mime_type,
            processing_status=(
                ProcessingStatus.pending
                if (text or readable_image)
                else ProcessingStatus.skipped
            ),
        )

        if self.storage is not None:
            key = documents.object_key(user_id, item.id, document.ext)
            await self.storage.upload(key, document.data, document.mime_type)
            item.storage_key = key
        elif text is None:
            # Nothing to keep and nowhere to keep it: accepting this would silently
            # discard the user's file.
            raise DocumentError(
                "File storage isn't configured, so only PDFs and text files can be "
                "uploaded right now."
            )

        item = await self.repo.add(item)
        if (text or readable_image) and enqueue:
            await self._enqueue(item)

        log.info(
            "vault_document_saved",
            item_id=str(item.id),
            user_id=str(user_id),
            mime_type=document.mime_type,
            size=document.size,
            stored=item.storage_key is not None,
            has_text=text is not None,
            queued_for_vision=readable_image,
        )
        return item

    async def save_voice_note(
        self,
        user_id: uuid.UUID,
        audio: bytes,
        *,
        title: str | None = None,
        waveform: list[int] | None = None,
        language: str | None = None,
        duration: float | None = None,
        enqueue: bool = True,
        extra_metadata: dict[str, Any] | None = None,
    ) -> VaultItem:
        """Transcribe a spoken note and save what was said.

        The **transcript is the memory** -- it is what gets summarised, tagged, embedded
        and searched. The audio is a keepsake beside it, so the two failures are not
        treated alike:

        * A transcription failure aborts the save. There is nothing to remember, and a
          row holding only an audio file the pipeline cannot read is an item the user
          would have to open to discover is empty.
        * A *storage* failure does not. By the time the bucket is reached the clip has
          already been transcribed and paid for; discarding the words because the audio
          could not be filed would throw away the expensive half to preserve the cheap
          one. The item is saved without `storage_key` and the failure is logged.

        That ordering is the opposite of `save_document`, and deliberately: there the
        file *is* the item, so a bucket that will not take it means there is no item.
        """
        clip = transcription.inspect(audio)
        pinned = transcription.normalise_language(language)
        transcript = await transcription.transcribe(clip, pinned)

        meta: dict[str, Any] = {
            "source": "voice",
            "transcript_language": transcript.language,
            "transcript_model": transcript.model,
            # The provider's measurement when it made one, else the recorder's. Only the
            # whisper-* models report a duration, and the player needs one regardless: a
            # MediaRecorder WebM carries none in its header and reports `Infinity` until
            # it is fully buffered, so without this the scrubber has no length at all.
            "duration_seconds": transcript.duration or duration,
            # Kept so a re-transcription can reuse the choice rather than guessing again
            # -- which is the whole point of having made a choice.
            "transcribe_language": pinned,
            "file_size": clip.size,
            "mime_type": clip.mime_type,
        }
        # Amplitude peaks measured by the recorder, already validated and clamped. Kept in
        # metadata rather than derived on read: reading peaks back off the stored file
        # would mean downloading and decoding the audio in the browser, which the
        # presigned URL cannot serve cross-origin anyway.
        if waveform:
            meta["waveform"] = waveform
        meta.update(extra_metadata or {})

        # A title the user typed wins over one derived from speech -- they were looking
        # at the words when they wrote it. Capped to the column, stripped of the newlines
        # a paste can carry.
        chosen = (title or "").strip().replace("\n", " ")[:_TITLE_MAX]

        item = VaultItem(
            user_id=user_id,
            type=ContentType.voice,
            source_url=None,
            title=chosen or transcription.title_from(transcript.text),
            content=transcript.text,
            item_metadata=meta,
            processing_status=ProcessingStatus.pending,
        )

        # `file_name` / `file_size` / `mime_type` are set only once the object is really
        # in the bucket, because they are what the UI reads to decide whether to offer
        # playback and a download. Filling them in for audio that was never stored puts a
        # Download button on the page whose only possible answer is a 404. The clip's own
        # size and type stay in `item_metadata` either way, for anyone debugging later.
        if self.storage is not None:
            key = documents.object_key(user_id, item.id, clip.ext)
            try:
                await self.storage.upload(key, clip.data, clip.mime_type)
            except Exception as exc:  # noqa: BLE001 - never lose a paid-for transcript
                log.warning(
                    "voice_audio_store_failed",
                    user_id=str(user_id),
                    error=type(exc).__name__,
                    detail="transcript saved; the recording itself was not kept",
                )
            else:
                item.storage_key = key
                item.file_name = f"{transcription.VOICE_FILE_STEM}.{clip.ext}"
                item.file_size = clip.size
                item.mime_type = clip.mime_type

        item = await self.repo.add(item)
        if enqueue:
            await self._enqueue(item)

        log.info(
            "vault_voice_saved",
            item_id=str(item.id),
            user_id=str(user_id),
            language=transcript.language,
            duration=transcript.duration,
            chars=len(transcript.text),
            stored=item.storage_key is not None,
        )
        return item

    @staticmethod
    def _content_type(document: documents.Document) -> ContentType:
        if document.ext == "pdf":
            return ContentType.pdf
        if document.is_image:
            return ContentType.image
        return ContentType.document

    async def file_link(
        self, item_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[str, VaultItem] | None:
        """A short-lived download URL, or None when there is no file to hand back.

        Ownership is enforced by the repository (`get` scopes on user_id and returns None
        on mismatch), so a guessed item id from another account is a 404 here, not a
        download. The URL is minted per request and expires in minutes — it is never
        stored, never logged and never reusable by a third party who did not ask for it.
        """
        item = await self.repo.get(item_id, user_id)
        if item is None or not item.storage_key or self.storage is None:
            return None
        url = await self.storage.presigned_get(
            item.storage_key,
            filename=item.file_name or "download",
            content_type=item.mime_type or "application/octet-stream",
            expires=settings.DOWNLOAD_LINK_TTL_SECONDS,
        )
        return url, item

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
        self,
        user_id: uuid.UUID,
        title: str,
        content: str,
        *,
        enqueue: bool = True,
        extra_metadata: dict[str, Any] | None = None,
    ) -> VaultItem:
        item = VaultItem(
            user_id=user_id,
            type=ContentType.note,
            title=title,
            content=content,
            item_metadata=dict(extra_metadata or {}),
            processing_status=ProcessingStatus.pending,
        )
        item = await self.repo.add(item)
        if enqueue:
            await self._enqueue(item)  # AI still summarizes/tags/embeds notes
        return item

    async def update_content(
        self,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        blocks: list[dict[str, Any]],
    ) -> VaultItem | None:
        """Replace an item's body with what the user typed. Returns None when not theirs.

        Ownership is the repository's `get` (scoped on `user_id`), so another account's
        id is indistinguishable from one that does not exist and the route answers 404
        either way -- the endpoint cannot be used to probe which ids are real.

        Three things move together and the reasons differ:

        * `content` is replaced outright. That is what the user asked for; the previous
          extraction is not kept, since a memory with two bodies has no answer to "what
          does this say".
        * `ai_highlights` are re-checked against the new text. They are stored as
          verbatim quotes of `content`; after an edit some of them are quotes of a
          paragraph that no longer exists, and a mark that cannot be located would either
          vanish or -- worse, if the words happen to reappear -- land somewhere the model
          never pointed.
        * The block document is kept in `item_metadata` so reopening the editor restores
          headings and lists. It is *derived* from the same sanitize pass as `content`,
          never accepted as a second source of truth.

        Both JSONB fields are reassigned rather than mutated: SQLAlchemy does not track
        in-place edits to a plain JSONB value, so a mutation would flush nothing and the
        save would silently do nothing at all.

        The embedding is deliberately NOT recomputed here. Re-running the pipeline would
        re-fetch `source_url` and overwrite the text the user just wrote, so semantic
        search keeps ranking this item by its pre-edit body until it is reprocessed.
        """
        item = await self.repo.get(item_id, user_id)
        if item is None or item.deleted_at is not None:
            return None

        document, content = editor_doc.sanitize(blocks)

        item.content = content
        item.ai_highlights = keep_verbatim(list(item.ai_highlights), content)
        item.item_metadata = {
            **item.item_metadata,
            "editor_doc": document,
            "content_edited_at": utcnow().isoformat(),
        }
        await self.repo.add(item)

        log.info(
            "vault_content_edited",
            item_id=str(item.id),
            user_id=str(user_id),
            blocks=len(document["blocks"]),
            chars=len(content),
            highlights_kept=len(item.ai_highlights),
        )
        return item

    async def list(
        self, user_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[Sequence[VaultItem], int]:
        return await self.repo.list_for_user(user_id, limit, offset)

    async def list_recent(
        self,
        user_id: uuid.UUID,
        limit: int,
        *,
        created_after: datetime | None = None,
        content_types: Sequence[ContentType] | None = None,
        category: str | None = None,
    ) -> tuple[Sequence[VaultItem], int]:
        """Newest-first listing with optional filters, for chat-surface queries."""
        return await self.repo.list_filtered(
            user_id,
            limit=limit,
            created_after=created_after,
            content_types=content_types,
            category=category,
        )

    async def get(self, item_id: uuid.UUID, user_id: uuid.UUID) -> VaultItem | None:
        return await self.repo.get(item_id, user_id)

    async def search(
        self, user_id: uuid.UUID, query: str, limit: int, offset: int
    ) -> tuple[Sequence[VaultItem], int]:
        return await self.repo.search(user_id, query, limit, offset)

    async def reprocess(
        self, item_id: uuid.UUID, user_id: uuid.UUID, language: str | None = None
    ) -> VaultItem:
        """Put a finished-badly item back on the queue at its owner's request.

        Only `failed` and `skipped` are re-drivable, and for different reasons. A failure
        is usually transient -- a provider timeout, a worker that died -- so trying again
        is exactly right. A `skipped` item had nothing to read *at the time*, which is a
        statement about this deployment as much as about the file: an image saved before a
        vision key existed becomes readable the moment one does, and without this there is
        no way to ask for that.

        `pending` and `processing` are refused as already-running rather than as errors —
        the UI hides the button in those states, so reaching here means a double click or
        a script.

        Raises `ReprocessError` with a message written for the person who pressed it.
        """
        item = await self.repo.get(item_id, user_id)
        if item is None:
            # Scoped by the repository, so another account's id is indistinguishable from
            # one that does not exist; the route answers 404 either way.
            raise ItemNotFound()

        if item.processing_status in (
            ProcessingStatus.pending,
            ProcessingStatus.processing,
        ):
            raise ReprocessError("This one is already being processed.")
        # A finished item is normally refused: re-running it would spend the whole AI
        # pipeline to replace a result with itself. A voice note whose audio is still in
        # the bucket is the one exception, and a narrow one. Its transcript is the single
        # output that can come back confidently, fluently wrong -- auto-detection can hear
        # a Bengali clip as Chinese and produce a perfect transcript of a language nobody
        # spoke -- and "finished" is exactly what makes that unfixable otherwise. The
        # audio is still there to redo it from, so it can be redone.
        retranscribable = item.type is ContentType.voice and bool(item.storage_key)
        if item.processing_status is ProcessingStatus.completed and not retranscribable:
            raise ReprocessError("This one finished already — there is nothing to retry.")

        # The button disables itself the moment the item goes back to `pending`, so this
        # only has to stop a double click and a script. Deliberately a cooldown rather
        # than a lifetime cap: a cap strands a user whose provider was down all morning.
        age = (utcnow() - item.updated_at).total_seconds()
        if age < settings.REPROCESS_COOLDOWN_SECONDS:
            wait = int(settings.REPROCESS_COOLDOWN_SECONDS - age) + 1
            raise ReprocessError(f"Just tried that. Give it {wait} more seconds.")

        # A voice note is re-transcribed rather than merely re-enriched, because the thing
        # most likely to be wrong with one is the transcript itself: auto-detection can
        # hear a Bengali clip as Chinese and produce a fluent, confident, useless memory.
        # Only when the audio is still in the bucket -- clearing the words with nothing to
        # re-read them from would destroy the memory in the name of repairing it.
        if retranscribable:
            item.content = None
            pinned = transcription.normalise_language(language)
            if pinned:
                # Naming the language is what removes the detection step, so a re-run
                # with a choice is a different attempt rather than the same coin flip.
                item.item_metadata = {
                    **item.item_metadata,
                    "transcribe_language": pinned,
                }
            log.info(
                "vault_reprocess_retranscribe",
                item_id=str(item.id),
                language=pinned,
            )

        item.processing_status = ProcessingStatus.pending
        item.processing_error = None
        # Reset so the worker's own three attempts start fresh; a manual retry is a new
        # attempt at the job, not a continuation of the one that ran out of tries.
        item.retry_count = 0
        item.item_metadata = {
            **item.item_metadata,
            "reprocess_requested_at": utcnow().isoformat(),
        }
        item = await self.repo.add(item)

        # Same enqueue-before-commit race as `save_url`, and benign here: the row already
        # exists and is visible, so a worker that dequeues early re-reads it and sets it
        # to `processing` regardless of the status it finds.
        await self._enqueue(item)
        log.info("vault_reprocess_requested", item_id=str(item.id), user_id=str(user_id))
        return item

    async def delete(self, item_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        item = await self.repo.get(item_id, user_id)
        if item is None:
            return False
        key = item.storage_key
        await self.repo.delete(item)
        if key and self.storage is not None:
            # After the row, and best-effort inside the provider: a bucket hiccup must not
            # fail the user's delete. `B2Storage.delete` logs instead of raising, so the
            # worst case is an orphaned object, not a vault item that refuses to go away.
            await self.storage.delete(key)
        return True
