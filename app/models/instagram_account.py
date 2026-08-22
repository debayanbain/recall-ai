"""An Instagram Business/Creator account a user has connected through Facebook.

This is a *resource grant*, not an identity -- distinct from `oauth_accounts`, which
answers "who is this person". A user signs in with Google or Facebook and then connects
Instagram separately, so one user can hold several Instagram accounts (one per Facebook
Page they manage).

Instagram Graph calls are made with the **Page** access token, not the user token, which
is why both are stored. Page tokens derived from a long-lived user token do not expire,
so this row is what a future Instagram extractor reads from. Both tokens are Fernet
ciphertext -- see `app.core.crypto`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import new_uuid, utcnow


class InstagramAccount(SQLModel, table=True):
    __tablename__ = "instagram_accounts"
    __table_args__ = (
        # One row per (user, IG account). Re-connecting updates in place rather than
        # stacking duplicate grants with stale tokens.
        UniqueConstraint("user_id", "instagram_user_id", name="uq_instagram_user_account"),
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

    # Instagram Business account, as reported by the Graph API.
    instagram_user_id: str = Field(sa_column=Column(Text, nullable=False))
    username: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    name: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    profile_picture_url: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # The Facebook Page the IG account is linked to. Required: IG Graph reads are
    # authorised by this Page's token, not the user's.
    page_id: str = Field(sa_column=Column(Text, nullable=False))
    page_name: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    page_access_token_encrypted: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    user_access_token_encrypted: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )
    # Expiry of the long-lived *user* token (~60 days). The Page token outlives it, but
    # once this passes the user must re-consent to mint a fresh one.
    token_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    scopes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now()),
    )
