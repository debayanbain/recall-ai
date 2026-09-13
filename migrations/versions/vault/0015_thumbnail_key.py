"""mirrored card thumbnail on vault items

Adds `vault_items.thumbnail_key`: the object key of our own copy of a link's `og:image`.
A scraped still is a *signed* fbcdn/cdninstagram URL that stops resolving about a week
after the capture, so `thumbnail_url` alone means every link card silently loses its
picture -- see `app/services/thumbnails.py`.

Guarded like 0010: `0001_initial` builds the schema with `SQLModel.metadata.create_all()`,
so on a fresh database this column already exists and an unguarded ADD COLUMN would abort
the whole upgrade (Alembic wraps it in one transaction, so the database would roll back to
empty).

Nothing is backfilled. The stills this would have copied are already expired, and the
column being NULL is exactly what makes a card fall back to the scraped URL it has.

Revision ID: 0015_thumbnail_key
Revises: 0014_space_icon
Create Date: 2026-09-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_thumbnail_key"
down_revision = "0014_space_icon"
branch_labels = None
depends_on = None

TABLE = "vault_items"
COLUMN = "thumbnail_key"


def _existing_columns() -> set[str]:
    return {col["name"] for col in sa.inspect(op.get_bind()).get_columns(TABLE)}


def upgrade() -> None:
    if COLUMN not in _existing_columns():
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=512), nullable=True))


def downgrade() -> None:
    if COLUMN in _existing_columns():
        op.drop_column(TABLE, COLUMN)
