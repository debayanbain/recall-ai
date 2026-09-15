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

---

**Recall is two halves now, and a model decides between them.** The version that shipped
first was arithmetic end to end: nearest few vectors, one floor, write them all as
`related_to`. It worked exactly as designed and produced an inbox of four copies of the
same reel with no reason attached, because a cosine distance cannot tell "these are the
same video" from "these are both in Bengali" from "these are genuinely two halves of one
subject". Three stages replace that single number:

1. **Recall** -- deliberately over-offers. The vector half runs at
   `CONNECTION_RECALL_FLOOR`, well *below* the old decision floor, and a second half
   searches `ai_tags`: two notes about one job application share `jobs` and `visa` and
   can still sit far apart in embedding space, because a summary about a form and a
   summary about an interview genuinely are different text. Union, dedupe, cap.
2. **Signals** -- free, deterministic, computed here: how many tags the two share,
   whether the category matches, the similarity, and whether they came from the same URL.
   These are facts about two rows and they go to the judge as evidence, not as prose it
   has to infer.
3. **Judgement** -- one call, all candidates at once (`ai/connection_judge.py`). Most of
   what it does is say no.

**The judge is an improvement on the floor, never a prerequisite for it.** Off,
unconfigured, or failed, `_floor_only` is what runs: `CONNECTION_MIN_SCORE` over the
recall list, everything `related_to`. That is the behaviour this module shipped with, and
it is honest rather than merely degraded. The ladder is the same shape as
`RecallAgentService` falling back to `RecallChatService` -- the older path is the better
tested one.

**Nothing here writes a `confirmed` edge, judged or not.** A judge that is right nine
times in ten still writes a wrong edge every tenth capture, and an edge is a route from
one page's text into another memory's prompt context. Confidence sorts the inbox and
feeds a floor; it never replaces the tap.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.ai import connection_judge
from app.core.config import settings
from app.core.logging import get_logger
from app.models.vault import VaultItem
from app.repositories.connection import ConnectionRepository, Proposal
from app.repositories.vault import VaultRepository
from app.services.chat_engine.cards import build_card

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
    #:
    #: **None for a candidate the tag half found and the vector half did not.** Not zero:
    #: "not measured" and "not similar" are different claims, `memory_connections.score`
    #: is nullable for the same reason, and the floor-only path deliberately refuses to
    #: propose a candidate it has no number for.
    score: float | None


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

        found = await self._recall(item, vector, room=room)
        if not found:
            log.info("connection_derive_no_candidates")
            return 0

        proposals = await self._decide(item, found)
        written = await self.repo.suggest_many(item.user_id, item_id, proposals)
        log.info(
            "connection_derive_done",
            considered=len(found),
            proposed=len(proposals),
            written=written,
            best=round(found[0].score, 3) if found[0].score is not None else None,
        )
        return written

    # ---- 1. recall -----------------------------------------------------------

    async def _recall(
        self, item: VaultItem, vector: list[float], *, room: int
    ) -> list[Candidate]:
        """Everything worth weighing, from both halves, nearest-and-most-shared first.

        Over-offers on purpose: `_decide` is what throws away, and a candidate never
        recalled is one nothing can consider. The cap is the *judge's* ceiling rather than
        `CONNECTION_MAX_CANDIDATES`, which stays what the floor-only path writes -- the
        two numbers answer different questions ("how many may be read" against "how many
        may be written") and collapsing them means raising one to raise the other.

        Ranked by shared tags first and similarity second. A memory sharing three of the
        vault's own tags is a better candidate than one half a point closer in a space
        nobody can inspect, and when the judge is off the score floor still decides.
        """
        ceiling = min(
            settings.CONNECTION_JUDGE_MAX_CANDIDATES,
            connection_judge.MAX_CANDIDATES,
        )
        by_id: dict[uuid.UUID, Candidate] = {}

        # The vector half, at the recall floor rather than the decision floor.
        near = await self.candidates(
            item.id, limit=ceiling * 2, vector=vector, item=item
        )
        for candidate in near:
            # `candidates()` always scores what it returns; the None case belongs to the
            # tag half below. Written as a guard rather than asserted, because the type
            # says the field is optional and a future caller of `candidates()` reading
            # this as "always a float" is the bug this avoids.
            if (
                candidate.score is not None
                and candidate.score >= settings.CONNECTION_RECALL_FLOOR
            ):
                by_id[candidate.item.id] = candidate

        # The tag half. Only reached when the enrichment produced tags -- a `skipped`
        # item or one whose enrichment failed contributes nothing here, and asking with an
        # empty list would be a statement that cannot match.
        tags = _tags_of(item)
        if tags:
            others = await self.vault_repo.search_by_tags(
                item.user_id,
                sorted(tags),
                limit=settings.CONNECTION_TAG_CANDIDATES,
                exclude_item_id=item.id,
            )
            for other in others:
                # `score=None` where the vector half never saw it: "not measured" and
                # "not similar" are different claims, and the column is nullable for
                # exactly this. A candidate the vector half *did* find keeps its score.
                by_id.setdefault(other.id, Candidate(item=other, score=None))

        ranked = sorted(
            by_id.values(),
            key=lambda c: (
                len(tags & _tags_of(c.item)),
                c.score if c.score is not None else 0.0,
            ),
            reverse=True,
        )
        return ranked[: max(0, min(ceiling, room))]

    # ---- 2. judgement --------------------------------------------------------

    async def _decide(
        self, item: VaultItem, found: list[Candidate]
    ) -> list[Proposal]:
        """Which of the recalled memories are really connected, and how.

        Degrades to `_floor_only` on every path that is not a clean judgement: the switch
        being off, no key, a provider failure, or an answer with nothing kept in it that
        cleared the confidence floor. The fallback is the behaviour this module shipped
        with and is the better tested path -- the same ladder `RecallAgentService`
        climbs down.
        """
        if not connection_judge.judge_available():
            return self._floor_only(found)

        tags = _tags_of(item)
        canonical = _canonical_url(item)
        keyed = {f"c{index + 1}": candidate for index, candidate in enumerate(found)}
        payload = connection_judge.JudgeInput(
            subject_card=build_card(item),
            candidates=tuple(
                connection_judge.JudgeCandidate(
                    key=key,
                    card=build_card(candidate.item),
                    shared_tags=tuple(sorted(tags & _tags_of(candidate.item))),
                    same_category=_same_category(item, candidate.item),
                    vector_score=candidate.score,
                    near_duplicate=bool(canonical)
                    and _canonical_url(candidate.item) == canonical,
                )
                for key, candidate in keyed.items()
            ),
            declined_hints=await self._declined_hints(item.user_id),
        )

        try:
            judgements = await connection_judge.judge_connections(payload)
        except connection_judge.ConnectionJudgeFailed as exc:
            # Never the capture's failure, and never the derivation's either: by the time
            # this runs the memory is saved, enriched and answered for.
            log.info("connection_judge_failed", error=type(exc).__name__)
            return self._floor_only(found)

        floor = settings.CONNECTION_JUDGE_MIN_CONFIDENCE
        proposals: list[Proposal] = []
        for judgement in judgements:
            candidate = keyed.get(judgement.key)
            if candidate is None:
                # Unreachable: `_validate` already drops a key that was not sent. Checked
                # anyway, because this is the boundary where a stray key would become a
                # row -- and a `None` here would be an AttributeError inside a Celery task
                # rather than a candidate quietly skipped.
                continue
            if not judgement.keep or judgement.confidence < floor:
                continue
            proposals.append(
                Proposal(
                    target_id=candidate.item.id,
                    score=candidate.score,
                    relation=judgement.relation,
                    ai_reason=judgement.reason or None,
                    swap=judgement.swap,
                )
            )

        log.info(
            "connection_judged",
            considered=len(found),
            answered=len(judgements),
            kept=len(proposals),
        )
        if not proposals and not judgements:
            # An empty answer is the model failing to answer, not the model saying no --
            # a genuine "none of these" comes back as judgements with `keep: false`. The
            # floor is what decides when nothing decided.
            return self._floor_only(found)
        return proposals[: settings.CONNECTION_MAX_CANDIDATES]

    def _floor_only(self, found: list[Candidate]) -> list[Proposal]:
        """The arithmetic answer: everything above the decision floor, as `related_to`.

        The only claim a cosine distance supports, which is why it is both the fallback
        and honest rather than merely degraded. A tag-only candidate has no score and is
        deliberately *not* proposed here -- with no judge there is nothing to tell a
        shared tag from a shared language, and `jobs` belongs to hundreds of items.
        """
        floor = settings.CONNECTION_MIN_SCORE
        return [
            Proposal(target_id=candidate.item.id, score=candidate.score)
            for candidate in found
            if candidate.score is not None and candidate.score >= floor
        ][: settings.CONNECTION_MAX_CANDIDATES]

    async def _declined_hints(self, user_id: uuid.UUID) -> tuple[str, ...]:
        """Subjects this person has already declined, as one phrase per pair.

        Fail-soft and deliberately so: this is a hint that makes the judge stricter, and a
        capture must not lose its connections because the read that fetches it failed.
        """
        try:
            declines = await self.repo.recent_declines(
                user_id, limit=settings.CONNECTION_DECLINED_HINTS
            )
        except Exception as exc:  # noqa: BLE001 - a prompt hint is never worth a retry
            log.info("connection_declines_unavailable", error=type(exc).__name__)
            return ()
        return tuple(
            f"{left} ↔ {right}" for left, right in declines if left and right
        )

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


def _tags_of(item: VaultItem) -> set[str]:
    """A memory's tags, case-folded, for comparing with another memory's.

    Folded because the enrichment writes what the model produced and "Docker" and
    "docker" are one subject -- an overlap count that misses them under-reports the
    strongest signal recall has. Non-strings are dropped rather than coerced: `ai_tags` is
    a JSONB column, so what comes back is whatever was written, and `str(None)` would
    become a tag called "None" that quietly matches every other broken row.
    """
    return {
        tag.strip().casefold()
        for tag in (item.ai_tags or [])
        if isinstance(tag, str) and tag.strip()
    }


def _same_category(left: VaultItem, right: VaultItem) -> bool:
    """Both carry the same `ai_category`. Two empties are not a match.

    `ai_category` is a closed list checked with `in CATEGORIES`, so this is an equality
    and not a normalisation -- but an item that was never enriched has None, and treating
    two Nones as agreement would mark every unenriched pair as sharing a subject.
    """
    return bool(left.ai_category) and left.ai_category == right.ai_category


def _canonical_url(item: VaultItem) -> str | None:
    """The source URL reduced to what identifies the page, or None.

    The one mechanical duplicate signal this has: same host and path means the same
    video, article or link saved twice. Deliberately narrow.

    * **The query string is dropped**, because that is where tracking parameters live --
      the same reel arrives as `?igsh=...` from one share and `?fbclid=...` from another,
      and keeping them means two saves of one page never match.
    * **The fragment is dropped** for the same reason.
    * **`www.` and a trailing slash are dropped**, which are spelling rather than
      identity.
    * **Nothing else is normalised.** Short links are *not* resolved here: that is a
      network fetch, in a Celery task, against a URL from a scraped page -- which is the
      SSRF surface `assert_safe_url` exists for, and it would be a request per candidate
      per capture to settle a signal the judge is reading as one line among several.
    * A non-http scheme is refused rather than compared. `duplicate_of` is a claim about
      two saved pages, and two rows sharing `about:blank` are not one page.
    """
    raw = (item.source_url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        # A malformed URL is not a duplicate signal. It is also not worth a warning: the
        # column holds whatever a person pasted.
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    host = parts.netloc.lower().removeprefix("www.")
    if not host:
        return None
    path = parts.path.rstrip("/")
    return f"{host}{path}"
