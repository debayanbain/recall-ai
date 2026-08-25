"""add 'facebook' to the contenttype enum

Facebook reels are their own source, and squeezing them into `article` or `instagram`
would make the type column lie about where a memory came from.

`ALTER TYPE ... ADD VALUE` is allowed inside a transaction on PostgreSQL 12+ *provided the
new value is not used in that same transaction* — this migration only declares it, so the
Alembic transaction is fine. IF NOT EXISTS keeps it idempotent alongside `0001_initial`,
which builds the enum from the current models on a fresh database.

Revision ID: 0007_content_type_facebook
Revises: 0006_extraction_runs
Create Date: 2026-08-25
"""
from __future__ import annotations

from alembic import op

revision = "0007_content_type_facebook"
down_revision = "0006_extraction_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE contenttype ADD VALUE IF NOT EXISTS 'facebook'")


def downgrade() -> None:
    # PostgreSQL cannot drop an enum value. Removing it would mean recreating the type
    # and rewriting every dependent column, which is not worth it for a downgrade path.
    pass
