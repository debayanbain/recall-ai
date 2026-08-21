"""schema sync: align existing DB to canonical schema (files/schema.sql)

Revision ID: 0002_schema_sync
Revises: 0001_initial
Create Date: 2026-06-24

Safe to run on a DB that already has the correct schema (IF NOT EXISTS / IF EXISTS guards).
"""
from __future__ import annotations

from alembic import op

revision = "0002_schema_sync"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- extensions (idempotent) -------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # ---- users ---------------------------------------------------------------
    # Replace google_sub with auth_provider + provider_account_id
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth_provider TEXT NOT NULL DEFAULT 'google'")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS provider_account_id TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='users' AND column_name='google_sub') THEN
                -- Migrate existing google_sub into provider_account_id
                UPDATE users SET provider_account_id = google_sub WHERE provider_account_id IS NULL;
                ALTER TABLE users DROP COLUMN google_sub;
            END IF;
        END $$;
    """)
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_name='users' AND constraint_name='uq_users_provider'
            ) THEN
                ALTER TABLE users ADD CONSTRAINT uq_users_provider
                    UNIQUE (auth_provider, provider_account_id);
            END IF;
        END $$;
    """)

    # ---- vault_items ---------------------------------------------------------
    op.execute("ALTER TABLE vault_items ADD COLUMN IF NOT EXISTS language TEXT")
    op.execute("ALTER TABLE vault_items ADD COLUMN IF NOT EXISTS retry_count INT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE vault_items ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE vault_items ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='vault_items' AND column_name='error') THEN
                ALTER TABLE vault_items RENAME COLUMN error TO processing_error;
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE vault_items ADD COLUMN IF NOT EXISTS processing_error TEXT")

    # ---- vault_chunks (replaces vault_embeddings 1:1 -> 1:many) -------------
    op.execute("DROP TABLE IF EXISTS vault_embeddings")
    op.execute("""
        CREATE TABLE IF NOT EXISTS vault_chunks (
            id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
            vault_item_id  UUID         NOT NULL REFERENCES vault_items(id) ON DELETE CASCADE,
            user_id        UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            chunk_index    INT          NOT NULL,
            content        TEXT         NOT NULL,
            embedding      VECTOR(1536),
            token_count    INT,
            created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
            UNIQUE (vault_item_id, chunk_index)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_vault_chunks_user ON vault_chunks (user_id)")
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_vault_chunks_embedding_hnsw
        ON vault_chunks USING hnsw (embedding vector_cosine_ops)
    """)

    # ---- collections ---------------------------------------------------------
    op.execute("ALTER TABLE collections ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE collections ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")

    # ---- collection_items ---------------------------------------------------
    # Old schema had id UUID PK; new schema uses composite PK (collection_id, vault_item_id)
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='collection_items' AND column_name='id') THEN
                ALTER TABLE collection_items DROP CONSTRAINT IF EXISTS collection_items_pkey;
                ALTER TABLE collection_items DROP COLUMN IF EXISTS id;
                ALTER TABLE collection_items ADD COLUMN IF NOT EXISTS position INT NOT NULL DEFAULT 0;
                -- Re-add composite PK only if not yet primary key
                ALTER TABLE collection_items ADD PRIMARY KEY (collection_id, vault_item_id);
            END IF;
        END $$;
    """)
    op.execute("ALTER TABLE collection_items ADD COLUMN IF NOT EXISTS position INT NOT NULL DEFAULT 0")

    # ---- subscriptions -------------------------------------------------------
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS current_period_start TIMESTAMPTZ")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMPTZ")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.table_constraints
                WHERE table_name='subscriptions' AND constraint_name='subscriptions_user_id_key'
            ) THEN
                ALTER TABLE subscriptions ADD CONSTRAINT subscriptions_user_id_key UNIQUE (user_id);
            END IF;
        END $$;
    """)

    # ---- audit_log ----------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id           BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id      UUID         REFERENCES users(id) ON DELETE SET NULL,
            action       TEXT         NOT NULL,
            entity_type  TEXT,
            entity_id    UUID,
            ip           INET,
            request_id   TEXT,
            metadata     JSONB        NOT NULL DEFAULT '{}'::jsonb,
            created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_user_time ON audit_log (user_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_entity ON audit_log (entity_type, entity_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TABLE IF EXISTS vault_chunks")
    op.execute("ALTER TABLE vault_items DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE vault_items DROP COLUMN IF EXISTS processed_at")
    op.execute("ALTER TABLE vault_items DROP COLUMN IF EXISTS retry_count")
    op.execute("ALTER TABLE vault_items DROP COLUMN IF EXISTS language")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS provider_account_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS auth_provider")
    op.execute("ALTER TABLE collections DROP COLUMN IF EXISTS deleted_at")
    op.execute("ALTER TABLE collections DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS cancel_at_period_end")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS current_period_end")
    op.execute("ALTER TABLE subscriptions DROP COLUMN IF EXISTS current_period_start")
