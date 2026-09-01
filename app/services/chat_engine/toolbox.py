"""What the memory tools actually do. One user, one turn, every bound in one place.

`app/ai/chat/tools.py` declares what the model may ask for; this is what happens when it
asks. The split is the same one the rest of this codebase makes between a prompt and the
thing it is a prompt for -- and here it carries the security boundary, because a tool is
the one place a model's output becomes an action.

Four properties, and none of them is expressible as a tool argument, which is the point:

* **The user is fixed at construction.** `user_id` comes from the caller that resolved
  the account -- never from the model, never from the message, never from a memory. A
  prompt injection cannot ask for another tenant's rows because there is no argument to
  ask with, and the repository re-applies the predicate underneath anyway.
* **Ids are capabilities, and only surfaced ones exist.** `get_memory` reads `_seen`,
  which holds exactly the memories a search or a list has already returned *in this
  turn*. A short id is a prefix of a UUID the owner already has and reaches nothing on
  its own; restricting it further is what stops the model spending calls on ids a memory
  told it to open.
* **The relevance gate is not the model's to skip.** Every search goes through
  `evidence.assess` before its results are rendered, exactly as the single-shot path
  does. The model chose the query; it does not get to choose what counts as a match. A
  weak set is handed back labelled weak rather than silently upgraded.
* **Arguments are validated here, next to the query.** `content_types` is checked against
  the real enum and `days` is bounded, because these values reach a SQL filter and a
  hallucinated one would otherwise arrive there intact. Validating in the schema instead
  would put the check somewhere a second caller can skip.

Everything returned is *text the model will read*, fenced with `chain.fence_block` -- the
same fence the single-shot path uses. A tool result is quoted material for the same
reason a retrieved memory is: it came from the same scraped pages.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from app.ai.chat import chain
from app.core.logging import get_logger
from app.models.base import ContentType
from app.models.vault import VaultItem
from app.repositories.vault import VaultRepository
from app.services.chat_engine.cards import (
    DETAIL_MAX_ITEMS,
    build_card,
    build_detail_card,
    short_id,
)
from app.services.chat_engine.evidence import EvidenceStatus, assess
from app.services.chat_engine.retrieval import (
    DEFAULT_LIMIT,
    MemoryFilters,
    MemoryRetriever,
)

log = get_logger("recall.chat")

#: The same ceiling the planner applies. A model that answers "how many days is 'a while
#: ago'" with 40000 is not describing a period anyone has been saving things for.
_MAX_DAYS = 3650

#: Query text is echoed into a log line and into a fixed reply. Bounded for the same
#: reason every other model output here is.
_MAX_QUERY = 500

#: How many memories a listing returns. Smaller than a search's top-k on purpose: a
#: listing has no relevance ordering to trust, so a long one is a long way to say
#: "here is everything".
_LIST_LIMIT = 10

#: What a tool says when it found nothing. Plain English, addressed to the model rather
#: than to the user -- the model translates its own reply, and the fixed sentence the
#: *user* sees when nothing at all was found is written in `recall_chat`.
_NO_MATCH = (
    "No memories matched. If you have not already tried different words, search once "
    "more; otherwise tell them you could not find it."
)


class MemoryToolbox:
    """The tools for one user, for one question. Not reusable across turns.

    Deliberately short-lived: `_seen` is the set of memories this answer is allowed to
    cite, and carrying it into the next question would let an answer cite evidence that
    was retrieved for a different one.
    """

    def __init__(
        self,
        user_id: uuid.UUID,
        repo: VaultRepository,
        *,
        top_k: int | None = None,
    ) -> None:
        self.user_id = user_id
        self.repo = repo
        self.memories = MemoryRetriever(repo)
        self.top_k = top_k or DEFAULT_LIMIT
        #: short id -> the row it names. Insertion-ordered, so `allowed_ids` follows the
        #: order the model was shown them in.
        self._seen: dict[str, VaultItem] = {}
        #: What the model actually searched for, in order. The first entry is the best
        #: available description of the subject when nothing was found at all -- it is
        #: the model's own extraction of it, which is what the planner used to produce.
        self.queries: list[str] = []

    # --- what the answer is allowed to have said --------------------------------------

    @property
    def allowed_ids(self) -> tuple[str, ...]:
        return tuple(self._seen)

    @property
    def allowed_urls(self) -> tuple[str | None, ...]:
        return tuple(item.source_url for item in self._seen.values())

    @property
    def items(self) -> list[VaultItem]:
        return list(self._seen.values())

    @property
    def found_nothing(self) -> bool:
        """True when no tool ever surfaced a memory, so there is nothing to answer from."""
        return not self._seen

    # --- the tools ---------------------------------------------------------------------

    async def search_memories(
        self,
        query: str,
        days: int | None = None,
        content_types: Sequence[str] = (),
        category: str | None = None,
    ) -> str:
        text = (query or "").strip()[:_MAX_QUERY]
        if not text:
            # An empty search is the model reaching for a listing. Answered as one
            # rather than refused: a refusal costs a round to say what a redirect says.
            return await self.list_memories(days, content_types, category)

        self.queries.append(text)
        memories = await self.memories.recall(
            self.user_id,
            text,
            MemoryFilters(
                created_after=_created_after(days),
                content_types=_content_types(content_types),
                category=_category(category),
            ),
            limit=self.top_k,
        )
        evidence = assess(memories)
        log.info(
            "recall_tool_search",
            retrieved=len(memories),
            kept=len(evidence.memories),
            best=round(evidence.best_score, 3),
            status=evidence.status.value,
        )
        if evidence.status is EvidenceStatus.no_evidence:
            return _NO_MATCH

        blocks = self._render(evidence.items)
        if evidence.status is EvidenceStatus.insufficient:
            # The same distinction `GUIDANCE_WEAK` draws on the single-shot path, said
            # per result rather than once for the turn: with several searches in a turn,
            # one system-level caveat cannot say which of them it is about.
            return (
                "These are only a WEAK match. Do not stretch them to fit -- say you "
                "found related memories but nothing that answers it precisely.\n\n"
                f"{blocks}"
            )
        return blocks

    async def list_memories(
        self,
        days: int | None = None,
        content_types: Sequence[str] = (),
        category: str | None = None,
    ) -> str:
        """Newest-first, no embedding and no vector scan.

        The cheap half of retrieval, and the right answer to "what did I save this
        week?": there is no subject to rank against, so paying for an embedding buys an
        ordering that means nothing.
        """
        items, total = await self.repo.list_filtered(
            self.user_id,
            limit=_LIST_LIMIT,
            created_after=_created_after(days),
            content_types=_content_types(content_types),
            category=_category(category),
        )
        log.info("recall_tool_list", returned=len(items), total=total)
        if not items:
            return _NO_MATCH
        header = f"{total} saved in total; the {len(items)} newest are below.\n\n"
        return header + self._render(items)

    async def get_memory(self, memory_id: str) -> str:
        """One memory's own text -- only for an id already surfaced in this turn.

        The restriction is not about secrecy: a short id is a prefix of a UUID whose row
        this user owns, and the repository would scope it regardless. It is about a model
        that has been told by a *memory* to open something. An id it has not been handed
        is one it did not get from the vault.
        """
        wanted = (memory_id or "").strip().lower()
        item = self._seen.get(wanted)
        if item is None:
            log.info("recall_tool_unknown_id", requested=wanted[:16])
            return (
                "No memory with that id has been returned to you. Use only ids from "
                "the blocks above, or search first."
            )
        return chain.fence_block(
            short_id(item),
            build_detail_card(item),
            title=item.title or item.source_url or "Untitled",
            category=item.ai_category,
            saved=item.created_at.date().isoformat() if item.created_at else None,
            url=item.source_url,
        )

    # --- rendering ----------------------------------------------------------------------

    def _render(self, items: Sequence[VaultItem]) -> str:
        """Cards as fenced blocks, registering each one as citable evidence.

        Registration happens here rather than at the call sites so a tool that returns
        blocks without recording them is not a thing anyone can write: the validator
        checks the answer's citations against `allowed_ids`, and a block shown to the
        model but missing from that set would have its own citation stripped as a
        fabrication.
        """
        blocks = []
        for item in items[:DETAIL_MAX_ITEMS * 4]:
            identifier = short_id(item)
            self._seen.setdefault(identifier, item)
            blocks.append(
                chain.fence_block(
                    identifier,
                    build_card(item),
                    title=item.title or item.source_url or "Untitled",
                    category=item.ai_category,
                    saved=item.created_at.date().isoformat() if item.created_at else None,
                    url=item.source_url,
                )
            )
        return "\n\n".join(blocks)


# --- argument validation ------------------------------------------------------------------
#
# Model output on its way to a SQL filter. Every one of these silently drops what it does
# not recognise rather than raising: a hallucinated content type should cost the model a
# narrower search, not the user their answer.


def _created_after(days: int | None) -> datetime | None:
    if days is None or not (1 <= days <= _MAX_DAYS):
        return None
    return datetime.now(UTC) - timedelta(days=days)


def _content_types(values: Sequence[str]) -> list[ContentType] | None:
    valid = {t.value for t in ContentType}
    resolved = [ContentType(v) for v in values if isinstance(v, str) and v in valid]
    return resolved or None


def _category(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned[:64] or None
