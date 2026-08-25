"""A device's long-lived login: one row per refresh token.

This is the *stateful* half of authentication. The access JWT is unrevocable by design;
this table is what makes "sign out", "sign out everywhere" and "that wasn't me" real
actions rather than cosmetic ones.

One row per issued refresh token, not one per device: rotation writes a new row and
retires the old one, so a session is really a chain linked by `family_id`. Keeping the
retired rows (rather than deleting them) is what makes replay detectable -- a token
presented after it was rotated away is either a stolen copy or a client bug, and both
deserve the whole chain revoked.

Nothing here is ever sent to the browser except `id`: `token_hash` is a SHA-256 digest
(see `app.core.security`), and the raw token exists only in the response that minted it.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import new_uuid, utcnow


class UserSession(SQLModel, table=True):
    __tablename__ = "user_sessions"
    __table_args__ = (
        # Every rotation looks the presented token up by digest; without this index that
        # is a sequential scan over every session ever issued.
        Index("ix_user_sessions_token_hash", "token_hash", unique=True),
        Index("ix_user_sessions_family_id", "family_id"),
        Index("ix_user_sessions_expires_at", "expires_at"),
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
    # Constant across a rotation chain. Revoking a family is how a detected replay kills
    # both the thief's token and the victim's in one statement.
    family_id: uuid.UUID = Field(
        sa_column=Column(PGUUID(as_uuid=True), nullable=False)
    )
    token_hash: str = Field(sa_column=Column(Text, nullable=False))

    # Sliding expiry: refreshed on every rotation.
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    # When the *chain* began. Copied forward on rotation so an endlessly refreshed
    # session still hits REFRESH_TOKEN_ABSOLUTE_DAYS.
    family_started_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )

    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    # "rotated" | "logout" | "logout_all" | "reuse_detected". Read by humans during an
    # incident, so keep the vocabulary small and stable.
    revoked_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    replaced_by_id: uuid.UUID | None = Field(
        default=None, sa_column=Column(PGUUID(as_uuid=True), nullable=True)
    )

    # Shown back to the user in the device list. Truncated at write time -- a User-Agent
    # is attacker-controlled and unbounded.
    user_agent: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    ip_address: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    last_used_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
