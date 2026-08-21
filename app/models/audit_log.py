"""AuditLog — high-volume event trail (bigint PK, no soft delete)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import utcnow


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_user_time", "user_id", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Optional[int] = Field(
        default=None,
        sa_column=Column(BigInteger, primary_key=True, autoincrement=True),
    )
    user_id: Optional[uuid.UUID] = Field(
        default=None,
        sa_column=Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")),
    )
    action: str = Field(sa_column=Column(Text, nullable=False))
    entity_type: Optional[str] = None
    entity_id: Optional[uuid.UUID] = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True))
    )
    ip: Optional[str] = Field(default=None, sa_column=Column(INET))
    request_id: Optional[str] = None
    meta: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    )
    created_at: datetime = Field(
        default_factory=utcnow, sa_column_kwargs={"server_default": text("now()")}
    )
