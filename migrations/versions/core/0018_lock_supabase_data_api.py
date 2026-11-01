"""close Supabase's Data API over the app's tables

Supabase serves every table in `public` over its REST Data API
(`https://<ref>.supabase.co/rest/v1/<table>`), and its default privileges grant those
tables to the `anon` and `authenticated` roles the moment they are created. The anon key
that authenticates as `anon` is *publishable* -- Supabase's own docs put it in browser
code. Nothing in this app uses the Data API, so with row level security off, the
`users`, `user_sessions`, `oauth_accounts` and `vault_items` tables would be readable
and writable by anyone holding a key that was never meant to be a secret.

Two locks, either of which is enough on its own:

  * **Grants revoked** from `anon` and `authenticated` on every table and sequence in
    `public`, and the default privileges for the migrating role revoked too, so a table
    a LATER migration creates is not quietly granted again.
  * **Row level security enabled** with no policies, which denies those roles every
    row. The app is unaffected: it connects as the tables' owner, and an owner bypasses
    RLS unless it is FORCEd, which this does not do.

Functions are deliberately left alone. EXECUTE on a function is granted to PUBLIC, not
to `anon`, and the functions in `public` here are pgvector / citext / pg_trgm's own --
revoking PUBLIC's EXECUTE on those is how the app's own vector operators stop working.

Gated on the `anon` role existing, which is Supabase's signature: on Neon this is a
no-op, so a database the app already runs on is not changed by it. Idempotent -- REVOKE
of a privilege not held and ENABLE on an enabled table both succeed -- which every
migration here has to be, because `0001_initial` builds the schema with create_all().

Revision ID: 0018_lock_supabase_data_api
Revises: 0017_trash_purged_at
Create Date: 2026-10-24
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_lock_supabase_data_api"
down_revision = "0017_trash_purged_at"
branch_labels = None
depends_on = None

# Fixed names, interpolated as identifiers below because GRANT/REVOKE cannot take bind
# parameters. Only roles from this tuple that actually exist are ever used.
_API_ROLES = ("anon", "authenticated")


def upgrade() -> None:
    bind = op.get_bind()
    existing = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
            {"names": list(_API_ROLES)},
        )
    }
    if "anon" not in existing:
        return  # not Supabase
    roles = ", ".join(f'"{name}"' for name in _API_ROLES if name in existing)

    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {roles}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {roles}")
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {roles}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {roles}"
    )

    tables = bind.execute(
        sa.text(
            # format('%I') quotes the identifier server-side, so no name is spliced raw.
            "SELECT format('%I', tablename) FROM pg_tables "
            "WHERE schemaname = 'public' AND tableowner = current_user"
        )
    ).scalars()
    for table in tables:
        op.execute(sa.text(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY"))


def downgrade() -> None:
    # Deliberately not reversed: re-granting the Data API roles would re-open every table
    # to the publishable anon key, which is not something a schema downgrade should do as
    # a side effect. Re-grant by hand if the Data API is ever genuinely wanted.
    pass
