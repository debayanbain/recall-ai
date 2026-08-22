"""User model."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Text, UniqueConstraint, func
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
    name: str | None = None
    avatar_url: str | None = None
    auth_provider: str = Field(
        default="google",
        sa_column=Column("auth_provider", Text, nullable=False, server_default="google"),
    )
    provider_account_id: str | None = None
    # True only when a provider asserted the address is verified. Account linking by email
    # is gated on this: an unverified address from one provider must never be able to take
    # over an existing account created through another.
    email_verified: bool = Field(
        default=False,
        sa_column=Column("email_verified", Boolean, nullable=False, server_default="false"),
    )
    plan: Plan = Field(default=Plan.free)

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    )
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
