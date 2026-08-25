"""Vault use-cases: save URL/note, list, get, search, delete.

Saving NEVER blocks on AI — it stores the item, enqueues a job, returns fast.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.core.config import settings
from app.core.logging import get_logger
from app.core.urls import canonical_url
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.queue.client import enqueue_process_item
from app.repositories.vault import VaultRepository
from app.services import documents
from app.services.documents import DocumentError
from app.storage import ObjectStorage

log = get_logger("vault.service")


class VaultService:
    def __init__(self, repo: VaultRepository, storage: ObjectStorage | None = None) -> None:
        self.repo = repo
        # None when no bucket is configured. Uploads then still work for PDFs and text
        # files (their *text* is the memory), and are refused for anything binary.
        self.storage = storage

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

    async def save_document(
        self, user_id: uuid.UUID, data: bytes, filename: str | None
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
                ProcessingStatus.pending if text else ProcessingStatus.skipped
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
        if text:
            await self._enqueue(item)

        log.info(
            "vault_document_saved",
            item_id=str(item.id),
            user_id=str(user_id),
            mime_type=document.mime_type,
            size=document.size,
            stored=item.storage_key is not None,
            has_text=text is not None,
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
        key = item.storage_key
        await self.repo.delete(item)
        if key and self.storage is not None:
            # After the row, and best-effort inside the provider: a bucket hiccup must not
            # fail the user's delete. `B2Storage.delete` logs instead of raising, so the
            # worst case is an orphaned object, not a vault item that refuses to go away.
            await self.storage.delete(key)
        return True
