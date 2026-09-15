"""VaultItem data access including search and chunk embeddings."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import load_only
from sqlmodel import col, func, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import ContentType, ProcessingStatus
from app.models.connection import MemoryConnection
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
        """One item, if it is this user's and still exists.

        A deleted row answers `None`, exactly as a missing one does -- the same reason
        "not yours" answers `None` rather than raising: a caller that can tell the three
        apart is a caller that can enumerate them.
        """
        item = await self.session.get(VaultItem, item_id)
        if item is None or item.user_id != user_id or item.deleted_at is not None:
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
        """For workers: fetch without user scoping. Still honours deletion.

        Skipping the tenant check is what this exists for -- the worker has no request
        user. Skipping the *deletion* check was never part of that: an item deleted while
        it was queued must not be processed, summarised or replied about, and the caller
        already treats `None` as "nothing to do here" (`process_missing_item`).
        """
        item = await self.session.get(VaultItem, item_id)
        if item is None or item.deleted_at is not None:
            return None
        return item

    async def owned_items(
        self, item_ids: Sequence[uuid.UUID] | set[uuid.UUID], user_id: uuid.UUID
    ) -> dict[uuid.UUID, VaultItem]:
        """The caller's own live rows among these ids, keyed by id. ONE statement.

        For the callers that act on *several* memories at once -- connecting two of them,
        adding a selection to a Space -- where the check has to be per item rather than
        per request. A caller who owns one id and borrowed the other must not get through,
        and a body carrying two ids is exactly where a stranger's is easiest to slip in.
        `SpaceService._attach` makes the same check one item at a time; this is that rule
        in one round trip.

        Returns the rows rather than the ids because every caller needs both: the check
        is `len(result) == len(asked)`, and the rows are what the response renders, so
        answering with ids alone buys a second round trip to fetch what was just read.

        Soft-deleted rows are excluded, so a tombstone is never a valid endpoint.
        """
        wanted = list(dict.fromkeys(item_ids))
        if not wanted:
            return {}
        result = await self.session.exec(
            select(VaultItem).where(
                col(VaultItem.id).in_(wanted),
                VaultItem.user_id == user_id,
                col(VaultItem.deleted_at).is_(None),
            )
        )
        return {item.id: item for item in result.all()}

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
        # Read by the trash listing to say how many days a memory has left. A
        # `cards_only` row that did not load it would hit `raiseload` instead.
        "deleted_at",
        "file_name",
        "file_size",
        "mime_type",
        # Not serialised -- it is what `services/thumbnails.presigned_urls` signs to
        # produce the card's picture, and reading it off a `cards_only` row would
        # otherwise hit `raiseload`.
        "thumbnail_key",
    )

    async def _page(
        self,
        base: Any,
        limit: int,
        offset: int,
        *,
        cards_only: bool = False,
        order_by: Any = None,
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
            # Newest first everywhere except the trash, which is read by when a memory
            # was deleted rather than by when it was saved -- "what did I just throw
            # away" is the question that page exists to answer.
            .order_by(order_by if order_by is not None else col(VaultItem.created_at).desc())
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
        statuses: Sequence[ProcessingStatus] | None = None,
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
        if statuses:
            base = base.where(col(VaultItem.processing_status).in_(list(statuses)))

        return await self._page(base, limit, offset)

    async def search_by_tags(
        self,
        user_id: uuid.UUID,
        tags: Sequence[str],
        *,
        limit: int = 10,
        exclude_item_id: uuid.UUID | None = None,
    ) -> list[VaultItem]:
        """The caller's memories carrying any of these tags, newest first. ONE statement.

        The half of connection recall a vector search cannot do. Two notes about one job
        application written three months apart both carry `jobs` and `visa` and can still
        sit far apart in embedding space -- a summary about a form and a summary about an
        interview genuinely are different text. The tags are the subject the enrichment
        already worked out, and they are free to reuse.

        **Written as an OR of containments, on purpose.** `ai_tags` is JSONB behind a GIN
        `jsonb_path_ops` index, which serves `@>` and nothing else: `?|` (has any key)
        would be the obvious operator and that index cannot answer it, so the query would
        fall back to a sequential scan over the whole vault. One `@>` per tag is one
        index scan per tag, which the planner unions itself.

        Overlap is counted in Python by the caller rather than in SQL. Ranking inside the
        statement would mean a lateral over `jsonb_array_elements` per row, and the
        candidate list is already bounded to `limit` -- the cost of being approximate here
        is that a memory sharing three tags can lose a slot to one sharing one, and what
        sits behind that is a judge that reads both.

        `deleted_at` is filtered, so a tombstone is never a candidate. Card columns only:
        every caller renders these into a prompt or a card, and none reads a body.
        """
        wanted = [tag for tag in dict.fromkeys(t.strip() for t in tags) if tag][:8]
        if not wanted or limit < 1:
            return []
        matches: Any = or_(
            *(col(VaultItem.ai_tags).contains([tag]) for tag in wanted)
        )
        query = (
            select(VaultItem)
            .where(
                VaultItem.user_id == user_id,
                col(VaultItem.deleted_at).is_(None),
                matches,
            )
            .order_by(col(VaultItem.created_at).desc())
            .limit(limit)
            .options(
                load_only(
                    *(getattr(VaultItem, name) for name in self._CARD_COLUMNS),
                    raiseload=True,
                )
            )
        )
        if exclude_item_id is not None:
            query = query.where(col(VaultItem.id) != exclude_item_id)
        rows = (await self.session.execute(query)).scalars().all()
        return list(rows)

    async def search_semantic(
        self,
        user_id: uuid.UUID,
        vector: list[float],
        *,
        limit: int = 8,
        created_after: datetime | None = None,
        content_types: Sequence[ContentType] | None = None,
        category: str | None = None,
        exclude_item_id: uuid.UUID | None = None,
    ) -> list[tuple[VaultItem, float]]:
        """Nearest-neighbour search over the user's own chunks, closest first.

        The query vector MUST come from the same provider that wrote the stored ones:
        Gemini's 768 dims are zero-padded to 1536 and are not comparable with OpenAI's
        native 1536. Mixing them returns confident nonsense rather than an error.

        `exclude_item_id` keeps one item off its own list, which is what connection
        derivation needs: it searches with a vector drawn *from* a row, so that row is its
        own nearest neighbour at distance zero and would otherwise take a candidate slot
        every time.

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
        if exclude_item_id is not None:
            # For connection derivation, whose query vector belongs to one of these rows.
            # A memory is always its own nearest neighbour at distance zero, so without
            # this the strongest candidate every time is the item itself.
            query = query.where(col(VaultChunk.vault_item_id) != exclude_item_id)

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

    async def list_slow_captures(
        self,
        source: str,
        older_than_minutes: int,
        limit: int = 100,
    ) -> Sequence[VaultItem]:
        """Captures from `source` that are still working and have not been nudged.

        Deliberately **not** `list_stranded`, which is the sweeper's query and the wrong
        one here in two ways. It measures `updated_at`, which is when a worker last
        touched the row rather than when the person sent it -- and the person is timing
        their own wait. And it excludes items with a running extraction run, which are
        the deferred Apify crawls: those legitimately sit in `processing` for minutes and
        are exactly the captures whose silence gets reported as a bug.

        `nudged_at` is checked in SQL rather than in Python so a backlog of already-nudged
        rows can never crowd out the ones still waiting for their first message.
        """
        cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
        metadata = col(VaultItem.item_metadata)
        result = await self.session.exec(
            select(VaultItem)
            .where(
                col(VaultItem.processing_status).in_(
                    (ProcessingStatus.pending, ProcessingStatus.processing)
                ),
                col(VaultItem.created_at) < cutoff,
                col(VaultItem.deleted_at).is_(None),
                metadata["source"].astext == source,
                metadata["nudged_at"].astext.is_(None),
            )
            .order_by(col(VaultItem.created_at))
            .limit(limit)
        )
        return result.all()

    async def get_trashed(
        self, item_id: uuid.UUID, user_id: uuid.UUID
    ) -> VaultItem | None:
        """One of this user's trashed memories, if it can still come back.

        Four ways to answer `None`, and they are deliberately one answer: no such row,
        not yours, not deleted, or already purged. A caller that can tell them apart can
        enumerate ids that are not theirs, which is the reason `get()` collapses the same
        cases.

        A purged row is excluded because there is nothing left in it to restore -- the
        scrub already ran.
        """
        item = await self.session.get(VaultItem, item_id)
        if (
            item is None
            or item.user_id != user_id
            or item.deleted_at is None
            or item.purged_at is not None
        ):
            return None
        return item

    async def list_trashed(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        """The trash page: this user's deleted-but-recoverable memories, newest first."""
        base = select(VaultItem).where(
            VaultItem.user_id == user_id,
            col(VaultItem.deleted_at).is_not(None),
            col(VaultItem.purged_at).is_(None),
        )
        return await self._page(
            base,
            limit,
            offset,
            cards_only=True,
            order_by=col(VaultItem.deleted_at).desc(),
        )

    async def list_trash_rows(
        self, user_id: uuid.UUID, limit: int
    ) -> Sequence[VaultItem]:
        """Full rows of one user's trash, for "empty trash".

        Full rows and not cards: the caller has to read `storage_key` and `thumbnail_key`
        before the scrub clears them, and a `cards_only` row would raise on both.
        """
        result = await self.session.exec(
            select(VaultItem)
            .where(VaultItem.user_id == user_id)
            .where(col(VaultItem.deleted_at).is_not(None))
            .where(col(VaultItem.purged_at).is_(None))
            .order_by(col(VaultItem.deleted_at))
            .limit(limit)
        )
        return result.all()

    async def list_expired_trash(
        self, cutoff: datetime, limit: int
    ) -> Sequence[VaultItem]:
        """Rows whose trash window has run out, oldest first.

        Unscoped, like `get_unscoped`, and for the same caller: the beat task has no
        request user. It is bounded because the purge deletes bucket objects one at a
        time -- an unbounded sweep would hold a transaction open across hundreds of
        network calls, and whatever is left is taken by the next tick anyway.
        """
        result = await self.session.exec(
            select(VaultItem)
            .where(col(VaultItem.deleted_at).is_not(None))
            .where(col(VaultItem.deleted_at) < cutoff)
            .where(col(VaultItem.purged_at).is_(None))
            .order_by(col(VaultItem.deleted_at))
            .limit(limit)
        )
        return result.all()

    async def trash(self, item: VaultItem) -> None:
        """Move a memory to the trash: gone from everywhere, destroyed nowhere.

        Every read in this file filters `deleted_at`, so writing it is the whole of the
        removal as far as the product is concerned -- the item leaves listings, search,
        chat retrieval, connections and the worker on the same statement. What it does
        *not* do is scrub the row, delete the chunks or touch the bucket. That is
        `purge`, and it runs TRASH_RETENTION_DAYS later.

        The chunks stay on purpose. They carry the words and the vector drawn from them,
        which is exactly what a restore has to put back -- and re-deriving them would mean
        re-embedding, so a restore would cost money and could quietly fail. They are
        already invisible: `search_semantic` joins the item and filters `deleted_at`.
        """
        item.deleted_at = datetime.now(UTC)
        self.session.add(item)
        await self.session.flush()

    async def restore(self, item: VaultItem) -> None:
        """Put a trashed memory back. Nothing to rebuild -- nothing was taken away.

        Callers reach this through `get_trashed`, which already refuses a purged row, so
        this cannot resurrect a tombstone into an empty memory.
        """
        item.deleted_at = None
        self.session.add(item)
        await self.session.flush()

    async def purge(self, item: VaultItem) -> None:
        """The real deletion: scrub the row and take the derived data with it.

        This is the old `delete`, now reached either when the trash window runs out or
        when the owner asks for it outright. It is the irreversible half, which is why it
        is a separate call from `trash` rather than a flag on it.

        **The content is scrubbed, not kept.** A tombstone exists for referential
        integrity and so a row's absence is explainable; it is not a copy of a memory
        somebody asked to be rid of. Keeping the title and body around past the trash
        window is exactly the thing a person deleting a memory does not want, and it
        would sit in the database until the account was closed. What survives is the id,
        the owner, the kind and the timestamps -- enough to explain a gap, not enough to
        reconstruct anything.

        The chunks go with it. They are derived data carrying the same words *and* the
        vector drawn from them, so leaving them would keep the deleted text searchable in
        the one index built to find it.
        """
        item.purged_at = datetime.now(UTC)
        # A purge reached directly ("delete forever") never passed through `trash`, so
        # this may be the first tombstone the row gets. Set rather than overwritten: the
        # trash window is measured from when the user deleted it, not from the purge.
        if item.deleted_at is None:
            item.deleted_at = item.purged_at
        item.title = None
        item.summary = None
        item.content = None
        item.source_url = None
        item.thumbnail_url = None
        item.language = None
        item.ai_tags = []
        item.ai_highlights = []
        item.ai_label = None
        item.ai_category = None
        item.item_metadata = {}
        item.processing_error = None
        # The objects themselves are removed by `VaultService`, which reads the keys
        # before calling this. Cleared here so nothing can mint a presigned URL to bytes
        # that are on their way out.
        item.storage_key = None
        item.thumbnail_key = None
        item.file_name = None
        item.file_size = None
        item.mime_type = None
        self.session.add(item)
        await self.session.flush()
        await self.session.execute(
            sa_delete(VaultChunk).where(col(VaultChunk.vault_item_id) == item.id)
        )
        # The connections go too, and hard, for the same reason the chunks do: an edge is
        # derived data asserting "this memory is about the same thing as that one", which
        # is a statement about content somebody asked to be rid of. Leaving them would
        # also leave a tombstone visibly related to a live memory on that memory's own
        # page. Removing the rows frees the pair as well, so a deletion cannot
        # permanently block a connection someone draws later between what remains.
        #
        # They survive the *trash* stage, where they are already hidden by every read
        # filtering the neighbour's `deleted_at` -- which is what lets a restore bring a
        # memory back with its neighbourhood intact.
        await self.session.execute(
            sa_delete(MemoryConnection).where(
                or_(
                    col(MemoryConnection.source_item_id) == item.id,
                    col(MemoryConnection.target_item_id) == item.id,
                )
            )
        )

    async def get_chunk(self, item_id: uuid.UUID, chunk_index: int = 0) -> VaultChunk | None:
        """One chunk of one item, by its position.

        Unscoped, like `get_unscoped`, and for the same caller: the worker has no request
        user. It is reachable only from the derivation task, which then scopes every read
        it makes by the item's own `user_id`.
        """
        result = await self.session.exec(
            select(VaultChunk).where(
                col(VaultChunk.vault_item_id) == item_id,
                VaultChunk.chunk_index == chunk_index,
            )
        )
        return result.first()

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
