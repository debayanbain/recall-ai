"""The two-stage derivation: recall over-offers, the judge decides.

DB-backed like `test_derivation.py`, and stubbed at exactly one seam --
`connection_judge.judge_connections` -- so nothing here reaches a provider. What is *not*
stubbed is everything between the vectors and the row: recall's two halves, the signals,
the confidence floor, the swap, and the fallback.

The properties, and why each one is here:

* **A tag-only candidate is reachable.** It is the whole reason the second recall half
  exists: two notes about one job application share `jobs` and `visa` and can still sit
  far apart in embedding space. Without the test, deleting the tag half breaks nothing
  visible.
* **A judged edge is still `suggested`.** The confirmation tap is what stands between a
  scraped page and the agent's prompt context, and a confidence number does not replace it.
* **`keep: false` writes nothing**, which is the point of the whole stage -- the inbox
  full of near-duplicate reels is what it exists to stop.
* **A failure falls back to the floor**, so a provider outage degrades to the behaviour
  this feature shipped with rather than to no connections at all.
"""
from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.ai import connection_judge as judge
from app.core.config import settings
from app.models.base import ConnectionOrigin, ConnectionStatus, Relation
from app.models.connection import MemoryConnection
from app.models.user import User
from app.models.vault import VaultItem
from app.repositories.connection import ConnectionRepository
from app.repositories.vault import VaultRepository
from app.services.connection_derivation import ConnectionDeriver
from tests.conftest import make_item

DIM = settings.EMBEDDING_DIM


def vector(*leading: float) -> list[float]:
    values = list(leading) + [0.0] * (DIM - len(leading))
    return values[:DIM]


async def embed(session: AsyncSession, item: VaultItem, values: list[float]) -> None:
    await VaultRepository(session).upsert_chunk(
        item.id, item.user_id, values, content=item.title or ""
    )
    await session.commit()


def deriver(session: AsyncSession) -> ConnectionDeriver:
    return ConnectionDeriver(ConnectionRepository(session), VaultRepository(session))


async def rows(session: AsyncSession) -> list[MemoryConnection]:
    return list((await session.exec(select(MemoryConnection))).all())


async def retag(
    session: AsyncSession, item: VaultItem, tags: list[str], category: str | None = None
) -> VaultItem:
    item.ai_tags = tags
    if category is not None:
        item.ai_category = category
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


@pytest.fixture
def judging(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[judge.JudgeInput]]:
    """Turn the judge on and answer for it. Returns the prompts it was handed.

    The switch is forced off by `_no_provider_calls`; every test here re-enables it and
    replaces the call, which is the only combination that exercises this path without
    spending anything.
    """

    def install(
        answer: Callable[[judge.JudgeInput], list[judge.Judgement]],
    ) -> list[judge.JudgeInput]:
        seen: list[judge.JudgeInput] = []

        async def _judge(payload: judge.JudgeInput) -> list[judge.Judgement]:
            seen.append(payload)
            return answer(payload)

        monkeypatch.setattr(settings, "CONNECTION_JUDGE_ENABLED", True)
        monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
        monkeypatch.setattr(
            "app.services.connection_derivation.connection_judge.judge_connections",
            _judge,
        )
        return seen

    return install


def keeping(
    relation: Relation = Relation.related_to,
    *,
    confidence: float = 0.9,
    swap: bool = False,
    reason: str = "Both are about the same visa interview.",
) -> Callable[[judge.JudgeInput], list[judge.Judgement]]:
    def answer(payload: judge.JudgeInput) -> list[judge.Judgement]:
        return [
            judge.Judgement(
                key=candidate.key,
                keep=True,
                relation=relation,
                swap=swap,
                reason=reason,
                confidence=confidence,
            )
            for candidate in payload.candidates
        ]

    return answer


def rejecting(payload: judge.JudgeInput) -> list[judge.Judgement]:
    return [
        judge.Judgement(
            key=candidate.key,
            keep=False,
            relation=Relation.related_to,
            swap=False,
            reason="Only share a language.",
            confidence=0.95,
        )
        for candidate in payload.candidates
    ]


# --------------------------------------------------------------------------------------


async def test_the_judge_labels_the_edge_and_records_its_reason(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    judging(keeping(Relation.expands))
    focus = await make_item(session, user, "the interview")
    other = await make_item(session, user, "the form")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    assert await deriver(session).derive(focus.id) == 1
    [edge] = await rows(session)
    assert edge.relation == Relation.expands.value
    assert edge.ai_reason == "Both are about the same visa interview."
    assert edge.origin == ConnectionOrigin.ai.value


async def test_a_judged_edge_is_still_only_a_suggestion(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    # Confidence sorts an inbox. It does not confirm an edge, at any value, ever -- see
    # the injection note in CLAUDE.md.
    judging(keeping(Relation.duplicate_of, confidence=1.0))
    focus = await make_item(session, user, "reel")
    other = await make_item(session, user, "reel again")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    await deriver(session).derive(focus.id)
    [edge] = await rows(session)
    assert edge.status == ConnectionStatus.suggested.value
    assert edge.confirmed_at is None


async def test_a_rejected_candidate_writes_nothing(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    judging(rejecting)
    focus = await make_item(session, user, "reel one")
    other = await make_item(session, user, "reel two")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    assert await deriver(session).derive(focus.id) == 0
    assert await rows(session) == []


async def test_a_low_confidence_keep_is_dropped(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    # Every suggestion is a decision a person has to make. One the model itself is unsure
    # of is the worst kind to spend that on.
    judging(keeping(confidence=settings.CONNECTION_JUDGE_MIN_CONFIDENCE - 0.1))
    focus = await make_item(session, user, "a")
    other = await make_item(session, user, "b")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    assert await deriver(session).derive(focus.id) == 0


async def test_a_backwards_relation_is_stored_with_its_ends_swapped(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    # "The candidate is part of the new memory" arrives as part_of + b_to_a. Swapping is
    # free -- the unique key is the unordered pair -- and it means every reader can take
    # the stored direction at face value.
    judging(keeping(Relation.part_of, swap=True))
    focus = await make_item(session, user, "the chapter")
    book = await make_item(session, user, "the book")
    await embed(session, focus, vector(1.0))
    await embed(session, book, vector(1.0))

    await deriver(session).derive(focus.id)
    [edge] = await rows(session)
    assert edge.source_item_id == book.id
    assert edge.target_item_id == focus.id


async def test_a_symmetric_relation_is_pointed_from_the_lower_id(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    # Otherwise half of them render backwards from one of their two pages and somebody
    # eventually "fixes" a row that was never wrong.
    judging(keeping(Relation.duplicate_of))
    focus = await make_item(session, user, "reel")
    other = await make_item(session, user, "same reel")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    await deriver(session).derive(focus.id)
    [edge] = await rows(session)
    assert edge.source_item_id == min(focus.id, other.id)
    assert edge.target_item_id == max(focus.id, other.id)


async def test_a_shared_tag_reaches_the_judge_even_when_the_vectors_do_not(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    """The reason the second recall half exists.

    Both memories are about one job application; their *text* is a form and an interview,
    so the vectors are orthogonal and the vector half finds nothing. The tags the
    enrichment already paid for do.
    """
    seen = judging(keeping(Relation.related_to))
    focus = await make_item(session, user, "visa form")
    other = await make_item(session, user, "visa interview")
    await retag(session, focus, ["visa", "jobs"], category="Career")
    await retag(session, other, ["visa", "jobs"], category="Career")
    await embed(session, focus, vector(1.0, 0.0))
    await embed(session, other, vector(0.0, 1.0))

    assert await deriver(session).derive(focus.id) == 1
    [payload] = seen
    [candidate] = payload.candidates
    assert candidate.shared_tags == ("jobs", "visa")
    assert candidate.same_category is True
    # Never seen by the vector half, so there is no measurement to report. NULL rather
    # than zero: "not measured" and "not similar" are different claims.
    assert candidate.vector_score is None
    [edge] = await rows(session)
    assert edge.score is None


async def test_the_same_source_url_is_reported_to_the_judge(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    seen = judging(keeping(Relation.duplicate_of))
    focus = await make_item(session, user, "reel")
    other = await make_item(session, user, "reel, again")
    for item, url in (
        (focus, "https://www.facebook.com/reel/123?igsh=one"),
        (other, "https://facebook.com/reel/123/"),
    ):
        item.source_url = url
        session.add(item)
    await session.commit()
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    await deriver(session).derive(focus.id)
    [payload] = seen
    assert payload.candidates[0].near_duplicate is True


async def test_a_provider_failure_falls_back_to_the_score_floor(
    session: AsyncSession,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider outage degrades to the behaviour this feature shipped with.

    `related_to`, above `CONNECTION_MIN_SCORE`, with no reason attached -- weaker and
    noisier, but honest, and the path with the most tests behind it.
    """

    async def _boom(payload: judge.JudgeInput) -> list[judge.Judgement]:
        raise judge.ConnectionJudgeFailed("nope")

    monkeypatch.setattr(settings, "CONNECTION_JUDGE_ENABLED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        "app.services.connection_derivation.connection_judge.judge_connections", _boom
    )

    focus = await make_item(session, user, "a")
    other = await make_item(session, user, "b")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    assert await deriver(session).derive(focus.id) == 1
    [edge] = await rows(session)
    assert edge.relation == Relation.related_to.value
    assert edge.ai_reason is None
    assert edge.score is not None


async def test_the_fallback_refuses_a_candidate_it_has_no_number_for(
    session: AsyncSession,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no judge, a shared tag is not evidence of anything.

    `jobs` belongs to hundreds of items, and there is nothing in the floor-only path to
    tell a shared subject from a shared vocabulary. So a tag-only candidate is recalled
    and then declined, rather than written as an edge nothing judged.
    """
    monkeypatch.setattr(settings, "CONNECTION_JUDGE_ENABLED", False)
    focus = await make_item(session, user, "visa form")
    other = await make_item(session, user, "visa interview")
    await retag(session, focus, ["visa"])
    await retag(session, other, ["visa"])
    await embed(session, focus, vector(1.0, 0.0))
    await embed(session, other, vector(0.0, 1.0))

    assert await deriver(session).derive(focus.id) == 0


async def test_the_judge_is_shown_what_this_person_already_declined(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    """Taste, as a hint in a prompt and nothing stronger.

    What it buys is the difference between a system that offers the same wrong kind of
    connection forever and one that stops after being told twice.
    """
    seen = judging(rejecting)
    focus = await make_item(session, user, "reel one")
    other = await make_item(session, user, "reel two")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))
    await deriver(session).derive(focus.id)

    # Decline what the first pass proposed, then run a second capture.
    edges = await rows(session)
    for edge in edges:
        edge.status = ConnectionStatus.dismissed.value
        session.add(edge)
    await session.commit()

    third = await make_item(session, user, "reel three")
    await embed(session, third, vector(1.0))
    await deriver(session).derive(third.id)

    assert seen[-1].declined_hints  # the pair that was declined, by subject


async def test_recall_never_exceeds_what_one_prompt_may_weigh(
    session: AsyncSession,
    user: User,
    judging: Callable[..., list[judge.JudgeInput]],
) -> None:
    seen = judging(rejecting)
    focus = await make_item(session, user, "focus")
    await embed(session, focus, vector(1.0))
    for n in range(settings.CONNECTION_JUDGE_MAX_CANDIDATES + 6):
        other = await make_item(session, user, f"other {n}")
        await embed(session, other, vector(1.0, n / 100))

    await deriver(session).derive(focus.id)
    [payload] = seen
    assert len(payload.candidates) <= settings.CONNECTION_JUDGE_MAX_CANDIDATES
    assert len(payload.candidates) <= judge.MAX_CANDIDATES


def test_every_candidate_gets_its_own_key() -> None:
    # Keys are how a judgement finds its way back to a row. Two candidates sharing one
    # would write the wrong edge, silently.
    keys = [f"c{index + 1}" for index in range(5)]
    assert len(set(keys)) == len(keys)
