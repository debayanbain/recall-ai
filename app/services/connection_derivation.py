"""Finding the memories a new capture belongs next to.

One question, asked once per capture: *which of this person's other memories is this one
closest to?* The answer is the top few nearest chunks over the HNSW index, filtered by a
floor, written as **suggestions**.

Four things about that are decisions rather than details.

**Everything it writes is `suggested`, never `confirmed`.** A cosine distance says two
memories are close and says nothing else -- two documents that flatly contradict each
other are maximally close -- so the strongest claim the number supports is `related_to`,
and even that is offered rather than filed. It is also the security boundary: an edge is a
way to get one page's text next to another page in a prompt, and the person tapping Yes is
what stands between a scraped caption and the agent's context. See the injection note in
`CLAUDE.md`. Auto-confirming above some threshold deletes that, and no threshold replaces
it.

**The floor is applied here, not in the query.** `search_semantic` returns distances and
knows nothing about thresholds -- the same separation `MemoryRetriever.recall` and
`evidence.assess` make, and for the same reason: a threshold buried in a query is one
nobody can find when the embedding provider changes and it has to be re-measured.

**It compares chunk 0 to everything.** Today `ProcessingService._enrich` writes exactly
one chunk per item, so "chunk 0" and "every chunk" are the same set -- but writing it this
way keeps the derivation one probe on the day real chunking lands. Which *passage* of A is
nearest which passage of B is a better and different design, and it can be added without
changing what an edge means.

**Older memories are never re-scanned.** An edge is discovered from the new capture's side
only, which is what makes this O(1) per capture instead of O(n^2) over the vault. The
older memory still *shows* the edge -- that is what directional storage plus bidirectional
reads buys -- it just never goes looking on its own. A vault that predates the feature
needs a one-off backfill, not a beat task.

No model call. No provider. This is arithmetic over vectors that already exist.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.core.config import settings
from app.core.logging import get_logger
from app.models.vault import VaultItem
from app.repositories.connection import ConnectionRepository
from app.repositories.vault import VaultRepository

log = get_logger("recall.connections")

#: The chunk whose vector stands for the whole memory. See the module docstring.
_PRIMARY_CHUNK = 0


@dataclass(frozen=True, slots=True)
class Candidate:
    """One nearby memory and how near it is, **before** the floor is applied.

    The floor is deliberately not baked into the scan. `derive` applies it, and
    `scripts/backfill_connections.py` reads the unfiltered list so a dry run can show what
    a *different* floor would have done -- over the real vault, for free, because every
    vector it compares was already written by the pipeline. That is the one honest way to
    check `CONNECTION_MIN_SCORE` without paying for a fresh set of embeddings.
    """

    item: VaultItem
    #: `1 - cosine_distance`, clamped -- the same conversion `evidence.py` makes, because
    #: the threshold is written in that scale and nothing else.
    score: float


class ConnectionDeriver:
    def __init__(self, repo: ConnectionRepository, vault_repo: VaultRepository) -> None:
        self.repo = repo
        self.vault_repo = vault_repo

    async def derive(self, item_id: uuid.UUID) -> int:
        """Propose connections for one freshly enriched memory. Returns how many landed.

        Every exit is a number, never an exception: the caller is a Celery task that runs
        *after* the capture has been committed, and nothing here is worth turning a saved
        memory into a failed one.
        """
        item = await self.vault_repo.get_unscoped(item_id)
        if item is None:
            # Deleted between the enrichment and this task. Nothing to connect, and the
            # tombstone must not gain edges.
            log.info("connection_derive_missing_item")
            return 0

        vector = await self._vector_for(item_id)
        if vector is None:
            # No embedding: a `skipped` item (an image with no vision key, a .docx with no
            # readable text) or one whose enrichment failed. Not an error -- there is
            # simply nothing to compare, and saying so is the whole answer.
            log.info("connection_derive_no_vector", item_id=str(item_id))
            return 0

        held = await self.repo.count_for_item(item.user_id, item_id)
        room = settings.CONNECTION_MAX_PER_ITEM - held
        if room <= 0:
            log.info("connection_derive_at_ceiling", held=held)
            return 0

        wanted = min(settings.CONNECTION_MAX_CANDIDATES, room)
        found = await self.candidates(item_id, limit=wanted, vector=vector, item=item)
        floor = settings.CONNECTION_MIN_SCORE
        cleared = [(c.item.id, c.score) for c in found if c.score >= floor]
        written = await self.repo.suggest_many(item.user_id, item_id, cleared)
        log.info(
            "connection_derive_done",
            considered=len(found),
            cleared=len(cleared),
            written=written,
            best=round(found[0].score, 3) if found else None,
        )
        return written

    async def candidates(
        self,
        item_id: uuid.UUID,
        *,
        limit: int | None = None,
        vector: list[float] | None = None,
        item: VaultItem | None = None,
    ) -> list[Candidate]:
        """The nearest other memories and their scores, **unfiltered**, nearest first.

        No floor, because the floor is a separate judgement -- the same separation
        `MemoryRetriever.recall` and `evidence.assess` make, and for the same reason: a
        threshold buried in a query is one nobody can find when the embedding provider
        changes and it has to be re-measured. It is also what lets
        `scripts/backfill_connections.py` report what a *different* floor would have done.

        `vector` and `item` are passed in by `derive`, which has already read both. A
        caller that has neither gets them read here.
        """
        row = item or await self.vault_repo.get_unscoped(item_id)
        if row is None:
            return []
        values = vector if vector is not None else await self._vector_for(item_id)
        if values is None:
            return []
        neighbours = await self.vault_repo.search_semantic(
            row.user_id,
            values,
            limit=limit or settings.CONNECTION_MAX_CANDIDATES,
            exclude_item_id=item_id,
        )
        # The same conversion `evidence.py` makes, and clamped the same way: the distance
        # is what the index returns, the score is what a human threshold is written in.
        return [
            Candidate(item=other, score=max(0.0, min(1.0, 1.0 - distance)))
            for other, distance in neighbours
        ]

    async def _vector_for(self, item_id: uuid.UUID) -> list[float] | None:
        """The memory's own embedding, read back rather than recomputed.

        Recomputing would mean a provider call and a bill, per capture, for a vector the
        pipeline wrote to this row seconds ago.
        """
        chunk = await self.vault_repo.get_chunk(item_id, _PRIMARY_CHUNK)
        if chunk is None or chunk.embedding is None:
            return None
        return [float(value) for value in chunk.embedding]

