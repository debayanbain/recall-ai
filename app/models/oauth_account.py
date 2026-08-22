"""Linked third-party identity for a user.

One row per (provider, provider_account_id). This -- not `users.auth_provider` -- is the
source of truth for "which external account is this", so one user can link Google,
Facebook and X at once. Tokens are stored encrypted; see `app.core.crypto`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import new_uuid, utcnow


class OAuthAccount(SQLModel, table=True):
    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint("provider", "provider_account_id", name="uq_oauth_provider_account"),
    )

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    provider: str = Field(sa_column=Column(Text, nullable=False))
    provider_account_id: str = Field(sa_column=Column(Text, nullable=False))

    # Identity snapshot from the provider, kept for display and for re-linking.
    email: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    email_verified: bool = Field(default=False)
    name: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    avatar_url: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    username: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # Fernet ciphertext, never plaintext. Null when TOKEN_ENCRYPTION_KEY is unset (dev).
    access_token_encrypted: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    refresh_token_encrypted: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    scopes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    expires_at: datetime | None = Field(
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
