"""Turning a Telegram message into a vault item.

Everything a user sends the bot ends up in the same `VaultItem` table as a web save, via
the same `VaultService`, so the extractor registry, the document allowlist, the SSRF
guard and the AI pipeline are all inherited rather than reimplemented. This module only
decides *which* of those paths a message belongs on, and supplies the metadata that lets
the worker send a reply afterwards.

The message is untrusted input. Field types are checked rather than assumed -- Telegram's
schema is stable, but this arrives over the same wire as everything else.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.logging import get_logger
from app.models.vault import VaultItem
from app.services.documents import DocumentError
from app.services.telegram.client import TelegramApiError, TelegramClient
from app.services.vault_service import VaultService

log = get_logger("telegram")

_MAX_NOTE_CHARS = 20_000
_MAX_NOTE_TITLE = 120
# Mapped only for the types the upload allowlist already accepts. Telegram photos arrive
# with no filename at all, and `documents.inspect` reads the extension from the filename,
# so without a synthesised name every photo would be refused as an unsupported type.
_MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
    "application/json": "json",
    "application/rtf": "rtf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}


class CaptureKind(StrEnum):
    saved = "saved"
    duplicate = "duplicate"
    note = "note"
    stored_only = "stored_only"
    voice_unsupported = "voice_unsupported"
    too_large = "too_large"
    unsupported = "unsupported"
    nothing = "nothing"


@dataclass(slots=True)
class CaptureOutcome:
    kind: CaptureKind
    item: VaultItem | None = None


def source_metadata(chat_id: str, message_id: int | None) -> dict[str, Any]:
    """Stamped on every item the bot creates.

    `source == "telegram"` is what makes the worker send a reply when processing
    finishes. The chat id is recorded for debugging only -- the delivery task re-derives
    the real address from `telegram_accounts`, because a value inside `item_metadata`
    must never be able to route one user's content to another user's chat.
    """
    return {
        "source": "telegram",
        "telegram_chat_id": chat_id,
        "telegram_message_id": message_id,
    }


def _text_of(message: dict[str, Any]) -> str:
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _is_http_url(candidate: str) -> bool:
    """Only http(s) with a host. Other schemes are not extractable and not fetchable."""
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


def first_url(message: dict[str, Any]) -> str | None:
    """The first link in a message, preferring Telegram's own entity offsets.

    Entities are used first because they resolve a hyperlinked label (`text_link`) to its
    real target, which naive text scanning would miss entirely.
    """
    text = _text_of(message)
    entities = message.get("entities") or message.get("caption_entities") or []
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            kind = entity.get("type")
            if kind == "text_link":
                url = entity.get("url")
                if isinstance(url, str) and _is_http_url(url):
                    return url
            elif kind == "url":
                offset, length = entity.get("offset"), entity.get("length")
                if isinstance(offset, int) and isinstance(length, int):
                    # Telegram counts UTF-16 code units, so an emoji earlier in the
                    # message shifts every later offset if we slice the str directly.
                    encoded = text.encode("utf-16-le")
                    candidate = encoded[offset * 2 : (offset + length) * 2].decode(
                        "utf-16-le", errors="ignore"
                    )
                    if _is_http_url(candidate):
                        return candidate

    for token in text.split():
        if _is_http_url(token):
            return token
    return None


def _attachment(message: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The one attachment we care about, as (kind, payload)."""
    for key in ("voice", "audio", "video_note"):
        payload = message.get(key)
        if isinstance(payload, dict):
            return "voice", payload

    document = message.get("document")
    if isinstance(document, dict):
        return "document", document

    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        # Telegram sends every rendition, smallest first. The last is full resolution.
        largest = photos[-1]
        if isinstance(largest, dict):
            return "photo", largest

    video = message.get("video")
    if isinstance(video, dict):
        # Not in the upload allowlist; refused explicitly rather than downloaded first.
        return "unsupported", video
    return None


class TelegramCaptureService:
    """Owns one message's worth of work for one user."""

    def __init__(self, vault: VaultService, client: TelegramClient) -> None:
        self.vault = vault
        self.client = client

    async def capture_note(
        self, user_id: uuid.UUID, text: str, message: dict[str, Any], chat_id: str
    ) -> CaptureOutcome:
        """Save `text` as a note, verbatim.

        Separate from `capture` because the text is the *argument* of `/note`, not the
        message body -- capturing the body would store the command word too. No URL
        extraction and no attachment handling: `/note https://…` means the person wants
        the link kept as a thought, and second-guessing that is how an explicit
        instruction turns into a surprise.
        """
        text = text.strip()
        if not text:
            return CaptureOutcome(CaptureKind.nothing)

        meta = source_metadata(chat_id, _int_or_none(message.get("message_id")))
        item = await self.vault.create_note(
            user_id,
            title=_note_title(text),
            content=text[:_MAX_NOTE_CHARS],
            enqueue=False,
            extra_metadata=meta,
        )
        return CaptureOutcome(CaptureKind.note, item)

    async def capture(
        self, user_id: uuid.UUID, message: dict[str, Any], chat_id: str
    ) -> CaptureOutcome:
        meta = source_metadata(chat_id, _int_or_none(message.get("message_id")))

        attachment = _attachment(message)
        if attachment is not None:
            kind, payload = attachment
            if kind == "voice":
                return CaptureOutcome(CaptureKind.voice_unsupported)
            if kind == "unsupported":
                return CaptureOutcome(CaptureKind.unsupported)
            return await self._capture_file(user_id, payload, meta)

        url = first_url(message)
        if url is not None:
            item, created = await self.vault.save_url(
                user_id, url, enqueue=False, extra_metadata=meta
            )
            return CaptureOutcome(
                CaptureKind.saved if created else CaptureKind.duplicate, item
            )

        text = _text_of(message).strip()
        if not text:
            return CaptureOutcome(CaptureKind.nothing)

        item = await self.vault.create_note(
            user_id,
            title=_note_title(text),
            content=text[:_MAX_NOTE_CHARS],
            enqueue=False,
            extra_metadata=meta,
        )
        return CaptureOutcome(CaptureKind.note, item)

    async def _capture_file(
        self, user_id: uuid.UUID, payload: dict[str, Any], meta: dict[str, Any]
    ) -> CaptureOutcome:
        file_id = payload.get("file_id")
        if not isinstance(file_id, str):
            return CaptureOutcome(CaptureKind.unsupported)

        max_bytes = settings.TELEGRAM_MAX_FILE_MB * 1024 * 1024
        declared = _int_or_none(payload.get("file_size"))
        # Checked before the download so an oversized file costs nothing to refuse; the
        # download caps itself as well, because file_size is Telegram's claim, not a fact.
        if declared is not None and declared > max_bytes:
            return CaptureOutcome(CaptureKind.too_large)

        try:
            info = await self.client.get_file(file_id)
            file_path = info.get("file_path")
            if not isinstance(file_path, str):
                return CaptureOutcome(CaptureKind.unsupported)
            data = await self.client.download_file(file_path, max_bytes=max_bytes)
        except TelegramApiError as exc:
            log.warning("telegram_file_fetch_failed", error=str(exc)[:200])
            oversized = "size limit" in str(exc)
            return CaptureOutcome(
                CaptureKind.too_large if oversized else CaptureKind.unsupported
            )

        filename = _filename_for(payload, file_path)
        try:
            item = await self.vault.save_document(
                user_id, data, filename, enqueue=False, extra_metadata=meta
            )
        except DocumentError as exc:
            log.info("telegram_file_rejected", reason=str(exc)[:200])
            return CaptureOutcome(CaptureKind.unsupported)

        # No text means no summary and no tags are ever coming; the caller says so
        # plainly rather than promising a result that never arrives.
        kind = CaptureKind.saved if item.content else CaptureKind.stored_only
        return CaptureOutcome(kind, item)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _note_title(text: str) -> str:
    first_line = text.strip().splitlines()[0].strip()
    if len(first_line) <= _MAX_NOTE_TITLE:
        return first_line or "Note"
    return first_line[: _MAX_NOTE_TITLE - 1].rstrip() + "…"


def _filename_for(payload: dict[str, Any], file_path: str) -> str:
    """A filename with a real extension, because that is what the allowlist reads.

    Telegram supplies one for documents and never for photos. The extension is taken from
    the path Telegram itself returned, then from the declared MIME type; the magic-byte
    check in `documents.inspect` still has the final say on what the bytes actually are.
    """
    declared = payload.get("file_name")
    if isinstance(declared, str) and "." in declared:
        return declared

    ext = ""
    tail = file_path.rsplit("/", 1)[-1]
    if "." in tail:
        ext = tail.rsplit(".", 1)[-1].lower()
    if not ext:
        mime = payload.get("mime_type")
        ext = _MIME_EXT.get(mime, "") if isinstance(mime, str) else ""
    if not ext:
        # Left extensionless on purpose: `documents.inspect` then refuses it, which is
        # the correct answer for a file we cannot identify.
        return "telegram-upload"
    return f"telegram-upload.{ext}"
