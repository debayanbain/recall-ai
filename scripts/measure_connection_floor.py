"""Measure `CONNECTION_MIN_SCORE` instead of guessing it.

    uv run python scripts/measure_connection_floor.py

`CONNECTION_MIN_SCORE` decides which pairs of memories get proposed as connected. It is
the same *kind* of number as `RECALL_MIN_SCORE` and it has the same failure mode, which is
worth stating plainly because it has already happened once in this codebase: the first
draft of `RECALL_MIN_SCORE` carried Gemini's 0.55 onto an OpenAI vault where a true match
scores 0.373, and it would have reported memories the user really had saved as missing.
A threshold inherited from another provider is wrong in a way nothing reports.

It is **not** the same number, and must not be copied from it. `RECALL_MIN_SCORE` answers
"is this memory about the question" -- a short query against a document. This one compares
two documents, and document-to-document similarities sit systematically higher, so a floor
borrowed from the query side draws an edge between every pair and the graph says nothing.

What this prints is the only thing that settles it: the distribution of scores for pairs
you have decided are genuinely related, the distribution for pairs you have decided are
not, and the gap between them. Put the floor in the gap. If there is no gap, the embedding
model cannot tell these apart and no threshold will fix that -- which is also a finding.

**It spends real money** -- one embedding call per text -- and reaches the configured
provider on purpose. That is why it lives in `scripts/` rather than `tests/`: the autouse
`_no_provider_calls` fixture would refuse it, and rightly.

Re-run it whenever the embedding provider or model changes, alongside the re-embed that
change already requires. Edit `RELATED` and `UNRELATED` to look like the vault you
actually have -- the defaults are a starting point, not a benchmark.
"""
from __future__ import annotations

import asyncio
import statistics
from typing import NamedTuple

from app.ai import get_ai_provider
from app.core.config import settings


class Pair(NamedTuple):
    label: str
    a: str
    b: str


#: Pairs a person would call connected. Write these as the *enriched* text the pipeline
#: embeds -- `f"{title}\n{summary}\n{content}"` -- not as bare titles: a title-only pair
#: scores differently from what the vault actually stores, and the number this prints has
#: to be about the vault.
RELATED: list[Pair] = [
    Pair(
        "same topic, different angle",
        "Building a Second Brain\nA method for organising notes so past reading is "
        "findable later.\nThe PARA structure sorts everything into projects, areas, "
        "resources and archives.",
        "How to Take Smart Notes\nZettelkasten for writers.\nEvery note is atomic and "
        "linked to the notes it grew from, so structure emerges from the links.",
    ),
    Pair(
        "one expands the other",
        "FastAPI dependency injection\nHow Depends() resolves a dependency graph per "
        "request.\nSub-dependencies are cached within a request by default.",
        "Async SQLAlchemy sessions in FastAPI\nGetting a session per request without "
        "leaking it.\nThe session is yielded from a dependency and closed at the "
        "boundary.",
    ),
    Pair(
        "they disagree, which is still a connection",
        "Validate your SaaS idea in 7 days\nTalk to ten customers before writing code.",
        "Just build it\nMarket research is procrastination; ship and watch what happens.",
    ),
]

#: Pairs a person would *not* call connected. Include at least one that shares vocabulary
#: without sharing a subject -- that is the pair a too-low floor connects, and the reason
#: the check exists.
UNRELATED: list[Pair] = [
    Pair(
        "nothing in common",
        "Building a Second Brain\nA method for organising notes.",
        "Sourdough hydration\nWhy an 80% hydration dough spreads.",
    ),
    Pair(
        "shared words, different subject",
        "Redis persistence\nAOF rewrites fork the process.",
        "Forking a repository on GitHub\nA fork is a server-side copy you can push to.",
    ),
    Pair(
        "same field, unrelated question",
        "Postgres HNSW indexes\nApproximate nearest neighbour over pgvector.",
        "Postgres connection pooling\nPgBouncer in transaction mode and prepared "
        "statements.",
    ),
]


def cosine(a: list[float], b: list[float]) -> float:
    """The similarity the derivation compares against its floor.

    Deliberately computed the same way the database does -- `1 - cosine_distance`, clamped
    -- rather than any other similarity, because the number that matters is the one
    `evidence.score_from_distance` will produce at runtime, not one that merely correlates
    with it.
    """
    dot: float = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a: float = sum(x * x for x in a) ** 0.5
    norm_b: float = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))


async def score(pairs: list[Pair]) -> list[tuple[Pair, float]]:
    provider = get_ai_provider()
    out = []
    for pair in pairs:
        left = await provider.generate_embedding(pair.a)
        right = await provider.generate_embedding(pair.b)
        out.append((pair, cosine(left, right)))
    return out


def report(title: str, scored: list[tuple[Pair, float]]) -> list[float]:
    print(f"\n{title}")
    for pair, value in sorted(scored, key=lambda row: row[1], reverse=True):
        print(f"  {value:.3f}  {pair.label}")
    return [value for _pair, value in scored]


async def main() -> None:
    print(f"provider: {settings.AI_PROVIDER}")
    print(f"configured CONNECTION_MIN_SCORE: {settings.CONNECTION_MIN_SCORE}")

    related = report("related pairs (these should be ABOVE the floor)", await score(RELATED))
    unrelated = report(
        "unrelated pairs (these should be BELOW it)", await score(UNRELATED)
    )

    weakest_true = min(related)
    strongest_false = max(unrelated)
    print(
        f"\nweakest related:   {weakest_true:.3f}"
        f"\nstrongest unrelated: {strongest_false:.3f}"
    )

    if weakest_true <= strongest_false:
        # Not a failure of the threshold -- a failure of the embedding to separate these
        # at all. No number placed anywhere gets both sets right, and picking one anyway
        # is how a setting ends up looking measured when it is not.
        print(
            "\nNO GAP. These sets overlap, so no floor separates them. Either the pairs "
            "need to be more representative, or this embedding model cannot tell them "
            "apart -- which is a finding about the model, not about the setting."
        )
        return

    suggested = round(statistics.fmean([weakest_true, strongest_false]), 2)
    print(
        f"\ngap: {strongest_false:.3f} .. {weakest_true:.3f}"
        f"\nsuggested CONNECTION_MIN_SCORE: {suggested}"
    )
    if not (strongest_false < settings.CONNECTION_MIN_SCORE < weakest_true):
        print(
            f"\nThe configured value ({settings.CONNECTION_MIN_SCORE}) is OUTSIDE that "
            "gap. Too low connects everything; too high reports memories the person "
            "really did save as unrelated."
        )


if __name__ == "__main__":
    asyncio.run(main())
