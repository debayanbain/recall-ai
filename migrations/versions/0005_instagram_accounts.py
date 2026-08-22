"""connected Instagram Business accounts

Instagram is a resource grant, not an identity, so it gets its own table rather than a
row in `oauth_accounts`: one user can connect several IG accounts (one per Facebook Page
they manage), and what is stored is a Page access token used to read media -- not a
"who is this person" assertion.

Both token columns hold Fernet ciphertext (see `app.core.crypto`); the `*_encrypted`
suffix is there so a plaintext write stands out in review.

Guarded like 0004: `0001_initial` builds the schema with
`SQLModel.metadata.create_all()`, so on a fresh database it already creates this table
from the current models and an unguarded CREATE TABLE would abort the whole upgrade.

Revision ID: 0005_instagram_accounts
Revises: 0004_oauth_accounts
Create Date: 2026-08-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_instagram_accounts"
down_revision = "0004_oauth_accounts"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("instagram_accounts"):
        return
    op.create_table(
        "instagram_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instagram_user_id", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("profile_picture_url", sa.Text(), nullable=True),
        sa.Column("page_id", sa.Text(), nullable=False),
        sa.Column("page_name", sa.Text(), nullable=True),
        sa.Column("page_access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("user_access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
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
        sa.UniqueConstraint(
            "user_id", "instagram_user_id", name="uq_instagram_user_account"
        ),
    )
    op.create_index("ix_instagram_accounts_user_id", "instagram_accounts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_instagram_accounts_user_id", table_name="instagram_accounts")
    op.drop_table("instagram_accounts")
