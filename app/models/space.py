"""Spaces: a named context holding many memories, shared with many people.

A Space is deliberately **not** a folder. A `VaultItem` belongs to as many Spaces as it
belongs to -- `space_items` is a plain many-to-many and nothing anywhere enforces
exclusivity. That is the whole difference between "a context" and "where the file lives".

Four tables, and the split matters:

* `spaces` -- the container. Owned by one user (`user_id`), which is what `visibility` and
  every destructive action are checked against.
* `space_items` -- membership of a memory. Composite PK, so re-adding the same memory is a
  conflict the repository swallows rather than a duplicate row.
* `space_members` -- membership of a *person*. This is the first table in the codebase that
  lets one user read another's rows, so it is also the first place a role appears.
* `space_invites` -- a credential, kept in its own table for the same reason
  `telegram_link_tokens` is: it has a ten-minute-ish life and a purge by `expires_at` must
  never have to walk the memberships.

No `sqlmodel.Relationship` anywhere -- there are none in this repo, and joins are written
explicitly in `app/repositories/space.py` so the query a page costs is visible at the call
site rather than emerging from lazy loading.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import Visibility, new_uuid, utcnow


class Space(SQLModel, table=True):
    __tablename__ = "spaces"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    #: The owner. Membership lives in `space_members`; this column is what every
    #: owner-only action (visibility, invite, delete) is checked against, and it is
    #: never reassigned.
    user_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    )
    name: str = Field(max_length=255)
    slug: str = Field(sa_column=Column("slug", String(300), nullable=False, unique=True))
    description: str | None = None
    visibility: Visibility = Field(default=Visibility.private)

    emoji: str | None = Field(default=None, max_length=16)
    #: An accent *key* ("violet", "rose", ...), never a CSS class. The gradient strings
    #: live in the frontend; a column holding `from-violet-200 via-indigo-100` is a
    #: database coupled to a Tailwind version, and it breaks on their next major.
    accent: str | None = Field(default=None, max_length=24)
    pinned: bool = Field(
        default=False,
        sa_column=Column("pinned", Boolean, nullable=False, server_default="false"),
    )

    #: Model-written, and labelled as such everywhere it is rendered. Regenerated on
    #: request, never silently on read -- an overview that rewrites itself whenever the
    #: page is opened is a bill with no ceiling.
    ai_overview: str | None = None
    ai_topics: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    )
    overview_generated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    #: Cached from the last `/connections` computation. NULL means "never computed", which
    #: the UI renders as *nothing* rather than as zero -- an invented count is worse than
    #: an absent one, and the pairwise scan is O(n^2) and not worth running for a card.
    connection_count: int | None = Field(
        default=None, sa_column=Column(Integer, nullable=True)
    )
    connections_computed_at: datetime | None = Field(
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
    #: Soft delete, unlike `VaultRepository.delete`. A Space can hold other people's
    #: contributions, so destroying the rows is not one person's call to make quietly.
    deleted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class SpaceItem(SQLModel, table=True):
    __tablename__ = "space_items"
    #: Reverse lookup: "which Spaces is this memory in?", which the card's + menu asks on
    #: every open. Without it that is a sequential scan of every membership row.
    __table_args__ = (Index("ix_space_items_item", "vault_item_id"),)

    space_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("spaces.id", ondelete="CASCADE"),
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
    #: Who contributed it. SET NULL rather than CASCADE: a departed member's memories stop
    #: being attributed, but the Space does not silently lose rows other people are
    #: reading. Never used for authorization -- `vault_items.user_id` is the owner.
    added_by: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    added_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


class SpaceMember(SQLModel, table=True):
    """A person who may read a Space, and how much they may do to it.

    The owner is **not** stored here -- ownership is `spaces.user_id`. Keeping one source
    of truth for it means a role row can never contradict the column that gates deletion.
    """

    __tablename__ = "space_members"
    #: "Which Spaces am I in?" -- asked on every listing.
    __table_args__ = (Index("ix_space_members_user", "user_id"),)

    space_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("spaces.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        )
    )
    #: `SpaceRole`, stored as Text and not as a PG enum -- adding a role must never need
    #: an ALTER TYPE inside a migration, the same reasoning `extraction_runs.status` uses.
    #: Values are re-validated on the way in; the column is not the check.
    role: str = Field(sa_column=Column(Text, nullable=False))
    invited_by: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


class SpaceInvite(SQLModel, table=True):
    """A single-use link that grants one role in one Space.

    A link, not an email. There is no mailer in this service, and inviting by address
    would need an oracle for "does this person have a Recall account" -- which is exactly
    the enumeration every other surface here refuses to provide. So this is minted like a
    Telegram link token: 32 random bytes handed to the inviter once, only the SHA-256
    kept, single-use, and **unknown / spent / expired all answer the same sentence**.
    A fast hash is right for the same reason it is there: the input is 32 random bytes,
    there is no dictionary to run, and the lookup has to be one indexed equality match.
    """

    __tablename__ = "space_invites"

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    space_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("spaces.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    role: str = Field(sa_column=Column(Text, nullable=False))
    token_hash: str = Field(sa_column=Column(Text, nullable=False, unique=True, index=True))
    created_by: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    #: Set the moment the link is spent. The row is kept rather than deleted: a token
    #: presented after acceptance means two parties held it, and that is worth being able
    #: to see. The purge is by `expires_at` alone, never by this.
    accepted_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    accepted_by: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
