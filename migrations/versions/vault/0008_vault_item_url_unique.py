"""one live item per (user, url)

Backstop for the duplicate check in `VaultService.save_url`. The service lookup handles
the normal case, but two concurrent saves of the same link can both miss it and insert —
and every duplicate costs another paid scrape plus another round of AI calls.

Partial on `deleted_at IS NULL` so deleting an item frees the URL to be saved again.

Revision ID: 0008_vault_item_url_unique
Revises: 0007_content_type_facebook
Create Date: 2026-08-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_vault_item_url_unique"
down_revision = "0007_content_type_facebook"
branch_labels = None
depends_on = None

_INDEX = "uq_vault_items_user_source_url_live"


def upgrade() -> None:
    bind = op.get_bind()
    if _INDEX in {i["name"] for i in sa.inspect(bind).get_indexes("vault_items")}:
        return
    # Existing duplicates would make the index creation fail, so collapse them first,
    # keeping the oldest row of each group.
    op.execute(
        """
        DELETE FROM vault_items a USING vault_items b
        WHERE a.user_id = b.user_id
          AND a.source_url = b.source_url
          AND a.source_url IS NOT NULL
          AND a.deleted_at IS NULL AND b.deleted_at IS NULL
          AND a.created_at > b.created_at
        """
    )
    op.create_index(
        _INDEX,
        "vault_items",
        ["user_id", "source_url"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND source_url IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="vault_items")
