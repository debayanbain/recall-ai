"""The read side of RAG: a question in, the user's own memories out.

The write side -- extract, enrich, embed -- already exists in the processing pipeline and
stays there. This is only the lookup, and it deliberately stops one step short of an
answer: it returns rows, never prose, so the thing that decides *how much* of a memory to
show (`cards`) and the thing that decides *what to say* (the answer chain) stay separate
from the thing that decides *which memories*.

**The query embedding comes from `AIProvider`, the same code that wrote the stored
vectors.** Using a different embedding stack here would compare a fresh vector against a
space it was not drawn from: under Gemini the stored ones are 768 dims zero-padded to
1536, so the result would be plausible-looking ordering over noise -- wrong in a way
nothing would flag.

**Tenant scoping is not optional and not implemented here.** `user_id` is a required
positional argument and goes straight to `VaultRepository.search_semantic`, which does
the filtering. `get_unscoped` exists for the worker, which has no request user, and must
never be reachable from a conversation -- `tests/chat_engine/test_retrieval.py` pins that.

**Distances travel with the rows.** They used to be dropped here, which made every later
stage treat the eighth result exactly like the first -- and a vector search always
returns its k nearest rows however far away they are, so "nothing matched" and "eight
weak matches" arrived downstream looking identical. Nothing after this point can
reconstruct the number: the query vector is gone. Judging what the score *means* is
`evidence.py`'s job, not this module's; this only refuses to throw it away.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.ai import get_ai_provider
from app.models.base import ContentType
from app.repositories.vault import VaultRepository
from app.services.chat_engine.evidence import RetrievedMemory, from_rows

#: Enough for the answer to have something to choose between, few enough that their cards
#: fit a prompt budget. The caller may lower it -- a detail question wants one or two.
DEFAULT_LIMIT = 8


@dataclass(frozen=True, slots=True)
class MemoryFilters:
    """Narrowing a search already scoped to one user. Never widening it."""

    created_after: datetime | None = None
    content_types: Sequence[ContentType] | None = None
    category: str | None = None


class MemoryRetriever:
    """Nearest-neighbour lookup over one user's memories."""

    def __init__(self, repo: VaultRepository) -> None:
        self.repo = repo

    async def recall(
        self,
        user_id: uuid.UUID,
        question: str,
        filters: MemoryFilters | None = None,
        *,
        limit: int = DEFAULT_LIMIT,
    ) -> list[RetrievedMemory]:
        """The memories most like `question`, best first, each with its score.

        No answer model, and no judgement about whether the scores are good enough --
        that is `evidence.assess`, deliberately a separate step so the threshold is not
        buried in the query.
        """
        narrowing = filters or MemoryFilters()
        vector = await get_ai_provider().generate_embedding(question)
        rows = await self.repo.search_semantic(
            user_id,
            vector,
            limit=limit,
            created_after=narrowing.created_after,
            content_types=narrowing.content_types,
            category=narrowing.category,
        )
        return from_rows(rows)
