"""Collection and CollectionItem (ordered many-to-many vault items)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import Visibility, new_uuid, utcnow


class Collection(SQLModel, table=True):
    __tablename__ = "collections"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    name: str = Field(max_length=255)
    slug: str = Field(sa_column=Column("slug", nullable=False, unique=True), max_length=300)
    description: Optional[str] = None
    visibility: Visibility = Field(default=Visibility.private)

    created_at: datetime = Field(default_factory=utcnow, sa_column_kwargs={"server_default": text("now()")})
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    )
    deleted_at: Optional[datetime] = None


class CollectionItem(SQLModel, table=True):
    __tablename__ = "collection_items"
    __table_args__ = (Index("ix_collection_items_item", "vault_item_id"),)

    collection_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("collections.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    vault_item_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("vault_items.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    position: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    added_at: datetime = Field(default_factory=utcnow, sa_column_kwargs={"server_default": text("now()")})
