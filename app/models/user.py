"""User model."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, DateTime, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlmodel import Field, SQLModel

from app.models.base import Plan, new_uuid, utcnow


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("auth_provider", "provider_account_id", name="uq_users_provider"),
    )

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    email: str = Field(sa_column=Column(CITEXT, nullable=False, unique=True))
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    auth_provider: str = Field(
        default="google",
        sa_column=Column("auth_provider", nullable=False, server_default="google"),
    )
    provider_account_id: Optional[str] = None
    plan: Plan = Field(default=Plan.free)

    created_at: datetime = Field(default_factory=utcnow, sa_column_kwargs={"server_default": text("now()")})
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    )
    deleted_at: Optional[datetime] = None
