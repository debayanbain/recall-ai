"""per-memory AI label and verbatim highlight spans

`ai_label` is the one line that distinguishes a memory from its neighbours (tags are
topical and collide on purpose). `ai_highlights` holds sentences copied verbatim out of
`content` so the UI can mark them in place.

Guarded like 0004/0005/0006/0010: `0001_initial` builds the schema with
`SQLModel.metadata.create_all()`, so on a fresh database these columns already exist and
an unguarded ADD COLUMN would abort the whole upgrade (Alembic wraps it in one
transaction, so the database would roll back to empty).

`ai_highlights` is NOT NULL with a `'[]'` server default to match the model, which types
it as `list[str]` rather than `list[str] | None` — the same shape `ai_tags` already has,
so nothing downstream has to branch on null vs empty.

Revision ID: 0012_ai_label_highlights
Revises: 0011_telegram_accounts
Create Date: 2026-08-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012_ai_label_highlights"
down_revision = "0011_telegram_accounts"
branch_labels = None
depends_on = None

TABLE = "vault_items"

COLUMNS: tuple[tuple[str, sa.Column[object]], ...] = (
    ("ai_label", sa.Column("ai_label", sa.String(length=120), nullable=True)),
    (
        "ai_highlights",
        sa.Column(
            "ai_highlights", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    ),
)


def _existing_columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    present = _existing_columns()
    for name, column in COLUMNS:
        if name not in present:
            op.add_column(TABLE, column)


def downgrade() -> None:
    present = _existing_columns()
    for name, _ in COLUMNS:
        if name in present:
            op.drop_column(TABLE, name)
