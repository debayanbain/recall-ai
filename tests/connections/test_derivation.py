"""What the worker proposes, and what it refuses to.

DB-backed and completely offline: the vectors are fixtures, so nothing here reaches an
embedding provider. That matters more than usual for this file -- the derivation's whole
job is to read a vector the pipeline already wrote, and a test that recomputed one would
be testing the provider instead.

The rule these exist to hold is that **nothing derived is ever `confirmed`**. A cosine
distance says two memories are close and says nothing more, and the tap that turns a
suggestion into a real edge is what stands between a scraped page and the agent's context.
"""
from __future__ import annotations

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.base import ConnectionOrigin, ConnectionStatus, Relation
from app.models.connection import MemoryConnection
from app.models.user import User
from app.models.vault import VaultItem
from app.repositories.connection import ConnectionRepository
from app.repositories.vault import VaultRepository
from app.services.connection_derivation import ConnectionDeriver
from tests.conftest import make_connection, make_item

DIM = settings.EMBEDDING_DIM


def vector(*leading: float) -> list[float]:
    """A unit-ish vector whose cosine similarity to another is easy to reason about."""
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


# --------------------------------------------------------------------------------------


async def test_only_candidates_above_the_floor_are_proposed(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.6)
    focus = await make_item(session, alice, "focus")
    near = await make_item(session, alice, "near")
    far = await make_item(session, alice, "far")
    await embed(session, focus, vector(1.0, 0.0))
    # cosine ~0.89 -> score ~0.89, clears 0.6
    await embed(session, near, vector(1.0, 0.5))
    # orthogonal -> cosine 0, score 0, nowhere near
    await embed(session, far, vector(0.0, 1.0))

    written = await deriver(session).derive(focus.id)
    await session.commit()

    assert written == 1
    edges = await rows(session)
    assert len(edges) == 1
    assert edges[0].target_item_id == near.id


async def test_a_derived_edge_is_only_ever_a_weak_suggestion(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single most important assertion in this file.

    Distance says "close". It cannot tell `expands` from `contradicts` -- two documents
    that flatly disagree are maximally close -- and it is not a person's decision to file
    something. Both halves are checked here because both are one `.value` away from being
    quietly wrong.
    """
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.5)
    focus = await make_item(session, alice, "focus")
    other = await make_item(session, alice, "other")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    await deriver(session).derive(focus.id)
    await session.commit()

    edge = (await rows(session))[0]
    assert edge.status == ConnectionStatus.suggested.value
    assert edge.relation == Relation.related_to.value
    assert edge.origin == ConnectionOrigin.ai.value
    assert edge.confirmed_at is None
    assert edge.score is not None and edge.score > 0.9


async def test_an_item_is_never_its_own_neighbour(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `exclude_item_id` the strongest candidate every single time is the item
    itself, at distance zero -- it would take a slot on every capture and the CHECK
    constraint would then refuse the insert."""
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.0)
    focus = await make_item(session, alice, "focus")
    await embed(session, focus, vector(1.0))

    written = await deriver(session).derive(focus.id)
    await session.commit()

    assert written == 0
    assert await rows(session) == []


async def test_candidates_are_capped(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.0)
    monkeypatch.setattr(settings, "CONNECTION_MAX_CANDIDATES", 2)
    focus = await make_item(session, alice, "focus")
    await embed(session, focus, vector(1.0))
    for n in range(5):
        other = await make_item(session, alice, f"other {n}")
        await embed(session, other, vector(1.0, n / 10))

    written = await deriver(session).derive(focus.id)
    await session.commit()

    assert written == 2


async def test_running_twice_proposes_nothing_new(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`reprocess` re-runs the pipeline, which re-runs this. `ON CONFLICT DO NOTHING`
    makes the second pass a no-op rather than a `UniqueViolation` -- which for this caller
    is the normal outcome, not the edge one."""
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.0)
    focus = await make_item(session, alice, "focus")
    other = await make_item(session, alice, "other")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))

    first = await deriver(session).derive(focus.id)
    await session.commit()
    second = await deriver(session).derive(focus.id)
    await session.commit()

    assert (first, second) == (1, 0)
    assert len(await rows(session)) == 1


async def test_a_dismissed_pair_is_never_proposed_again(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason a dismissal is a status on this table rather than a second table:
    the declined row *is* the conflicting row, so "never suggest this" needs no extra
    lookup and cannot be forgotten by a future caller."""
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.0)
    focus = await make_item(session, alice, "focus")
    other = await make_item(session, alice, "other")
    await embed(session, focus, vector(1.0))
    await embed(session, other, vector(1.0))
    edge = await make_connection(
        session, alice, focus, other, status=ConnectionStatus.suggested
    )
    await ConnectionRepository(session).dismiss(edge.id, alice.id)
    await session.commit()

    written = await deriver(session).derive(focus.id)
    await session.commit()

    assert written == 0
    edges = await rows(session)
    assert len(edges) == 1
    assert edges[0].status == ConnectionStatus.dismissed.value


async def test_a_stranger_memory_is_never_a_candidate(
    session: AsyncSession, alice: User, bob: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identical vectors, different owners. The scan is scoped by the item's own user."""
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.0)
    focus = await make_item(session, alice, "alice focus")
    theirs = await make_item(session, bob, "bob memory")
    await embed(session, focus, vector(1.0))
    await embed(session, theirs, vector(1.0))

    written = await deriver(session).derive(focus.id)
    await session.commit()

    assert written == 0


async def test_a_deleted_memory_is_never_a_candidate(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.0)
    focus = await make_item(session, alice, "focus")
    doomed = await make_item(session, alice, "doomed")
    await embed(session, focus, vector(1.0))
    await embed(session, doomed, vector(1.0))
    await VaultRepository(session).delete(doomed)
    await session.commit()

    written = await deriver(session).derive(focus.id)
    await session.commit()

    assert written == 0


async def test_an_item_with_no_embedding_is_answered_not_raised(
    session: AsyncSession, alice: User
) -> None:
    """A `skipped` item -- an image with no vision key, a .docx with nothing readable --
    has no vector to compare. That is an answer, and it must not look like a failure: the
    capture is already committed and nothing here is worth marking it broken."""
    focus = await make_item(session, alice, "no embedding")

    assert await deriver(session).derive(focus.id) == 0


async def test_a_missing_item_is_answered_not_raised(session: AsyncSession) -> None:
    import uuid

    assert await deriver(session).derive(uuid.uuid4()) == 0


async def test_the_per_item_ceiling_stops_the_scan(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounds the page, the layout, and how much of one memory's page a single scraped
    page can occupy. Counts every state, because a wall of pending suggestions is exactly
    as much of a wall as a wall of confirmed ones."""
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.0)
    monkeypatch.setattr(settings, "CONNECTION_MAX_PER_ITEM", 1)
    focus = await make_item(session, alice, "focus")
    held = await make_item(session, alice, "already connected")
    fresh = await make_item(session, alice, "would be new")
    await embed(session, focus, vector(1.0))
    await embed(session, fresh, vector(1.0))
    await make_connection(session, alice, focus, held)

    written = await deriver(session).derive(focus.id)
    await session.commit()

    assert written == 0
    assert len(await rows(session)) == 1


# --------------------------------------------------------------------------------------
# candidates(): the unfiltered scan the backfill's dry run reads
# --------------------------------------------------------------------------------------


async def test_candidates_applies_no_floor(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor is `derive`'s judgement, not the scan's.

    Keeping them apart is what lets `scripts/backfill_connections.py` show what a
    *different* floor would have done, over a real vault, for free -- every vector it
    compares was already written by the pipeline. That is the one honest way to check
    `CONNECTION_MIN_SCORE` without paying for a fresh set of embeddings.
    """
    monkeypatch.setattr(settings, "CONNECTION_MIN_SCORE", 0.99)
    focus = await make_item(session, alice, "focus")
    near = await make_item(session, alice, "near")
    far = await make_item(session, alice, "far")
    await embed(session, focus, vector(1.0, 0.0))
    await embed(session, near, vector(1.0, 0.5))
    await embed(session, far, vector(0.0, 1.0))

    found = await deriver(session).candidates(focus.id, limit=10)

    # Both come back, including the one no floor would keep -- and in distance order.
    assert [c.item.id for c in found] == [near.id, far.id]
    assert found[0].score > found[1].score
    # And nothing was written: a scan is a read.
    assert await rows(session) == []


async def test_candidates_answers_empty_for_a_memory_with_no_vector(
    session: AsyncSession, alice: User
) -> None:
    focus = await make_item(session, alice, "no embedding")

    assert await deriver(session).candidates(focus.id) == []


# --------------------------------------------------------------------------------------
# The agent's tool, against the real repository
# --------------------------------------------------------------------------------------


async def test_get_connections_renders_rows_the_real_repository_returns(
    session: AsyncSession, alice: User
) -> None:
    """The tool, end to end, with no fake in the way.

    `tests/chat_engine/agent/test_connections_tool.py` builds its `VaultItem`s in memory,
    which hides the one failure this path actually had: `list_for_item` narrows rows with
    `load_only(..., raiseload=True)`, and rendering them as full cards reads
    `ai_highlights` -- a column `_CARD_COLUMNS` deliberately excludes. Every call that
    returned a neighbour raised against a real database and passed every offline test.

    So this one is deliberately DB-backed, and it asserts on the rendered text rather than
    on a mock: whatever else changes, a neighbour has to come back renderable.
    """
    from app.services.chat_engine.cards import short_id
    from app.services.chat_engine.toolbox import MemoryToolbox

    focus = await make_item(session, alice, "the book")
    chapter = await make_item(session, alice, "the chapter")
    await make_connection(session, alice, chapter, focus, relation=Relation.part_of)

    box = MemoryToolbox(
        alice.id, VaultRepository(session), connections=ConnectionRepository(session)
    )
    box.register_snapshot([focus])

    rendered = await box.get_connections(short_id(focus))

    assert "the chapter" in rendered
    assert short_id(chapter) in box.allowed_ids
    # Read from the book, a stored "chapter part_of book" means the book HAS a part in
    # the chapter -- and the focus has to be the subject of that sentence, or every
    # directional relation is handed to the model backwards.
    assert f"[{short_id(focus)}] has a part in this memory" in rendered


async def test_get_connections_reads_the_edge_from_the_other_end_too(
    session: AsyncSession, alice: User
) -> None:
    """The same row, expanded from the chapter instead. One edge, two true sentences."""
    from app.services.chat_engine.cards import short_id
    from app.services.chat_engine.toolbox import MemoryToolbox

    book = await make_item(session, alice, "the book")
    chapter = await make_item(session, alice, "the chapter")
    await make_connection(session, alice, chapter, book, relation=Relation.part_of)

    box = MemoryToolbox(
        alice.id, VaultRepository(session), connections=ConnectionRepository(session)
    )
    box.register_snapshot([chapter])

    rendered = await box.get_connections(short_id(chapter))

    assert f"[{short_id(chapter)}] is part of this memory" in rendered


async def test_a_stranger_edge_is_invisible_to_the_tool(
    session: AsyncSession, alice: User, bob: User
) -> None:
    """The tenant predicate is on the edge *and* on the neighbour row it joins."""
    from app.services.chat_engine.cards import short_id
    from app.services.chat_engine.toolbox import MemoryToolbox

    theirs_a = await make_item(session, bob, "bob a")
    theirs_b = await make_item(session, bob, "bob b")
    await make_connection(session, bob, theirs_a, theirs_b)

    box = MemoryToolbox(
        alice.id, VaultRepository(session), connections=ConnectionRepository(session)
    )
    box.register_snapshot([theirs_a])

    rendered = await box.get_connections(short_id(theirs_a))

    assert "no connections yet" in rendered
