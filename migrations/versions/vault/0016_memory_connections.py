"""memory-to-memory connections

Adds `memory_connections`: one row per edge between two of one person's memories, with a
relation, an origin (who drew it) and a status (suggested / confirmed / dismissed). See
`app/models/connection.py` for why the row is directional, why the unique key excludes
the relation, and why a dismissal is a status rather than a second table.

Guarded like 0013 and 0015: `0001_initial` runs `SQLModel.metadata.create_all()` against
the *current* models, so on a fresh database this table already exists by the time this
revision runs -- and an unguarded CREATE TABLE would abort the whole upgrade, since
Alembic wraps it in one transaction and the database would roll back to empty.

`pair_low` / `pair_high` are GENERATED ALWAYS ... STORED and are declared here exactly as
the model declares them, so the `create_all` path and the Alembic path emit the same DDL.
They exist to make the unique constraint order-independent: an edge is one edge whichever
end it was drawn from.

Revision ID: 0016_memory_connections
Revises: 0015_thumbnail_key
Create Date: 2026-09-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016_memory_connections"
down_revision = "0015_thumbnail_key"
branch_labels = None
depends_on = None

TABLE = "memory_connections"

_INDEXES = (
    "ix_memory_connections_source",
    "ix_memory_connections_target",
    "ix_memory_connections_status",
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if TABLE in _tables():
        return

    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation", sa.Text(), server_default="related_to", nullable=False),
        sa.Column("origin", sa.Text(), server_default="user", nullable=False),
        sa.Column("status", sa.Text(), server_default="confirmed", nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("ai_reason", sa.Text(), nullable=True),
        sa.Column(
            "pair_low",
            postgresql.UUID(as_uuid=True),
            sa.Computed("least(source_item_id, target_item_id)", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "pair_high",
            postgresql.UUID(as_uuid=True),
            sa.Computed("greatest(source_item_id, target_item_id)", persisted=True),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_item_id"], ["vault_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_item_id"], ["vault_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "pair_low", "pair_high", name="uq_memory_connections_pair"
        ),
        sa.CheckConstraint(
            "source_item_id <> target_item_id", name="ck_memory_connections_no_self"
        ),
    )
    op.create_index(op.f("ix_memory_connections_user_id"), TABLE, ["user_id"])
    op.create_index("ix_memory_connections_source", TABLE, ["user_id", "source_item_id"])
    op.create_index("ix_memory_connections_target", TABLE, ["user_id", "target_item_id"])
    op.create_index("ix_memory_connections_status", TABLE, ["user_id", "status"])


def downgrade() -> None:
    if TABLE not in _tables():
        return
    for name in _INDEXES:
        op.drop_index(name, table_name=TABLE)
    op.drop_index(op.f("ix_memory_connections_user_id"), table_name=TABLE)
    op.drop_table(TABLE)
