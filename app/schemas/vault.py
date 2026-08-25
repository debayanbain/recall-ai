"""Vault request/response DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from app.models.base import ContentType, ProcessingStatus


class SaveUrlRequest(BaseModel):
    url: HttpUrl
    title: str | None = None


class CreateNoteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1)


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
    processing_status: ProcessingStatus
    created_at: datetime

    # Uploaded-file metadata. `storage_key` is deliberately absent: the bucket layout is
    # not the browser's business, and the only way to reach a file is GET /vault/{id}/file.
    file_name: str | None = None
    file_size: int | None = None
    mime_type: str | None = None


class VaultItemDetail(VaultItemRead):
    content: str | None
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
