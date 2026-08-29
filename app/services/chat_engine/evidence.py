"""Whether the retrieved memories are enough to answer from -- decided before the model.

Retrieval always returns something. `ORDER BY embedding <=> $1 LIMIT 8` has no notion of
"nothing matched": ask a vault of holiday photos about Redis eviction and it returns the
eight least-unrelated photos, in confident order. Handing those to an answer model is the
single most productive way to manufacture a memory the user never saved -- the model is
told to speak from the blocks, the blocks are about Croatia, and the answer connects
Croatia to Redis because that is the only material it was given.

So the top-k list is filtered on relevance *here*, between the retriever and the prompt,
and the outcome is one of three states rather than a boolean:

* `no_evidence` -- nothing cleared the floor. The caller answers with a fixed sentence
  and makes **no model call at all**. An empty context is the one input the answer
  prompt has no honest reply to, and paying a provider to produce a hallucination is
  worse than paying nothing to say "I couldn't find that".
* `insufficient` -- something is related but nothing is a strong match. The answer is
  still generated, from the memories that survived, with the prompt told to say plainly
  that the match is weak instead of stretching it.
* `supported` -- at least one memory answers the question. The ordinary path.

Two filters, and they do different jobs. The **floor** (`RECALL_MIN_SCORE`) is absolute
and is what an embedding model's own scale decides; it is configuration for exactly that
reason -- Gemini's similarities sit high and bunched, OpenAI's spread low, and a constant
compiled in here would be wrong for one of them with nothing to notice. The **margin**
(`RECALL_SCORE_MARGIN`) is relative and provider-independent: it drops memories far
weaker than the best hit even when they clear the floor, because a strong match diluted
by seven mediocre ones is how unrelated memories end up being connected in an answer.

Deterministic on purpose. A model call to decide whether the model may be called has no
ceiling, and it would put the judgement back inside the thing being judged.

Pure: no database, no provider, no surface. `tests/chat_engine/test_evidence.py` pins it.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.core.config import settings
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id


class EvidenceStatus(StrEnum):
    """How much the retrieved memories support answering, not what to say about it."""

    no_evidence = "no_evidence"
    insufficient = "insufficient"
    supported = "supported"


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    """One memory and how well it matched. The pair, kept together deliberately.

    The score used to be dropped at the retriever, which made every later stage treat
    the eighth result exactly like the first. Nothing downstream can reconstruct it --
    the vector is gone by then -- so it travels with the row.
    """

    item: VaultItem
    score: float


@dataclass(frozen=True, slots=True)
class Evidence:
    """The memories an answer may use, and how far it may go with them."""

    status: EvidenceStatus
    memories: tuple[RetrievedMemory, ...] = ()

    @property
    def items(self) -> list[VaultItem]:
        return [memory.item for memory in self.memories]

    @property
    def ids(self) -> tuple[str, ...]:
        """The short ids the prompt will label these blocks with, in the same order.

        This is what the post-generation validator checks a citation against, so it has
        to be derived from the same function the prompt uses -- two implementations of
        "the id of a memory" is one implementation that drifts and a validator that
        starts approving ids it invented itself.
        """
        return tuple(short_id(memory.item) for memory in self.memories)

    @property
    def best_score(self) -> float:
        return self.memories[0].score if self.memories else 0.0


def score_from_distance(distance: float) -> float:
    """Cosine distance from pgvector as a 0..1 relevance score.

    pgvector's `<=>` is `1 - cosine_similarity`, so this is the similarity itself, with
    the negative half of the range clamped away: a memory that points the *opposite* way
    to the question is not more relevant than one that is merely unrelated, and letting
    it go negative would only make the margin arithmetic below harder to read.
    """
    return max(0.0, min(1.0, 1.0 - distance))


def assess(
    memories: Sequence[RetrievedMemory],
    *,
    min_score: float | None = None,
    strong_score: float | None = None,
    margin: float | None = None,
) -> Evidence:
    """Filter top-k down to what actually supports an answer, and say how strongly.

    The thresholds default to configuration and are injectable so a test can state the
    numbers it is exercising rather than inheriting a deployment's tuning.
    """
    floor = settings.RECALL_MIN_SCORE if min_score is None else min_score
    strong = settings.RECALL_STRONG_SCORE if strong_score is None else strong_score
    spread = settings.RECALL_SCORE_MARGIN if margin is None else margin

    # Relevance order is the retriever's, but it is re-established rather than assumed:
    # everything below reads `[0]` as "the best match", and a caller that reorders for
    # display would otherwise silently change which memory the thresholds are measured
    # against. Sorting a list of at most a dozen items costs nothing.
    ranked = sorted(memories, key=lambda memory: memory.score, reverse=True)
    above_floor = [memory for memory in ranked if memory.score >= floor]
    if not above_floor:
        # Deliberately `no_evidence` and not `insufficient`, even though rows came back.
        # "I found something related but it doesn't say" is a claim about the vault, and
        # rows that all failed the relevance floor are not grounds for making it.
        return Evidence(EvidenceStatus.no_evidence)

    best = above_floor[0].score
    kept = tuple(memory for memory in above_floor if memory.score >= best - spread)
    status = (
        EvidenceStatus.supported if best >= strong else EvidenceStatus.insufficient
    )
    return Evidence(status, kept)


def from_rows(rows: Sequence[tuple[VaultItem, float]]) -> list[RetrievedMemory]:
    """Repository `(item, distance)` pairs as scored memories."""
    return [RetrievedMemory(item, score_from_distance(distance)) for item, distance in rows]
