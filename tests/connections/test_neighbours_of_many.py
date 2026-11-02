"""The read behind a chat answer's suggestions, against a real database.

It has to be real: `raiseload` only raises on a row that came from a session, the `UNION
ALL` and the `CASE` ordering only exist in SQL, and the tenant predicate is the kind of
line a fake would happily agree with whether it was there or not.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.ai.chat import history
from app.ai.chat.harness import graph
from app.ai.chat.harness.schemas import FinalAnswer
from app.core.config import settings
from app.models.base import ConnectionStatus, Relation
from app.models.user import User
from app.repositories.connection import ConnectionRepository
from app.repositories.vault import VaultRepository
from app.services.chat_engine.cards import memory_link, short_id
from app.services.chat_engine.engine import ChatEngine
from app.services.chat_engine.evidence import RetrievedMemory
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.chat_engine.toolbox import MemoryToolbox
from app.services.chat_engine.types import InboundMessage
from app.services.recall_agent import RecallAgentService
from app.services.surfaces.telegram.render import render
from tests.conftest import make_connection, make_item


async def test_both_ends_of_an_edge_are_read(session: AsyncSession, alice: User) -> None:
    focus = await make_item(session, alice, "focus")
    downstream = await make_item(session, alice, "downstream")
    upstream = await make_item(session, alice, "upstream")
    await make_connection(session, alice, focus, downstream, relation=Relation.expands)
    await make_connection(session, alice, upstream, focus, relation=Relation.part_of)

    rows = await ConnectionRepository(session).neighbours_of_many(alice.id, [focus.id])

    found = {n.item.title: (asked, n.direction) for asked, n in rows}
    assert found == {
        "downstream": (focus.id, "outgoing"),
        "upstream": (focus.id, "incoming"),
    }


async def test_only_a_confirmed_edge_is_ever_returned(
    session: AsyncSession, alice: User
) -> None:
    """A suggestion is a guess nobody approved, and a page can be written to earn one. The
    tap is the security control, so there is deliberately no argument that skips it."""
    focus = await make_item(session, alice, "focus")
    confirmed = await make_item(session, alice, "confirmed")
    suggested = await make_item(session, alice, "suggested")
    dismissed = await make_item(session, alice, "dismissed")
    await make_connection(session, alice, focus, confirmed)
    await make_connection(session, alice, focus, suggested, status=ConnectionStatus.suggested)
    await make_connection(session, alice, focus, dismissed, status=ConnectionStatus.dismissed)

    rows = await ConnectionRepository(session).neighbours_of_many(alice.id, [focus.id])

    assert [neighbour.item.title for _, neighbour in rows] == ["confirmed"]


async def test_another_persons_memories_are_unreachable(
    session: AsyncSession, alice: User, bob: User
) -> None:
    mine = await make_item(session, alice, "mine")
    theirs = await make_item(session, bob, "theirs")
    their_other = await make_item(session, bob, "their other")
    await make_connection(session, bob, theirs, their_other)

    repo = ConnectionRepository(session)

    # Naming someone else's memory, and asking as someone with no edges at all.
    assert await repo.neighbours_of_many(alice.id, [theirs.id]) == []
    assert await repo.neighbours_of_many(alice.id, [mine.id]) == []


async def test_a_deleted_neighbour_is_not_offered(
    session: AsyncSession, alice: User
) -> None:
    focus = await make_item(session, alice, "focus")
    gone = await make_item(session, alice, "gone")
    await make_connection(session, alice, focus, gone)
    await VaultRepository(session).trash(gone)
    await session.commit()

    assert await ConnectionRepository(session).neighbours_of_many(alice.id, [focus.id]) == []


async def test_memories_the_answer_already_used_are_left_out(
    session: AsyncSession, alice: User
) -> None:
    one = await make_item(session, alice, "one")
    two = await make_item(session, alice, "two")
    named = await make_item(session, alice, "named")
    fresh = await make_item(session, alice, "fresh")
    await make_connection(session, alice, one, two)
    await make_connection(session, alice, one, named)
    await make_connection(session, alice, one, fresh)

    rows = await ConnectionRepository(session).neighbours_of_many(
        alice.id, [one.id, two.id], exclude=[named.id]
    )

    assert [neighbour.item.title for _, neighbour in rows] == ["fresh"]


async def test_the_limit_cuts_the_least_relevant_memory_first(
    session: AsyncSession, alice: User
) -> None:
    """`item_ids` arrives most relevant first. The newest edge in the vault must not push out
    the neighbours of the memory the answer was actually about."""
    best = await make_item(session, alice, "best")
    worse = await make_item(session, alice, "worse")
    near = await make_item(session, alice, "near best")
    newest = await make_item(session, alice, "newer, near worse")
    await make_connection(session, alice, best, near)
    await make_connection(session, alice, worse, newest)

    rows = await ConnectionRepository(session).neighbours_of_many(
        alice.id, [best.id, worse.id], limit=1
    )

    assert [(asked, n.item.title) for asked, n in rows] == [(best.id, "near best")]


async def test_nothing_asked_about_costs_no_statement(
    session: AsyncSession, alice: User
) -> None:
    assert await ConnectionRepository(session).neighbours_of_many(alice.id, []) == []


async def test_what_a_suggestion_reads_is_loaded(session: AsyncSession, alice: User) -> None:
    """The `raiseload` check. Rows are read `load_only`, so a field the renderers touch that
    is missing from the card columns raises here rather than for a person on a live turn."""
    focus = await make_item(session, alice, "focus")
    other = await make_item(session, alice, "other")
    await make_connection(session, alice, focus, other, relation=Relation.expands)

    [(_, neighbour)] = await ConnectionRepository(session).neighbours_of_many(
        alice.id, [focus.id]
    )

    assert neighbour.item.title == "other"
    assert neighbour.item.source_url is None or isinstance(neighbour.item.source_url, str)
    assert str(neighbour.connection.relation) == Relation.expands.value
    assert short_id(neighbour.item)
    assert memory_link(neighbour.item)


async def test_a_whole_turn_over_real_rows_ends_in_a_message_a_person_can_read(
    session: AsyncSession, alice: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything but the model: the real search result, snapshot, edge read, engine and
    renderer. This is the path a live question takes, and rows read `load_only` are the
    only ones that can show a renderer touching a column that was never loaded."""
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recall.example")
    asked = await make_item(session, alice, "5 job sources")
    connected = await make_item(session, alice, "Moving to Germany")
    undecided = await make_item(session, alice, "A guess nobody approved")
    await make_connection(session, alice, asked, connected, relation=Relation.expands)
    await make_connection(
        session, alice, asked, undecided, status=ConnectionStatus.suggested
    )

    async def _recall(*_: Any, **__: Any) -> list[RetrievedMemory]:
        return [RetrievedMemory(asked, 0.9)]

    async def _run(
        question: str, past: Any, executor: MemoryToolbox, **_: Any
    ) -> AsyncIterator[graph.AgentEvent]:
        await executor.query_memories(text=question)
        yield graph.AgentEnd(
            calls=["QueryMemories"], final=FinalAnswer(text="You saved 5 job sources.")
        )

    remembered: list[str] = []

    async def _load(_: str) -> list[Any]:
        return []

    async def _append(_session: str, _question: str, answer: str) -> None:
        remembered.append(answer)

    monkeypatch.setattr(MemoryRetriever, "recall", _recall)
    monkeypatch.setattr(graph, "run_agent", _run)
    monkeypatch.setattr(history, "load", _load)
    monkeypatch.setattr(history, "append", _append)

    repo = VaultRepository(session)
    service = RecallAgentService(repo, None, ConnectionRepository(session))
    reply = await ChatEngine(service, alice.id).handle(
        InboundMessage(
            surface="test",
            external_user_id="1",
            external_chat_id="1",
            text="where did i save those job sites?",
        )
    )

    assert render(reply) == (
        "You saved 5 job sources.\n\n"
        "🔗 <b>Connected in your vault</b>\n"
        f'• <a href="https://recall.example/memory/{connected.id}">Moving to Germany</a>'
        " — is expanded by <i>5 job sources</i>"
    )
    assert remembered == ["You saved 5 job sources."]
