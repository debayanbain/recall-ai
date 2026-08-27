"""VaultItem data access including search and chunk embeddings."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlmodel import col, func, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import ContentType
from app.models.vault import VaultChunk, VaultItem

# Semantic search reads chunks, but callers want items. With more than one chunk per
# item the top-k chunks can all belong to the same item, so we over-fetch and dedupe.
_CHUNK_OVERSAMPLE = 4


class VaultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, item: VaultItem) -> VaultItem:
        self.session.add(item)
        await self.session.flush()
        await self.session.refresh(item)
        return item

    async def get(self, item_id: uuid.UUID, user_id: uuid.UUID) -> VaultItem | None:
        item = await self.session.get(VaultItem, item_id)
        if item is None or item.user_id != user_id:
            return None
        return item

    async def get_by_source_url(
        self, user_id: uuid.UUID, source_url: str
    ) -> VaultItem | None:
        """Find a live item for this user with the same canonical URL."""
        result = await self.session.exec(
            select(VaultItem)
            .where(VaultItem.user_id == user_id)
            .where(VaultItem.source_url == source_url)
            .where(col(VaultItem.deleted_at).is_(None))
            .limit(1)
        )
        return result.first()

    async def get_unscoped(self, item_id: uuid.UUID) -> VaultItem | None:
        """For workers: fetch without user scoping."""
        return await self.session.get(VaultItem, item_id)

    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        base = select(VaultItem).where(
            VaultItem.user_id == user_id,
            col(VaultItem.deleted_at).is_(None),
        )
        total = await self.session.exec(
            select(func.count()).select_from(base.subquery())
        )
        rows = await self.session.exec(
            base.order_by(col(VaultItem.created_at).desc()).limit(limit).offset(offset)
        )
        return rows.all(), total.one()

    async def search(
        self, user_id: uuid.UUID, query: str, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        """Phase 1 search: case-insensitive ILIKE over title/summary/content."""
        pattern = f"%{query}%"
        base = select(VaultItem).where(
            VaultItem.user_id == user_id,
            col(VaultItem.deleted_at).is_(None),
            or_(
                col(VaultItem.title).ilike(pattern),
                col(VaultItem.summary).ilike(pattern),
                col(VaultItem.content).ilike(pattern),
            ),
        )
        total = await self.session.exec(
            select(func.count()).select_from(base.subquery())
        )
        rows = await self.session.exec(
            base.order_by(col(VaultItem.created_at).desc()).limit(limit).offset(offset)
        )
        return rows.all(), total.one()

    async def list_filtered(
        self,
        user_id: uuid.UUID,
        *,
        limit: int = 20,
        offset: int = 0,
        created_after: datetime | None = None,
        content_types: Sequence[ContentType] | None = None,
        category: str | None = None,
        tags: Sequence[str] | None = None,
    ) -> tuple[Sequence[VaultItem], int]:
        """Newest-first listing with the filters a natural-language query produces.

        Rides `ix_vault_items_user_created`, and `ix_vault_items_ai_tags` (GIN,
        jsonb_path_ops) when tags are supplied.
        """
        base = select(VaultItem).where(
            VaultItem.user_id == user_id,
            col(VaultItem.deleted_at).is_(None),
        )
        if created_after is not None:
            base = base.where(col(VaultItem.created_at) >= created_after)
        if content_types:
            base = base.where(col(VaultItem.type).in_(list(content_types)))
        if category:
            base = base.where(VaultItem.ai_category == category)
        if tags:
            # `@>` on jsonb: the item's tag array must contain every tag asked for.
            base = base.where(col(VaultItem.ai_tags).contains(list(tags)))

        total = await self.session.exec(
            select(func.count()).select_from(base.subquery())
        )
        rows = await self.session.exec(
            base.order_by(col(VaultItem.created_at).desc()).limit(limit).offset(offset)
        )
        return rows.all(), total.one()

    async def search_semantic(
        self,
        user_id: uuid.UUID,
        vector: list[float],
        *,
        limit: int = 8,
        created_after: datetime | None = None,
        content_types: Sequence[ContentType] | None = None,
        category: str | None = None,
    ) -> list[tuple[VaultItem, float]]:
        """Nearest-neighbour search over the user's own chunks, closest first.

        The query vector MUST come from the same provider that wrote the stored ones:
        Gemini's 768 dims are zero-padded to 1536 and are not comparable with OpenAI's
        native 1536. Mixing them returns confident nonsense rather than an error.

        `user_id` is applied to both tables. `vault_chunks.user_id` is the one that keeps
        the index scan inside the caller's own rows; the predicate on `vault_items` is
        deliberate duplication, so a future chunk written with the wrong owner cannot
        leak through this path.

        Ordering is a bare `ORDER BY embedding <=> $1 LIMIT n`, which is the only shape
        the HNSW index serves. Deduplication by item happens in Python for the same
        reason -- a DISTINCT ON would make the planner drop the index.
        """
        # pgvector's distance operators live on the Vector type's comparator. SQLModel
        # cannot express a Vector column, so the model declares `embedding` as
        # `Any | None` and the operator is invisible to the type checker.
        embedding: Any = col(VaultChunk.embedding)
        distance = embedding.cosine_distance(vector).label("distance")
        query = (
            select(VaultChunk.vault_item_id, distance)
            .join(VaultItem, col(VaultChunk.vault_item_id) == col(VaultItem.id))
            .where(
                VaultChunk.user_id == user_id,
                VaultItem.user_id == user_id,
                col(VaultItem.deleted_at).is_(None),
                embedding.is_not(None),
            )
        )
        if created_after is not None:
            query = query.where(col(VaultItem.created_at) >= created_after)
        if content_types:
            query = query.where(col(VaultItem.type).in_(list(content_types)))
        if category:
            query = query.where(VaultItem.ai_category == category)

        result = await self.session.exec(
            query.order_by(distance).limit(limit * _CHUNK_OVERSAMPLE)
        )

        best: dict[uuid.UUID, float] = {}
        for item_id, dist in result.all():
            if item_id not in best:
                best[item_id] = float(dist)
            if len(best) >= limit:
                break
        if not best:
            return []

        # One round trip for the rows themselves, then restored to distance order: the
        # IN clause loses the ordering the index just established.
        items = await self.session.exec(
            select(VaultItem).where(col(VaultItem.id).in_(list(best)))
        )
        by_id = {item.id: item for item in items.all()}
        return sorted(
            ((by_id[i], d) for i, d in best.items() if i in by_id),
            key=lambda pair: pair[1],
        )

    async def delete(self, item: VaultItem) -> None:
        await self.session.delete(item)

    async def upsert_chunk(
        self,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        vector: list[float],
        content: str,
        chunk_index: int = 0,
        token_count: int | None = None,
    ) -> None:
        result = await self.session.exec(
            select(VaultChunk).where(
                VaultChunk.vault_item_id == item_id,
                VaultChunk.chunk_index == chunk_index,
            )
        )
        existing = result.first()
        if existing is None:
            self.session.add(
                VaultChunk(
                    vault_item_id=item_id,
                    user_id=user_id,
                    chunk_index=chunk_index,
                    content=content,
                    embedding=vector,
                    token_count=token_count,
                )
            )
        else:
            existing.embedding = vector
            existing.content = content
            existing.token_count = token_count
            self.session.add(existing)
