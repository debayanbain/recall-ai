"""initial schema: extensions + all tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-21
"""
from __future__ import annotations

from alembic import op
from sqlmodel import SQLModel

# Import registers all tables on SQLModel.metadata.
import app.models  # noqa: F401

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extensions (idempotent — safe to re-run)
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    bind = op.get_bind()
    SQLModel.metadata.create_all(bind=bind)

    # Special indexes Alembic autogenerate cannot emit (opclasses).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vault_items_title_trgm "
        "ON vault_items USING gin (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_vault_items_summary_trgm "
        "ON vault_items USING gin (summary gin_trgm_ops)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    SQLModel.metadata.drop_all(bind=bind)
