"""linked OAuth identities + verified-email flag

Adds `oauth_accounts` so one user can hold several third-party logins (Google,
Facebook/Instagram, X) instead of the single `users.auth_provider` pair, and
`users.email_verified` so account linking by email can be gated on a provider having
actually verified the address.

Provider tokens are stored as Fernet ciphertext (see `app.core.crypto`); the columns are
named `*_encrypted` so a plaintext write is obvious in review.

Idempotent on purpose: `0001_initial` builds the schema with
`SQLModel.metadata.create_all()`, i.e. from the *current* models rather than a frozen
snapshot. On a brand-new database 0001 therefore already creates everything below, and an
unguarded ADD COLUMN / CREATE TABLE here aborts the whole upgrade. The guards make this
revision a no-op on a fresh DB while still applying to one created before these models
existed. Any future migration in this repo needs the same treatment.

Revision ID: 0004_oauth_accounts
Revises: 0003_timestamptz
Create Date: 2026-08-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_oauth_accounts"
down_revision = "0003_timestamptz"
branch_labels = None
depends_on = None



def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _has_table(name: str) -> bool:
    return name in _inspector().get_table_names()


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def upgrade() -> None:
    if not _has_column("users", "email_verified"):
        op.add_column(
            "users",
            sa.Column(
                "email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")
            ),
        )
    # Every pre-existing row came in through Google, which only hands back verified
    # addresses, so backfill them as verified rather than silently demoting them.
    op.execute("UPDATE users SET email_verified = true WHERE auth_provider = 'google'")

    if not _has_table("oauth_accounts"):
        _create_oauth_accounts()

    # Idempotent on their own: the UPDATE is a no-op when already applied and the INSERT
    # carries ON CONFLICT DO NOTHING, so both are safe on a re-run.
    op.execute("UPDATE users SET email_verified = true WHERE auth_provider = 'google'")
    op.execute(
        """
        INSERT INTO oauth_accounts (id, user_id, provider, provider_account_id, email,
                                    email_verified, name, avatar_url)
        SELECT gen_random_uuid(), id, auth_provider, provider_account_id, email,
               email_verified, name, avatar_url
        FROM users
        WHERE provider_account_id IS NOT NULL AND auth_provider IS NOT NULL
        ON CONFLICT (provider, provider_account_id) DO NOTHING
        """
    )


def _create_oauth_accounts() -> None:
    op.create_table(
        "oauth_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_account_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column(
            "email_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
            "provider", "provider_account_id", name="uq_oauth_provider_account"
        ),
    )
    op.create_index("ix_oauth_accounts_user_id", "oauth_accounts", ["user_id"])

    # Carry the existing single-provider links over so nobody is logged out by this migration.
    op.execute(
        """
        INSERT INTO oauth_accounts (id, user_id, provider, provider_account_id, email,
                                    email_verified, name, avatar_url)
        SELECT gen_random_uuid(), id, auth_provider, provider_account_id, email,
               email_verified, name, avatar_url
        FROM users
        WHERE provider_account_id IS NOT NULL AND auth_provider IS NOT NULL
        ON CONFLICT (provider, provider_account_id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_oauth_accounts_user_id", table_name="oauth_accounts")
    op.drop_table("oauth_accounts")
    op.drop_column("users", "email_verified")
