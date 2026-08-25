"""make every timestamp column timezone-aware

`utcnow()` returns an aware datetime, but only `updated_at` declared
`DateTime(timezone=True)`. Every other timestamp compiled to TIMESTAMP WITHOUT TIME
ZONE, so asyncpg rejected every ORM insert with "can't subtract offset-naive and
offset-aware datetimes" -- no row could be written through SQLModel at all.

Existing values are naive and were written as UTC, so `AT TIME ZONE 'UTC'` reinterprets
them without shifting the instant.

Revision ID: 0003_timestamptz
Revises: 0002_schema_sync
Create Date: 2026-08-21
"""
from __future__ import annotations

from alembic import op

revision = "0003_timestamptz"
down_revision = "0002_schema_sync"
branch_labels = None
depends_on = None

# (table, column) pairs that were created without a timezone.
_COLUMNS = [
    ("users", "created_at"),
    ("users", "deleted_at"),
    ("vault_items", "created_at"),
    ("vault_items", "deleted_at"),
    ("vault_items", "processed_at"),
    ("vault_chunks", "created_at"),
    ("collections", "created_at"),
    ("collections", "deleted_at"),
    ("collection_items", "added_at"),
    ("subscriptions", "created_at"),
    ("subscriptions", "current_period_start"),
    ("subscriptions", "current_period_end"),
    ("audit_log", "created_at"),
]


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMPTZ "
            f"USING {column} AT TIME ZONE 'UTC'"
        )


def downgrade() -> None:
    for table, column in _COLUMNS:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMP "
            f"USING {column} AT TIME ZONE 'UTC'"
        )
