"""An edge between two of one person's memories.

One table, `memory_connections`, and four decisions in it that are not obvious:

**One row, directional.** `source_item_id -> target_item_id`, and the *read* returns the
edge from whichever end asked, labelled with the inverse. Storing two rows per edge would
be two sources of truth that can disagree after a retype, with nothing to arbitrate --
the same reason ownership lives only in `spaces.user_id`. Storing one *undirected* row
would make `part_of` and `depends_on` unrenderable, since those say different things in
each direction. Bidirectional navigation is a read concern, and it is solved by reading.

**The unique key excludes the relation.** `(user_id, pair_low, pair_high)` over two
generated columns, so a pair has at most one edge however it is labelled and whichever
order it arrives in. Re-typing is an `UPDATE`, not a second row: two labelled edges
between the same two cards is not a thing anyone wants rendered, and it gives "already
connected" a clean answer instead of a duplicate.

**`pair_low` / `pair_high` are GENERATED and are never written by application code.**
SQLAlchemy knows they are `Computed` and leaves them out of every INSERT on its own, so
both `session.add(MemoryConnection(...))` and `pg_insert(...).values({...})` are safe --
what is *not* safe is naming either column in a `values()` dict, which Postgres refuses.
They exist so the unique constraint is order-independent: an edge is one edge whichever
end it was drawn from, and asking Postgres for `least`/`greatest` is cheaper and more
honest than asking every caller to sort two ids correctly.

**There are two text columns and they are not interchangeable.** `note` is the person's
own words. `ai_reason` is model output derived from scraped pages, one-lined and capped
*on the way in* (never at render time -- a redaction that happens on one render path is
one the second render path forgets), and it is rendered marked as machine-written.
Putting a model's sentence in `note` would show it as something its owner typed.

No `sqlmodel.Relationship` -- there are none in this repo. Joins are written out in
`app/repositories/connection.py` so the cost of a page is visible at the call site.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlmodel import Field, SQLModel

from app.models.base import ConnectionOrigin, ConnectionStatus, Relation, new_uuid, utcnow

#: The person's own note on an edge. Long enough for "this is the counter-argument",
#: short enough that it is a caption and not a second memory.
NOTE_MAX = 500
#: A model's one-line account of why two memories relate. Bounded for the same reason
#: every other model output here is, and shorter than `note` because nobody asked for it.
AI_REASON_MAX = 200


class MemoryConnection(SQLModel, table=True):
    __tablename__ = "memory_connections"
    __table_args__ = (
        # One edge per unordered pair, per owner. This is also the `ON CONFLICT` target
        # for both the derivation (DO NOTHING) and a manual create (DO UPDATE), which is
        # what makes re-deriving after a `reprocess` a no-op and what makes a dismissal
        # stick without a second table.
        UniqueConstraint("user_id", "pair_low", "pair_high", name="uq_memory_connections_pair"),
        # Defence in depth. The request schema rejects a self-edge first, with a 422 and
        # no round trip; this is what stops one arriving by any other route.
        CheckConstraint("source_item_id <> target_item_id", name="ck_memory_connections_no_self"),
        # A neighbourhood read is a UNION ALL of two halves rather than `WHERE source = ?
        # OR target = ?`, so each half rides its own index instead of relying on the
        # planner to build a BitmapOr. Same species of care as `search_semantic` refusing
        # `DISTINCT ON`: the shape is chosen so the index cannot be declined.
        Index("ix_memory_connections_source", "user_id", "source_item_id"),
        Index("ix_memory_connections_target", "user_id", "target_item_id"),
        # The suggestions inbox, which is per user and not per item: an edge discovered
        # from the new memory's side is a suggestion on *both* memories' pages, so
        # offering it per item offers it twice.
        Index("ix_memory_connections_status", "user_id", "status"),
    )

    id: uuid.UUID = Field(default_factory=new_uuid, primary_key=True)
    #: The owner of both ends. Every read re-applies it, and it is never a tool argument
    #: or a request field -- it comes from the resolved session and nowhere else.
    user_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            index=True,
            nullable=False,
        )
    )
    source_item_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("vault_items.id", ondelete="CASCADE"),
            nullable=False,
        )
    )
    target_item_id: uuid.UUID = Field(
        sa_column=Column(
            PGUUID(as_uuid=True),
            ForeignKey("vault_items.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    #: `Relation`, stored as Text and re-validated in Python. See the enum's docstring.
    relation: str = Field(
        sa_column=Column(Text, nullable=False, server_default=Relation.related_to.value)
    )
    #: `ConnectionOrigin`.
    origin: str = Field(
        sa_column=Column(Text, nullable=False, server_default=ConnectionOrigin.user.value)
    )
    #: `ConnectionStatus`.
    status: str = Field(
        sa_column=Column(Text, nullable=False, server_default=ConnectionStatus.confirmed.value)
    )

    #: The similarity this edge was derived at, 0..1. NULL for a hand-made one -- a
    #: number invented for an edge a person drew would be a measurement nobody took.
    score: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    #: The person's own words.
    note: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    #: A model's. Scrubbed and one-lined before it gets here.
    ai_reason: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    #: The unordered pair, computed by Postgres. Never written by application code, never
    #: serialized to a client: this is an index, not data.
    pair_low: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            Computed("least(source_item_id, target_item_id)", persisted=True),
            nullable=False,
        ),
    )
    pair_high: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            PGUUID(as_uuid=True),
            Computed("greatest(source_item_id, target_item_id)", persisted=True),
            nullable=False,
        ),
    )

    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    #: When a suggestion became real. NULL while it is still only offered.
    confirmed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    #: When it was declined. The row is kept rather than deleted -- it is what stops the
    #: same pair being proposed again on the next capture.
    dismissed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
