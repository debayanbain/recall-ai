"""Vault request/response DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models.base import ContentType, ProcessingStatus
from app.services.editor_doc import MAX_BLOCKS


class SaveUrlRequest(BaseModel):
    url: HttpUrl
    title: str | None = None


class CreateNoteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)


class ReprocessRequest(BaseModel):
    """Optional knobs on a retry. Only voice notes have one so far.

    `language` is validated against a closed allowlist in `services/transcription.py`
    rather than here: an unknown code means auto-detect, not a rejected request, because
    a client bug must not stand between someone and a fixed transcript.
    """

    language: str | None = Field(default=None, max_length=8)


class UpdateContentRequest(BaseModel):
    """A manual rewrite of an item's body, as EditorJS blocks.

    Deliberately the *only* thing this endpoint accepts. Taking the whole item and
    copying fields across would let a caller set `ai_category`, `processing_status` or
    `user_id` by adding them to the body; there is nothing here to overpost with.

    The plain text is derived from the blocks server-side rather than sent alongside
    them, so `content` and the stored document can never disagree about what the user
    actually wrote.
    """

    blocks: list[dict[str, Any]] = Field(max_length=MAX_BLOCKS)


class VaultItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: ContentType
    source_url: str | None
    title: str | None
    summary: str | None
    thumbnail_url: str | None
    ai_tags: list[str]
    ai_category: str | None
    #: One distinctive line per memory. Cards show it to tell two items apart when their
    #: tags are identical, which for topical tags is the common case rather than the edge.
    ai_label: str | None = None
    processing_status: ProcessingStatus
    #: Why the pipeline gave up, scrubbed of anything credential-shaped before storage
    #: (`app/core/errors.py`). Present so the owner is told what happened rather than
    #: shown a spinner that never resolves; absent on every healthy item.
    processing_error: str | None = None
    created_at: datetime

    # Uploaded-file metadata. `storage_key` is deliberately absent: the bucket layout is
    # not the browser's business, and the only way to reach a file is GET /vault/{id}/file.
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None


class VaultItemDetail(VaultItemRead):
    content: str | None
    #: Verbatim spans of `content`, for the reader to mark in place. Only sent with the
    #: detail because they are meaningless without the text they index into.
    ai_highlights: list[str] = Field(default_factory=list)
    item_metadata: dict[str, Any] = Field(default_factory=dict)


class VaultListResponse(BaseModel):
    items: list[VaultItemRead]
    total: int
    limit: int
    offset: int


class FileLinkResponse(BaseModel):
    """A short-lived download URL for an uploaded file.

    Minted per request and never stored. Treat it as a credential: it carries its own
    signature, so anyone holding it can fetch the object until it expires.
    """

    url: str
    expires_in: int
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None
