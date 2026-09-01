"""Collections become Spaces, and Spaces gain members and invites.

Three things happen here and the order matters.

**The rename.** `collections` -> `spaces`, `collection_items` -> `space_items`, and the
join table's `collection_id` -> `space_id`. Product and code now use one word for one
thing; carrying two was how `grep "space"` came back missing the model layer.

**The new columns** on `spaces`: presentation (`emoji`, `accent`, `pinned`), the
model-written overview (`ai_overview`, `ai_topics`, `overview_generated_at`) and the
connection cache (`connection_count`, `connections_computed_at`).

**The new tables**: `space_members` (a person and their role) and `space_invites` (a
single-use link that grants one role once).

Every step is guarded with an inspector, and it is not optional here. `0001_initial` runs
`SQLModel.metadata.create_all()` against the *current* models, so on a fresh database
`spaces` and friends already exist by the time this revision runs and `collections` never
did -- while on an existing database the opposite is true. An unguarded statement in
either direction aborts the whole upgrade inside Alembic's single transaction, and the
database rolls back to empty.

`accent` stores a key ("violet"), never a CSS class: a column holding
`from-violet-200 via-indigo-100` is a column coupled to a Tailwind version.

Revision ID: 0013_spaces
Revises: 0012_ai_label_highlights
Create Date: 2026-08-31
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_spaces"
down_revision = "0012_ai_label_highlights"
branch_labels = None
depends_on = None


#: (name, type, nullable, server_default) for each column added to `spaces`.
_NEW_SPACE_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object], bool, str | None], ...] = (
    ("emoji", sa.String(length=16), True, None),
    ("accent", sa.String(length=24), True, None),
    ("pinned", sa.Boolean(), False, "false"),
    ("ai_overview", sa.Text(), True, None),
    ("ai_topics", postgresql.JSONB(), False, "'[]'::jsonb"),
    ("overview_generated_at", sa.DateTime(timezone=True), True, None),
    ("connection_count", sa.Integer(), True, None),
    ("connections_computed_at", sa.DateTime(timezone=True), True, None),
)


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    tables = _tables()

    # --- rename, only on a database that still has the old names ---
    if "collections" in tables and "spaces" not in tables:
        op.rename_table("collections", "spaces")
        op.rename_table("collection_items", "space_items")
        op.alter_column("space_items", "collection_id", new_column_name="space_id")
        # IF EXISTS on every rename: the index and constraint names depend on how the
        # schema was built (create_all names them differently from a hand-written
        # migration), and a missing one must not abort the upgrade.
        for old, new in (
            ("ix_collection_items_item", "ix_space_items_item"),
            ("ix_collections_user_id", "ix_spaces_user_id"),
        ):
            op.execute(f"ALTER INDEX IF EXISTS {old} RENAME TO {new}")
        op.execute("ALTER TABLE spaces RENAME CONSTRAINT collections_pkey TO spaces_pkey")
        op.execute(
            "ALTER TABLE space_items RENAME CONSTRAINT collection_items_pkey TO space_items_pkey"
        )
        tables = _tables()

    if "spaces" not in tables:  # pragma: no cover - create_all always precedes this
        return

    # --- new columns on spaces ---
    existing = _columns("spaces")
    for name, type_, nullable, default in _NEW_SPACE_COLUMNS:
        if name not in existing:
            op.add_column(
                "spaces",
                sa.Column(
                    name,
                    type_,
                    nullable=nullable,
                    server_default=sa.text(default) if default else None,
                ),
            )

    # --- who added a memory to a Space ---
    # SET NULL, not CASCADE: a departed member's contributions stop being attributed but
    # the Space does not silently lose rows other people are reading.
    if "added_by" not in _columns("space_items"):
        op.add_column(
            "space_items",
            sa.Column("added_by", postgresql.UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_space_items_added_by",
            "space_items",
            "users",
            ["added_by"],
            ["id"],
            ondelete="SET NULL",
        )

    # --- membership ---
    if "space_members" not in tables:
        op.create_table(
            "space_members",
            sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            # Text, not a PG enum: adding a role must never need an ALTER TYPE inside a
            # migration. The value is validated in Python on the way in.
            sa.Column("role", sa.Text(), nullable=False),
            sa.Column("invited_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.PrimaryKeyConstraint("space_id", "user_id"),
            sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["invited_by"], ["users.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_space_members_user", "space_members", ["user_id"])

    # --- invites ---
    if "space_invites" not in tables:
        op.create_table(
            "space_invites",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("space_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("role", sa.Text(), nullable=False),
            # Only the digest. The raw token exists in the link the inviter copies and
            # nowhere else, so a database dump yields no usable invite.
            sa.Column("token_hash", sa.Text(), nullable=False),
            sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("accepted_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["space_id"], ["spaces.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["accepted_by"], ["users.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("token_hash", name="uq_space_invites_token"),
        )
        op.create_index("ix_space_invites_space_id", "space_invites", ["space_id"])
        op.create_index("ix_space_invites_token_hash", "space_invites", ["token_hash"])


def downgrade() -> None:
    tables = _tables()
    if "space_invites" in tables:
        op.drop_index("ix_space_invites_token_hash", table_name="space_invites")
        op.drop_index("ix_space_invites_space_id", table_name="space_invites")
        op.drop_table("space_invites")
    if "space_members" in tables:
        op.drop_index("ix_space_members_user", table_name="space_members")
        op.drop_table("space_members")
    if "space_items" in tables and "added_by" in _columns("space_items"):
        op.drop_constraint("fk_space_items_added_by", "space_items", type_="foreignkey")
        op.drop_column("space_items", "added_by")
    if "spaces" in tables:
        existing = _columns("spaces")
        for name, *_ in _NEW_SPACE_COLUMNS:
            if name in existing:
                op.drop_column("spaces", name)
        op.execute("ALTER TABLE spaces RENAME CONSTRAINT spaces_pkey TO collections_pkey")
        op.execute(
            "ALTER TABLE space_items RENAME CONSTRAINT space_items_pkey TO collection_items_pkey"
        )
        for new, old in (
            ("ix_space_items_item", "ix_collection_items_item"),
            ("ix_spaces_user_id", "ix_collections_user_id"),
        ):
            op.execute(f"ALTER INDEX IF EXISTS {new} RENAME TO {old}")
        op.alter_column("space_items", "space_id", new_column_name="collection_id")
        op.rename_table("space_items", "collection_items")
        op.rename_table("spaces", "collections")
