"""Whether retrieved rows are evidence, decided before a prompt exists.

The property under test is not "the filter works" but the one that makes the filter worth
having: a vector search cannot return nothing. Ask a vault of holiday photos about Redis
and it returns the eight least-unrelated photos in confident order, and every one of the
old zero-hit protections passes them straight through to the answer model.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id
from app.services.chat_engine.evidence import (
    EvidenceStatus,
    RetrievedMemory,
    assess,
    from_rows,
    score_from_distance,
)

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item(n: int = 0) -> VaultItem:
    return VaultItem(
        user_id=_USER,
        type=ContentType.article,
        title=f"Memory {n}",
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def _memories(*scores: float) -> list[RetrievedMemory]:
    return [RetrievedMemory(_item(n), score) for n, score in enumerate(scores)]


# --- the score itself -----------------------------------------------------------------


def test_a_distance_becomes_a_similarity() -> None:
    assert score_from_distance(0.0) == 1.0
    assert score_from_distance(0.3) == pytest.approx(0.7)


def test_an_opposing_vector_is_not_more_relevant_than_an_unrelated_one() -> None:
    """Cosine distance runs to 2. Clamped, or the margin arithmetic reads backwards."""
    assert score_from_distance(1.8) == 0.0
    assert score_from_distance(-0.2) == 1.0


def test_repository_rows_convert_in_order() -> None:
    rows = [(_item(0), 0.1), (_item(1), 0.4)]
    assert [round(memory.score, 2) for memory in from_rows(rows)] == [0.9, 0.6]


# --- no evidence ----------------------------------------------------------------------


def test_nothing_retrieved_is_no_evidence() -> None:
    assert assess([]).status is EvidenceStatus.no_evidence


def test_eight_distant_rows_are_no_evidence_rather_than_a_weak_answer() -> None:
    """The case the zero-hit short circuit never caught.

    `no_evidence` and not `insufficient` on purpose: "I found something related but it
    doesn't say" is itself a claim about the vault, and rows that all failed the
    relevance floor are not grounds for making it.
    """
    evidence = assess(_memories(*[0.2] * 8), min_score=0.55, strong_score=0.68)

    assert evidence.status is EvidenceStatus.no_evidence
    assert evidence.memories == ()


# --- weak and strong ------------------------------------------------------------------


def test_above_the_floor_but_below_strong_is_insufficient() -> None:
    evidence = assess(_memories(0.6), min_score=0.55, strong_score=0.68)

    assert evidence.status is EvidenceStatus.insufficient
    assert len(evidence.memories) == 1


def test_a_strong_hit_is_supported() -> None:
    evidence = assess(_memories(0.9, 0.85), min_score=0.55, strong_score=0.68)

    assert evidence.status is EvidenceStatus.supported
    assert len(evidence.memories) == 2


def test_the_status_is_decided_by_the_best_hit_not_the_worst() -> None:
    """One memory that answers the question is an answer, whatever trails behind it."""
    evidence = assess(_memories(0.92, 0.6, 0.57), min_score=0.55, strong_score=0.68)

    assert evidence.status is EvidenceStatus.supported


# --- the two filters ------------------------------------------------------------------


def test_rows_below_the_floor_are_dropped_from_a_good_result() -> None:
    evidence = assess(
        _memories(0.9, 0.88, 0.2), min_score=0.55, strong_score=0.68, margin=1.0
    )

    assert [round(m.score, 2) for m in evidence.memories] == [0.9, 0.88]


def test_a_strong_match_is_not_diluted_by_mediocre_ones() -> None:
    """The provider-independent half. Seven passable memories beside one that actually
    answers is how an answer starts connecting things the user never connected."""
    evidence = assess(
        _memories(0.95, 0.6, 0.58, 0.56), min_score=0.55, strong_score=0.68, margin=0.15
    )

    assert [round(m.score, 2) for m in evidence.memories] == [0.95]


def test_close_scores_all_survive_the_margin() -> None:
    evidence = assess(
        _memories(0.9, 0.88, 0.8), min_score=0.55, strong_score=0.68, margin=0.15
    )

    assert len(evidence.memories) == 3


def test_ordering_is_re_established_rather_than_assumed() -> None:
    """Everything downstream reads `[0]` as the best match."""
    evidence = assess(_memories(0.6, 0.95, 0.7), min_score=0.55, margin=1.0)

    assert [round(m.score, 2) for m in evidence.memories] == [0.95, 0.7, 0.6]
    assert evidence.best_score == pytest.approx(0.95)


def test_a_floor_of_zero_keeps_everything_it_is_given() -> None:
    """Disabling the guard has to be possible, and has to be a deliberate act."""
    evidence = assess(_memories(0.05, 0.04), min_score=0.0, strong_score=0.68, margin=1.0)

    assert evidence.status is EvidenceStatus.insufficient
    assert len(evidence.memories) == 2


# --- what the caller reads ------------------------------------------------------------


def test_the_ids_are_the_ones_the_prompt_will_use() -> None:
    """Derived from the same function the card and the fence use.

    A second implementation of "the id of a memory" is a validator that approves an id
    it worked out itself rather than one the model was actually shown.
    """
    memories = _memories(0.9, 0.88)
    evidence = assess(memories, min_score=0.55, margin=1.0)

    assert evidence.ids == tuple(short_id(m.item) for m in memories)
    assert evidence.items == [m.item for m in memories]


def test_an_empty_result_has_no_best_score_rather_than_a_missing_one() -> None:
    assert assess([]).best_score == 0.0
