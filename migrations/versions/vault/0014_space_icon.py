"""A Space's icon: a Lucide icon name beside the emoji it replaces.

`emoji` stays and is not migrated. A Space created before this revision has a glyph its
owner chose, and rewriting those into icon names would be this migration guessing what
someone meant -- the renderer prefers `icon`, falls back to `emoji`, then to a neutral
mark, so an old Space keeps exactly what it had until someone picks something new.

The column holds a **name** ("book-open"), never a component or an SVG, for the same
reason `accent` holds a key: the icon set is a frontend dependency, and a database that
stores markup from it is a database coupled to that package's version. An unknown name
renders as the fallback rather than as nothing.

Revision ID: 0014_space_icon
Revises: 0013_spaces
Create Date: 2026-09-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_space_icon"
down_revision = "0013_spaces"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Guarded like every revision here: `0001_initial` builds the schema from the current
    # models, so on a fresh database this column already exists and an unguarded
    # ADD COLUMN aborts the whole upgrade inside Alembic's single transaction.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "spaces" not in tables:  # pragma: no cover - create_all always precedes this
        return
    if "icon" not in _columns("spaces"):
        op.add_column("spaces", sa.Column("icon", sa.String(length=48), nullable=True))


def downgrade() -> None:
    if "icon" in _columns("spaces"):
        op.drop_column("spaces", "icon")
