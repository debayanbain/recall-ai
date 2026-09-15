"""trash bin: separate a recoverable deletion from a real one

Adds `vault_items.purged_at` and an index on `(user_id, deleted_at)`.

`DELETE /vault/{id}` used to scrub the row and delete the bucket object in one step, so a
mistaken tap was unrecoverable at the instant it happened. It now only sets `deleted_at`:
every read already filters that column, so the memory leaves the vault, search, chat
retrieval and the worker immediately -- but nothing is destroyed. The destruction is the
beat task `purge_expired_trash`, TRASH_RETENTION_DAYS later.

That needs a way to tell the two states apart, and `deleted_at` alone cannot: a purged row
still carries it. `purged_at` is what marks the row as already scrubbed, so the trash page
lists what can still come back rather than empty shells of memories that are gone.

Both statements are guarded. `0001_initial` builds the schema with
`SQLModel.metadata.create_all()`, so on a fresh database the column and the index already
exist, and an unguarded ADD COLUMN / CREATE INDEX would abort the entire upgrade (Alembic
wraps it in one transaction, so the database rolls back to empty).

Nothing is backfilled, and that is the safe direction: rows deleted before this migration
were already scrubbed by the old delete, so leaving `purged_at` NULL on them would offer a
restore that can only return an empty memory. They are stamped as purged instead -- the
one place this migration writes data, and it writes it to rows that hold nothing.

Revision ID: 0017_trash_purged_at
Revises: 0016_memory_connections
Create Date: 2026-09-15
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_trash_purged_at"
down_revision = "0016_memory_connections"
branch_labels = None
depends_on = None

TABLE = "vault_items"
COLUMN = "purged_at"
INDEX = "ix_vault_items_user_deleted"


def _existing_columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(TABLE)}


def _existing_indexes() -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(TABLE)}


def upgrade() -> None:
    if COLUMN not in _existing_columns():
        op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
        # Every row already deleted was scrubbed by the previous delete implementation.
        # Marking them purged keeps them out of the trash listing, where they would
        # otherwise appear as restorable memories with no title, no body and no file.
        op.execute(
            sa.text(
                f"UPDATE {TABLE} SET {COLUMN} = deleted_at WHERE deleted_at IS NOT NULL"
            )
        )
    if INDEX not in _existing_indexes():
        op.create_index(INDEX, TABLE, ["user_id", "deleted_at"])


def downgrade() -> None:
    if INDEX in _existing_indexes():
        op.drop_index(INDEX, table_name=TABLE)
    if COLUMN in _existing_columns():
        op.drop_column(TABLE, COLUMN)
