"""The recall lane: when it spends a model call, and how much of a memory it shows.

Three rules carry the cost and the honesty argument for the whole feature. Nothing
*relevant* must not reach the answer model -- an empty or merely-nearest context invites
it to invent a memory and charges for the privilege, and "nothing relevant" includes the
eight distant rows a vector search returns when the vault holds nothing on the subject. A
memory's body is shown only when the question asked for the words rather than for which
memory it was. And what the model produces is checked against the evidence it was given
before anyone reads it.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.ai.chat import chain, history
from app.ai.chat.planner import MemoryQuery
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id
from app.services.chat_engine.evidence import RetrievedMemory
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.recall_chat import RecallChatService

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_BODY = "Redis persists with RDB snapshots and an append-only file. " * 30


#: Comfortably above `RECALL_STRONG_SCORE` unless a test says otherwise.
_STRONG = 0.9


def _item(n: int = 0) -> VaultItem:
    return VaultItem(
        user_id=_USER,
        type=ContentType.article,
        title=f"Redis persistence {n}",
        ai_label=f"How Redis persistence works, part {n}",
        summary="RDB snapshots versus the append-only file.",
        content=_BODY,
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


@pytest.fixture
def _offline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """No provider, no Redis. Records whether the answer model was reached."""
    seen: dict[str, Any] = {
        "answers": [],
        "contexts": [],
        "recalls": [],
        "guidance": [],
        "stored": [],
    }

    async def _plan(question: str) -> MemoryQuery:
        return MemoryQuery(search_text="redis")

    async def _answer(
        question: str,
        documents: Any,
        past: Any,
        guidance: str = chain.GUIDANCE_SUPPORTED,
    ) -> str:
        seen["answers"].append(question)
        seen["contexts"].append(chain.format_context(documents))
        seen["guidance"].append(guidance)
        return seen.get("reply", "Two memories cover it.")

    async def _load(session_id: str) -> list[Any]:
        return []

    async def _append(session_id: str, q: str, a: str) -> None:
        seen["stored"].append(a)

    monkeypatch.setattr("app.services.recall_chat.planner.plan", _plan)
    monkeypatch.setattr(chain, "answer", _answer)
    monkeypatch.setattr(history, "load", _load)
    monkeypatch.setattr(history, "append", _append)
    return seen


def _with_memories(
    monkeypatch: pytest.MonkeyPatch,
    items: list[VaultItem],
    score: float = _STRONG,
) -> list[int]:
    """Stub retrieval and record the limit it was asked for."""
    limits: list[int] = []

    async def _recall(
        self: MemoryRetriever,
        user_id: uuid.UUID,
        question: str,
        filters: Any = None,
        *,
        limit: int = 8,
    ) -> list[RetrievedMemory]:
        limits.append(limit)
        return [RetrievedMemory(item, score) for item in items[:limit]]

    monkeypatch.setattr(MemoryRetriever, "recall", _recall)
    return limits


# --- zero hits -----------------------------------------------------------------------


async def test_zero_hits_never_reaches_the_answer_model(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_memories(monkeypatch, [])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    answer = await service.answer(_USER, "what did I save about redis?", "555000")

    assert _offline["answers"] == []
    assert answer.text is not None and "couldn't find" in answer.text
    assert not answer.failed


# --- cards by default ----------------------------------------------------------------


async def test_the_default_answer_sees_cards_and_not_bodies(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = _with_memories(monkeypatch, [_item(n) for n in range(8)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what did I save about redis?", "555000")

    context = _offline["contexts"][0]
    assert "How Redis persistence works, part 0" in context
    assert "RDB snapshots and an append-only file" not in context
    assert limits == [8]


# --- bodies when asked ---------------------------------------------------------------


async def test_a_detail_question_sees_the_body_of_fewer_memories(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = _with_memories(monkeypatch, [_item(n) for n in range(8)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what exactly did that redis article say?", "555000")

    context = _offline["contexts"][0]
    assert "RDB snapshots and an append-only file" in context
    assert "full text:" in context
    assert limits == [2], "a detail answer reads fewer memories, more closely"


async def test_a_detail_answer_still_names_the_memory(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The card stays on top: an answer that cannot say *which* memory is useless."""
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what exactly did that article say?", "555000")

    assert "How Redis persistence works, part 0" in _offline["contexts"][0]


async def test_every_block_is_still_fenced_as_quoted_material(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what exactly did that article say?", "555000")

    context = _offline["contexts"][0]
    assert context.startswith("<memory id=") and context.endswith("</memory>")


# --- relevance is a floor, not an ordering -------------------------------------------


async def test_distant_matches_are_treated_as_nothing_found(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Top-k always returns rows. Rows are not evidence.

    This is the case the zero-hit short circuit never covered: the vault holds eight
    things, none of them about the subject, and the search dutifully returns all eight.
    """
    _with_memories(monkeypatch, [_item(n) for n in range(8)], score=0.1)
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    answer = await service.answer(_USER, "what did I save about redis?", "555000")

    assert _offline["answers"] == [], "a distant match must not buy a model call"
    assert answer.text is not None and "couldn't find" in answer.text
    assert not answer.failed


async def test_a_weak_match_is_answered_but_told_it_is_weak(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the floor and the strong threshold, the answer is hedged rather than
    dropped: "I found something related but it doesn't say" is a true and useful reply,
    and it is only honest if the model was told which of the two situations it is in."""
    _with_memories(monkeypatch, [_item(0)], score=0.6)
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what did I save about redis?", "555000")

    assert _offline["guidance"] == [chain.GUIDANCE_WEAK]


async def test_a_strong_match_gets_the_ordinary_guidance(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what did I save about redis?", "555000")

    assert _offline["guidance"] == [chain.GUIDANCE_SUPPORTED]


# --- what comes back is checked -------------------------------------------------------


async def test_a_citation_of_a_memory_that_was_never_supplied_is_stripped(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id the model produced from nothing is the clearest fabrication signal there is."""
    _offline["reply"] = "You saved two things [deadbeef] about Redis."
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    answer = await service.answer(_USER, "what did I save about redis?", "555000")

    assert answer.text is not None and "deadbeef" not in answer.text
    assert answer.text.startswith("You saved two things about Redis")


async def test_an_invented_link_does_not_reach_the_user(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link is the one claim in an answer that can be checked against its evidence --
    and the one a person is invited to tap."""
    _offline["reply"] = "It is at https://redis.example.invalid/guide."
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    answer = await service.answer(_USER, "what did I save about redis?", "555000")

    assert answer.text is not None and "redis.example.invalid" not in answer.text


async def test_the_stored_history_is_the_checked_answer_not_the_raw_one(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """History is replayed into the next prompt. A fabrication kept there comes back as
    context and gets built on."""
    _offline["reply"] = "Saved [deadbeef] last week."
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    await service.answer(_USER, "what did I save about redis?", "555000")

    assert _offline["stored"] == ["Saved last week."]


async def test_an_empty_reply_is_a_failure_rather_than_a_blank_message(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _offline["reply"] = "   "
    _with_memories(monkeypatch, [_item(0)])
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    answer = await service.answer(_USER, "what did I save about redis?", "555000")

    assert answer.failed and not answer.text


async def test_the_answer_carries_the_ids_it_was_generated_from(
    _offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing renders these yet; they are what makes a wrong answer traceable."""
    items = [_item(0), _item(1)]
    _with_memories(monkeypatch, items)
    service = RecallChatService(repo=None)  # type: ignore[arg-type]

    answer = await service.answer(_USER, "what did I save about redis?", "555000")

    assert answer.memory_ids == tuple(short_id(item) for item in items)
