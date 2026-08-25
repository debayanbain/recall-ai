"""uploaded document storage on vault items

Adds the Backblaze B2 object pointer to `vault_items` and the `document` content type for
uploads that are not PDFs.

Guarded like 0004/0005/0006: `0001_initial` builds the schema with
`SQLModel.metadata.create_all()`, so on a fresh database these columns already exist and
an unguarded ADD COLUMN would abort the whole upgrade (Alembic wraps it in one
transaction, so the database would roll back to empty).

Revision ID: 0010_document_storage
Revises: 0009_user_sessions
Create Date: 2026-08-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_document_storage"
down_revision = "0009_user_sessions"
branch_labels = None
depends_on = None

TABLE = "vault_items"

COLUMNS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("storage_key", sa.String(length=512)),
    ("file_name", sa.String(length=255)),
    ("file_size", sa.BigInteger()),
    ("mime_type", sa.String(length=128)),
)


def _existing_columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    # `ALTER TYPE ... ADD VALUE` is fine inside a transaction on PostgreSQL 12+ as long as
    # the new value is not *used* in the same transaction. This only declares it.
    op.execute("ALTER TYPE contenttype ADD VALUE IF NOT EXISTS 'document'")

    present = _existing_columns()
    for name, column_type in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    present = _existing_columns()
    for name, _ in COLUMNS:
        if name in present:
            op.drop_column(TABLE, name)
    # PostgreSQL cannot drop an enum value; recreating the type and rewriting every
    # dependent column is not worth a downgrade path.
