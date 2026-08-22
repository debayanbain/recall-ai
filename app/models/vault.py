"""VaultItem — universal content entity — and VaultChunk for RAG embeddings."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus, new_uuid, utcnow


class VaultItem(SQLModel, table=True):
    __tablename__ = "vault_items"
    __table_args__ = (
        Index("ix_vault_items_user_created", "user_id", "created_at"),
        Index("ix_vault_items_user_status", "user_id", "processing_status"),
        Index("ix_vault_items_user_type", "user_id", "type"),
        Index("ix_vault_items_ai_tags", "ai_tags", postgresql_using="gin",
              postgresql_ops={"ai_tags": "jsonb_path_ops"}),
    )

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    type: ContentType = Field(index=True)
    source_url: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=512)
    summary: Optional[str] = None
    content: Optional[str] = None
    thumbnail_url: Optional[str] = None
    language: Optional[str] = None

    item_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    ai_tags: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    ai_category: Optional[str] = Field(default=None, index=True)

    processing_status: ProcessingStatus = Field(default=ProcessingStatus.pending, index=True)
    processing_error: Optional[str] = None
    retry_count: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default="0"))
    processed_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    )
    deleted_at: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class VaultChunk(SQLModel, table=True):
    """One chunk of a VaultItem with its embedding. 1:many per item for real RAG."""
    __tablename__ = "vault_chunks"
    __table_args__ = (
        UniqueConstraint("vault_item_id", "chunk_index", name="uq_vault_chunks_item_idx"),
        Index("ix_vault_chunks_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    vault_item_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("vault_items.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    )
    chunk_index: int = Field(sa_column=Column(Integer, nullable=False))
    content: str = Field(sa_column=Column(Text, nullable=False))
    embedding: Optional[Any] = Field(default=None, sa_column=Column(Vector(settings.EMBEDDING_DIM)))
    token_count: Optional[int] = None
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
