"""Telegram account bindings and their one-shot link tokens.

Two tables rather than a column on `users`: a Telegram binding is a *connection*, like
`instagram_accounts`, and a user may have none. The link tokens are separate again
because they are credentials with a ten-minute life -- keeping them in their own table
means the daily purge is a bounded DELETE by `expires_at` and never touches the binding.

`telegram_user_id` is unique GLOBALLY, not per user. A per-user constraint would let a
second account claim a Telegram identity someone else already linked, and every later
message from that chat would resolve to whichever row was found first.

Guarded with an inspector because `0001_initial` builds the schema from the *current*
models via `create_all`, so on a fresh database these tables already exist by the time
this revision runs; an unguarded CREATE TABLE would abort the whole upgrade.

Revision ID: 0011_telegram_accounts
Revises: 0010_document_storage
Create Date: 2026-08-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_telegram_accounts"
down_revision = "0010_document_storage"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has_table("telegram_accounts"):
        op.create_table(
            "telegram_accounts",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("telegram_user_id", sa.Text(), nullable=False),
            sa.Column("telegram_chat_id", sa.Text(), nullable=False),
            sa.Column("username", sa.Text(), nullable=True),
            sa.Column("first_name", sa.Text(), nullable=True),
            sa.Column("last_name", sa.Text(), nullable=True),
            sa.Column("language_code", sa.Text(), nullable=True),
            sa.Column(
                "linked_at",
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
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("telegram_user_id", name="uq_telegram_account_user"),
        )
        op.create_index("ix_telegram_accounts_user_id", "telegram_accounts", ["user_id"])

    if not _has_table("telegram_link_tokens"):
        op.create_table(
            "telegram_link_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("token_hash", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_telegram_link_tokens_user_id", "telegram_link_tokens", ["user_id"])
        op.create_index(
            "ix_telegram_link_tokens_token_hash",
            "telegram_link_tokens",
            ["token_hash"],
            unique=True,
        )
        op.create_index(
            "ix_telegram_link_tokens_expires_at", "telegram_link_tokens", ["expires_at"]
        )


def downgrade() -> None:
    op.drop_index("ix_telegram_link_tokens_expires_at", table_name="telegram_link_tokens")
    op.drop_index("ix_telegram_link_tokens_token_hash", table_name="telegram_link_tokens")
    op.drop_index("ix_telegram_link_tokens_user_id", table_name="telegram_link_tokens")
    op.drop_table("telegram_link_tokens")
    op.drop_index("ix_telegram_accounts_user_id", table_name="telegram_accounts")
    op.drop_table("telegram_accounts")
