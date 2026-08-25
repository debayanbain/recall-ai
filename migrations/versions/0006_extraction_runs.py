"""deferred extraction run tracking

Correlates a provider run id back to a vault item so a webhook arriving minutes later can
find its way home, and gives the sweeper something to query for runs that never called
back.

Guarded like 0004/0005: `0001_initial` builds the schema with
`SQLModel.metadata.create_all()`, so on a fresh database this table already exists and an
unguarded CREATE TABLE would abort the whole upgrade.

Revision ID: 0006_extraction_runs
Revises: 0005_instagram_accounts
Create Date: 2026-08-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_extraction_runs"
down_revision = "0005_instagram_accounts"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("extraction_runs"):
        return
    op.create_table(
        "extraction_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("vault_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_run_id", sa.Text(), nullable=False),
        sa.Column("dataset_id", sa.Text(), nullable=True),
        # Text, not a PG enum: adding a state later must not require ALTER TYPE outside
        # a transaction, which is what makes enum migrations awkward here.
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["vault_item_id"], ["vault_items.id"], ondelete="CASCADE"),
        # The webhook is at-least-once and the sweeper races it, so the provider's run id
        # is the idempotency key.
        sa.UniqueConstraint("provider", "provider_run_id", name="uq_extraction_run_provider"),
    )
    op.create_index("ix_extraction_runs_vault_item_id", "extraction_runs", ["vault_item_id"])
    op.create_index("ix_extraction_runs_status", "extraction_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_extraction_runs_status", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_vault_item_id", table_name="extraction_runs")
    op.drop_table("extraction_runs")
