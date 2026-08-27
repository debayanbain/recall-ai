"""Telegram bot linkage: which Telegram identity speaks for which RecallAI user.

Two tables, with different lifetimes.

`telegram_accounts` is the durable binding. `telegram_user_id` is unique **globally**,
not per user: a Telegram identity may speak for at most one RecallAI account. Scoping the
constraint to `(user_id, telegram_user_id)` would let a second user link a Telegram
account someone else already owns, and every later message from that chat would be
ambiguous -- in practice resolved to whichever row was found first, i.e. an account
takeover by whoever taps Start second.

`telegram_link_tokens` is the short-lived hand-off that establishes the binding. Only the
SHA-256 digest is stored, exactly as `user_sessions.token_hash` does. A fast hash is
correct here for the same reason: the input is 32 random bytes rather than a password, so
there is no dictionary to run, and the lookup has to be one indexed equality match.
Tokens are single-use -- `used_at` is stamped on redemption and a second presentation is
refused.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import new_uuid, utcnow


class TelegramAccount(SQLModel, table=True):
    __tablename__ = "telegram_accounts"
    __table_args__ = (
        # Global, not per-user. See the module docstring: a per-user constraint is an
        # account-takeover primitive, not a looser version of the same rule.
        UniqueConstraint("telegram_user_id", name="uq_telegram_account_user"),
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

    # Telegram's numeric ids, stored as text: they are identifiers, never arithmetic, and
    # Telegram has already outgrown 32-bit ids once.
    telegram_user_id: str = Field(sa_column=Column(Text, nullable=False))
    # In a private chat this equals telegram_user_id, but Telegram does not promise that
    # and the send API addresses the *chat*. Kept separately so replies never guess.
    telegram_chat_id: str = Field(sa_column=Column(Text, nullable=False))

    username: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    first_name: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    last_name: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    language_code: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    linked_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    )


class TelegramLinkToken(SQLModel, table=True):
    __tablename__ = "telegram_link_tokens"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )

    # SHA-256 of the value that travels in the deep link. The raw token is never stored.
    token_hash: str = Field(
        sa_column=Column(Text, nullable=False, unique=True, index=True),
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
    # Stamped on redemption. A token presented twice is refused rather than re-linked.
    used_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
