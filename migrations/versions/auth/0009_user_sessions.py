"""user_sessions: server-side refresh tokens

Sessions stop being "whatever the JWT says" and become rows: a short access token plus a
rotating, revocable refresh token (see `app.services.session_service`).

Guarded like 0004/0005: `0001_initial` builds the schema with
`SQLModel.metadata.create_all()`, so on a fresh database this table already exists by the
time this revision runs, and an unguarded CREATE TABLE would abort the whole upgrade --
Alembic wraps it in one transaction, so the database would roll back to empty.

Revision ID: 0009_user_sessions
Revises: 0008_vault_item_url_unique
Create Date: 2026-08-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_user_sessions"
down_revision = "0008_vault_item_url_unique"
branch_labels = None
depends_on = None

_TABLE = "user_sessions"


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        # SHA-256 hex of the opaque refresh token. The raw value is never stored, so a
        # dump of this table yields nothing anyone can log in with.
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "family_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Deleting a user must not leave live sessions behind that could still be
        # redeemed against a resurrected id.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_user_sessions_user_id", _TABLE, ["user_id"])
    # Unique: every refresh lookup is an equality match on this column, and two rows
    # sharing a digest would make "which session is this" ambiguous.
    op.create_index("ix_user_sessions_token_hash", _TABLE, ["token_hash"], unique=True)
    op.create_index("ix_user_sessions_family_id", _TABLE, ["family_id"])
    op.create_index("ix_user_sessions_expires_at", _TABLE, ["expires_at"])


def downgrade() -> None:
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
