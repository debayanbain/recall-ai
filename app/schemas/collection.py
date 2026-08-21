"""Collection request/response DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.base import Visibility
from app.schemas.vault import VaultItemRead


class CreateCollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    visibility: Visibility = Visibility.private


class AddItemRequest(BaseModel):
    vault_item_id: uuid.UUID


class CollectionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    visibility: Visibility
    created_at: datetime


class CollectionDetail(CollectionRead):
    items: list[VaultItemRead] = Field(default_factory=list)


class PublicCollection(BaseModel):
    name: str
    description: str | None
    items: list[VaultItemRead]
