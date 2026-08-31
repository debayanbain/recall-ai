"""VaultItem data access including search and chunk embeddings."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy.orm import load_only
from sqlmodel import col, func, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import ContentType, ProcessingStatus
from app.models.extraction_run import ExtractionRun, RunStatus
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

    #: The columns a card needs, which is what `VaultItemRead` serializes. A listing that
    #: selects `*` also drags `content`, `item_metadata` and `ai_highlights` across the
    #: wire for every row -- an article body is kilobytes, a page is twenty of them, and
    #: none of it is rendered. Loaded with `raiseload` so a field added to the list
    #: response without being added here fails loudly here rather than emitting a silent
    #: per-row query (or a `DetachedInstanceError` after the session closes).
    _CARD_COLUMNS = (
        "type",
        "source_url",
        "title",
        "summary",
        "thumbnail_url",
        "ai_tags",
        "ai_category",
        "ai_label",
        "processing_status",
        "processing_error",
        "created_at",
        "file_name",
        "file_size",
        "mime_type",
    )

    async def _page(
        self,
        base: Any,
        limit: int,
        offset: int,
        *,
        cards_only: bool = False,
    ) -> tuple[Sequence[VaultItem], int]:
        """Run a filtered listing and its total in ONE round trip.

        The obvious shape is two statements -- `SELECT count(*)` then `SELECT ... LIMIT`
        -- and against a local database that is free. Against a managed one in another
        region each statement is a full network round trip (~290ms measured to Neon
        ap-southeast-1), so the count silently doubled the cost of every list request.

        `count(*) OVER ()` computes the same total inside the same scan and rides back on
        every row. The one behavioural difference is that a page past the end returns no
        rows and therefore no count: that is reported as 0, which is what the caller does
        with an out-of-range offset anyway.

        `base` is a SQLModel `select(VaultItem)` with the tenant predicate already
        applied. It is never built from caller-supplied SQL -- every filter that reaches
        it is a bound parameter -- so this adds no injection surface.
        """
        total_col = func.count().over().label("total")
        query = (
            base.add_columns(total_col)
            .order_by(col(VaultItem.created_at).desc())
            .limit(limit)
            .offset(offset)
        )
        if cards_only:
            query = query.options(
                load_only(
                    *(getattr(VaultItem, name) for name in self._CARD_COLUMNS),
                    raiseload=True,
                )
            )
        # `session.execute`, not `session.exec`: SQLModel's `exec()` narrows a select back
        # to its single entity and silently drops the extra column, so the window total
        # would never arrive (and the row would unpack as the model's own fields).
        rows = await self.session.execute(query)
        pairs = rows.all()
        if not pairs:
            # No rows means no window total either. An offset past the end is the only
            # way to get here on a non-empty vault, and 0 is what the caller does with it.
            return [], 0
        return [cast("VaultItem", row[0]) for row in pairs], int(pairs[0][1])

    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        base = select(VaultItem).where(
            VaultItem.user_id == user_id,
            col(VaultItem.deleted_at).is_(None),
        )
        return await self._page(base, limit, offset, cards_only=True)

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
        return await self._page(base, limit, offset, cards_only=True)

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

        return await self._page(base, limit, offset)

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
        #
        # The tenant predicate is repeated here even though every id came from the scoped
        # query above. Fetching rows by id alone is the shape that turns any upstream
        # scoping mistake into a cross-tenant read, and this is the last query before the
        # rows reach a prompt -- so it states the constraint rather than inheriting it.
        items = await self.session.exec(
            select(VaultItem).where(
                col(VaultItem.id).in_(list(best)),
                VaultItem.user_id == user_id,
                col(VaultItem.deleted_at).is_(None),
            )
        )
        by_id = {item.id: item for item in items.all()}
        return sorted(
            ((by_id[i], d) for i, d in best.items() if i in by_id),
            key=lambda pair: pair[1],
        )

    async def list_stranded(
        self,
        status: ProcessingStatus,
        older_than_minutes: int,
        limit: int = 100,
    ) -> Sequence[VaultItem]:
        """Items sitting in `status` with nobody behind them.

        `updated_at` carries an `onupdate`, so for a `processing` row it is the moment the
        worker claimed it — which is exactly the clock a stall should be measured against.

        Items with a **running extraction run** are excluded. Those are the deferred
        Apify captures, which legitimately sit in `processing` for minutes while a crawl
        runs; `sweep_stale_runs` owns them and asks the provider what actually happened.
        Two sweepers reaching for the same row would race to a verdict, and the one with
        less information would sometimes win.

        Ordered oldest-first and capped so one sweep tick cannot try to rescue the whole
        table after an outage.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        running_runs = select(ExtractionRun.vault_item_id).where(
            ExtractionRun.status == RunStatus.running
        )
        result = await self.session.exec(
            select(VaultItem)
            .where(
                VaultItem.processing_status == status,
                col(VaultItem.updated_at) < cutoff,
                col(VaultItem.deleted_at).is_(None),
                col(VaultItem.id).not_in(running_runs),
            )
            .order_by(col(VaultItem.updated_at))
            .limit(limit)
        )
        return result.all()

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
