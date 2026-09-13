"""What an edge does, from the outside.

These pin the rules that are easy to state and easy to break later: a pair has one edge
however it is drawn, a memory cannot connect to itself, a dismissal is remembered, and an
edge read from its far end reads backwards on purpose.

Several of them exist to exercise SQL that only a real Postgres runs -- the GENERATED
pair columns, the unique constraint over them, the `xmax` trick behind `created`, and the
`UNION ALL` that makes a neighbourhood one statement. None of that is reachable from a
unit test.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import ConnectionStatus, Relation
from app.models.connection import MemoryConnection
from app.models.user import User
from tests.conftest import make_connection, make_item

API = "/api/v1/connections"


async def _rows(session: AsyncSession) -> list[MemoryConnection]:
    return list((await session.exec(select(MemoryConnection))).all())


def _stub_store(
    monkeypatch: pytest.MonkeyPatch, chat_api: object, proposal: object
) -> str:
    """Hand the accept route one minted proposal, without a broker.

    The store is constructed inside the route rather than injected, so this replaces the
    class. `spend` is one-shot on purpose: the route's own contract is that a token is
    spent before anything is written, and a fake that could be spent twice would hide a
    regression in exactly that ordering.
    """
    spent: list[str] = []

    class _Store:
        async def spend(self, token: str, user_id: object) -> object | None:
            if token in spent:
                return None
            spent.append(token)
            return proposal if token == "tok" else None

    monkeypatch.setattr(chat_api, "RedisProposalStore", _Store)
    return "tok"


# --------------------------------------------------------------------------------------
# Creating
# --------------------------------------------------------------------------------------


async def test_connecting_two_memories_returns_the_far_end_as_a_card(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    source = await make_item(session, alice, "second brain")
    target = await make_item(session, alice, "smart notes")

    response = await alice_client.post(
        API,
        json={
            "source_id": str(source.id),
            "target_id": str(target.id),
            "relation": "expands",
            "note": "builds on it",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    edge = body["connection"]
    assert edge["relation"] == "expands"
    assert edge["direction"] == "outgoing"
    assert edge["origin"] == "user"
    assert edge["status"] == "confirmed"
    assert edge["note"] == "builds on it"
    # A hand-drawn edge was never measured, and a zero would be a measurement.
    assert edge["score"] is None
    assert edge["memory"]["id"] == str(target.id)


async def test_a_card_never_carries_the_memory_body(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """`VaultItemRead`, never `VaultItemDetail` -- and the read is narrowed to match.

    Also the `raiseload` check: the neighbourhood query loads only card columns, so a
    field added to the response without being added to `_CARD_COLUMNS` raises here rather
    than emitting a silent per-row query.
    """
    source = await make_item(session, alice, "focus")
    target = await make_item(session, alice, "neighbour")
    await make_connection(session, alice, source, target)

    body = (await alice_client.get(f"{API}/for-item/{source.id}")).json()

    card = body["connections"][0]["memory"]
    assert "content" not in card
    assert "ai_highlights" not in card
    assert "item_metadata" not in card
    assert "storage_key" not in card


async def test_a_memory_cannot_be_connected_to_itself(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    item = await make_item(session, alice, "lonely")

    response = await alice_client.post(
        API, json={"source_id": str(item.id), "target_id": str(item.id)}
    )

    # The schema refuses it, so this costs no statement. The CHECK constraint behind it
    # is what stops one arriving by any other route.
    assert response.status_code == 422


async def test_an_unknown_relation_is_refused(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    source = await make_item(session, alice, "a")
    target = await make_item(session, alice, "b")

    response = await alice_client.post(
        API,
        json={
            "source_id": str(source.id),
            "target_id": str(target.id),
            "relation": "vaguely_about",
        },
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------------------
# One edge per pair, however it is drawn
# --------------------------------------------------------------------------------------


async def test_reconnecting_the_same_pair_backwards_updates_the_one_edge(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """200 and `created: false`, never 409.

    Somebody connects A to B from A's page and, a week later, from B's. Re-adding is a
    normal outcome -- a 409 would make the obvious action look like a broken server.
    """
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")

    first = await alice_client.post(
        API, json={"source_id": str(a.id), "target_id": str(b.id), "relation": "expands"}
    )
    second = await alice_client.post(
        API,
        json={"source_id": str(b.id), "target_id": str(a.id), "relation": "depends_on"},
    )

    assert first.json()["created"] is True
    assert second.status_code == 200
    assert second.json()["created"] is False
    # The unique key is the unordered pair, so the second call re-labelled the first edge.
    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].relation == Relation.depends_on.value


async def test_a_symmetric_relation_is_stored_from_the_lower_id(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """`related_to` and `contradicts` read the same from both ends, so the stored
    direction carries no meaning and is fixed. Left to chance, half of them render
    backwards and somebody eventually "fixes" a row that was never wrong."""
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    low, high = sorted([a.id, b.id])

    await alice_client.post(
        API,
        json={"source_id": str(high), "target_id": str(low), "relation": "contradicts"},
    )

    rows = await _rows(session)
    assert rows[0].source_item_id == low
    assert rows[0].target_item_id == high


# --------------------------------------------------------------------------------------
# Reading from either end
# --------------------------------------------------------------------------------------


async def test_an_edge_reads_backwards_from_its_far_end(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """One row, two readings. This is what "bidirectional navigation" means here: the
    edge is stored once and the *read* decides which way round it is."""
    source = await make_item(session, alice, "the book")
    target = await make_item(session, alice, "the chapter")
    await make_connection(session, alice, source, target, relation=Relation.part_of)

    from_source = (await alice_client.get(f"{API}/for-item/{source.id}")).json()
    from_target = (await alice_client.get(f"{API}/for-item/{target.id}")).json()

    assert from_source["connections"][0]["direction"] == "outgoing"
    assert from_source["connections"][0]["memory"]["id"] == str(target.id)
    assert from_target["connections"][0]["direction"] == "incoming"
    assert from_target["connections"][0]["memory"]["id"] == str(source.id)
    # Same row both times -- not two.
    assert from_source["connections"][0]["id"] == from_target["connections"][0]["id"]


async def test_suggestions_are_hidden_from_a_neighbourhood_until_asked_for(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    focus = await make_item(session, alice, "focus")
    confirmed = await make_item(session, alice, "confirmed neighbour")
    proposed = await make_item(session, alice, "proposed neighbour")
    await make_connection(session, alice, focus, confirmed)
    await make_connection(
        session, alice, focus, proposed, status=ConnectionStatus.suggested
    )

    default = (await alice_client.get(f"{API}/for-item/{focus.id}")).json()
    widened = (
        await alice_client.get(f"{API}/for-item/{focus.id}?include_suggested=true")
    ).json()

    assert [c["memory"]["id"] for c in default["connections"]] == [str(confirmed.id)]
    assert default["total"] == 1
    assert len(widened["connections"]) == 2


async def test_a_deleted_neighbour_leaves_the_neighbourhood(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """A scrubbed tombstone must never render as `Untitled` beside a live memory.

    `VaultRepository.delete` removes the edges outright, so this is belt and braces --
    but the filter is what holds if an edge is ever written another way.
    """
    focus = await make_item(session, alice, "focus")
    doomed = await make_item(session, alice, "doomed")
    await make_connection(session, alice, focus, doomed)

    assert (await alice_client.delete(f"/api/v1/vault/{doomed.id}")).status_code == 204

    body = (await alice_client.get(f"{API}/for-item/{focus.id}")).json()
    assert body["connections"] == []
    assert await _rows(session) == []


async def test_a_deleted_memory_cannot_be_a_focus(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    doomed = await make_item(session, alice, "doomed")
    await alice_client.delete(f"/api/v1/vault/{doomed.id}")

    assert (await alice_client.get(f"{API}/for-item/{doomed.id}")).status_code == 404


async def test_an_unknown_focus_is_a_404(alice_client: AsyncClient) -> None:
    assert (await alice_client.get(f"{API}/for-item/{uuid.uuid4()}")).status_code == 404


# --------------------------------------------------------------------------------------
# Confirming, dismissing, retyping, removing
# --------------------------------------------------------------------------------------


async def test_confirming_a_suggestion_makes_it_visible(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    focus = await make_item(session, alice, "focus")
    other = await make_item(session, alice, "other")
    edge = await make_connection(
        session, alice, focus, other, status=ConnectionStatus.suggested, score=0.71
    )

    response = await alice_client.post(f"{API}/{edge.id}/confirm")

    assert response.status_code == 200
    assert response.json()["status"] == "confirmed"
    # The score it was drawn at survives confirmation -- it is what the edge was
    # proposed on, and a person accepting it does not make it a measurement they took.
    assert response.json()["score"] == 0.71
    body = (await alice_client.get(f"{API}/for-item/{focus.id}")).json()
    assert len(body["connections"]) == 1


async def test_confirming_twice_is_a_404(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    focus = await make_item(session, alice, "focus")
    other = await make_item(session, alice, "other")
    edge = await make_connection(
        session, alice, focus, other, status=ConnectionStatus.suggested
    )

    await alice_client.post(f"{API}/{edge.id}/confirm")

    assert (await alice_client.post(f"{API}/{edge.id}/confirm")).status_code == 404


async def test_a_dismissed_pair_keeps_its_row(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """The row *is* the record that stops the derivation re-proposing the pair. Deleting
    it instead would make "never suggest this" last until the next capture."""
    focus = await make_item(session, alice, "focus")
    other = await make_item(session, alice, "other")
    edge = await make_connection(
        session, alice, focus, other, status=ConnectionStatus.suggested
    )

    assert (await alice_client.post(f"{API}/{edge.id}/dismiss")).status_code == 204

    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].status == ConnectionStatus.dismissed.value
    assert rows[0].dismissed_at is not None
    # And it is not in the neighbourhood, nor in the suggestions inbox.
    assert (await alice_client.get(f"{API}/for-item/{focus.id}")).json()["connections"] == []
    assert (await alice_client.get(f"{API}/suggestions")).json()["suggestions"] == []


async def test_connecting_a_dismissed_pair_by_hand_revives_it(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """A person deliberately connecting two memories overrides their own earlier "don't
    suggest this". Refusing would be a dead end nobody could guess the way out of."""
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    edge = await make_connection(
        session, alice, a, b, status=ConnectionStatus.suggested
    )
    await alice_client.post(f"{API}/{edge.id}/dismiss")

    response = await alice_client.post(
        API, json={"source_id": str(a.id), "target_id": str(b.id), "relation": "supports"}
    )

    assert response.status_code == 200
    assert response.json()["connection"]["status"] == "confirmed"
    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].dismissed_at is None
    assert rows[0].relation == Relation.supports.value


async def test_retyping_an_edge(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    edge = await make_connection(session, alice, a, b)

    response = await alice_client.patch(
        f"{API}/{edge.id}", json={"relation": "contradicts", "note": "disagrees"}
    )

    assert response.status_code == 200
    assert response.json()["relation"] == "contradicts"
    assert response.json()["note"] == "disagrees"
    assert len(await _rows(session)) == 1


async def test_an_empty_note_clears_it(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """`None` means leave it alone; the empty string is how a note is cleared. A nullable
    field with two meanings for null is a field nobody can use correctly."""
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    edge = await make_connection(session, alice, a, b)
    await alice_client.patch(f"{API}/{edge.id}", json={"note": "keep me"})

    untouched = await alice_client.patch(f"{API}/{edge.id}", json={"relation": "expands"})
    cleared = await alice_client.patch(f"{API}/{edge.id}", json={"note": ""})

    assert untouched.json()["note"] == "keep me"
    assert cleared.json()["note"] is None


async def test_deleting_an_edge(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    edge = await make_connection(session, alice, a, b)

    assert (await alice_client.delete(f"{API}/{edge.id}")).status_code == 204
    assert (await alice_client.delete(f"{API}/{edge.id}")).status_code == 404
    assert await _rows(session) == []


# --------------------------------------------------------------------------------------
# The suggestions inbox
# --------------------------------------------------------------------------------------


async def test_suggestions_come_back_strongest_first_with_both_ends(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """Per user, not per memory: an edge found from a new capture's side is a suggestion
    on *both* of its memories, so offering it per memory offers one decision twice."""
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    c = await make_item(session, alice, "c")
    await make_connection(session, alice, a, b, status=ConnectionStatus.suggested, score=0.64)
    await make_connection(session, alice, a, c, status=ConnectionStatus.suggested, score=0.81)

    body = (await alice_client.get(f"{API}/suggestions")).json()

    assert body["total"] == 2
    assert [s["score"] for s in body["suggestions"]] == [0.81, 0.64]
    first = body["suggestions"][0]
    assert {first["source"]["id"], first["target"]["id"]} == {str(a.id), str(c.id)}


# --------------------------------------------------------------------------------------
# Relation typing (the only route that spends a model call)
# --------------------------------------------------------------------------------------


async def test_typing_is_unavailable_by_default_and_calls_no_provider(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """503 before anything else happens.

    The switch is checked *before* the edge is read, so a deployment with typing off never
    reaches a provider and never even spends a statement finding out whose edge it is.
    `_no_provider_calls` would turn a leak here into a hard failure, which is the point.
    """
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    edge = await make_connection(session, alice, a, b)

    response = await alice_client.post(f"{API}/{edge.id}/retype")

    assert response.status_code == 503
    await session.refresh(edge)
    assert edge.relation == Relation.related_to.value
    assert edge.ai_reason is None


async def test_typing_labels_the_edge_and_marks_it_machine_written(
    alice_client: AsyncClient,
    session: AsyncSession,
    alice: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's sentence goes to `ai_reason`, never to `note`.

    Rendering a model's account of two memories as something its owner typed is the one
    way this feature can lie -- the same rule `item_metadata["content_source"] = "vision"`
    exists for.
    """
    from app.ai import connections as typing_ai
    from app.services import connection_service

    a = await make_item(session, alice, "second brain")
    b = await make_item(session, alice, "smart notes")
    edge = await make_connection(session, alice, a, b, relation=Relation.related_to)
    await alice_client.patch(f"{API}/{edge.id}", json={"note": "mine"})

    async def _typed(card_a: str, card_b: str) -> typing_ai.ConnectionTyping:
        # The prompt is shown cards, never bodies: a card carries the label, the summary
        # and the tags, and `content` is the field that does not fit.
        assert "body of" not in card_a and "body of" not in card_b
        return typing_ai.ConnectionTyping(
            relation=Relation.expands, swap=False, reason="Both cover PARA."
        )

    monkeypatch.setattr(connection_service.connections_ai, "typing_available", lambda: True)
    monkeypatch.setattr(connection_service.connections_ai, "type_connection", _typed)

    body = (await alice_client.post(f"{API}/{edge.id}/retype")).json()

    assert body["relation"] == "expands"
    assert body["ai_reason"] == "Both cover PARA."
    # The person's own note is untouched. Two columns, two authors.
    assert body["note"] == "mine"


async def test_typing_can_swap_the_ends(
    alice_client: AsyncClient,
    session: AsyncSession,
    alice: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`b_to_a` means the label only reads correctly the other way round. Swapping is free
    because the unique key is the unordered pair -- the row keeps its identity."""
    from app.ai import connections as typing_ai
    from app.services import connection_service

    a = await make_item(session, alice, "the chapter")
    b = await make_item(session, alice, "the book")
    edge = await make_connection(session, alice, b, a)

    async def _typed(_a: str, _b: str) -> typing_ai.ConnectionTyping:
        return typing_ai.ConnectionTyping(
            relation=Relation.part_of, swap=True, reason="One is a chapter of the other."
        )

    monkeypatch.setattr(connection_service.connections_ai, "typing_available", lambda: True)
    monkeypatch.setattr(connection_service.connections_ai, "type_connection", _typed)

    await alice_client.post(f"{API}/{edge.id}/retype")

    await session.refresh(edge)
    assert edge.source_item_id == a.id
    assert edge.target_item_id == b.id
    assert len(await _rows(session)) == 1


async def test_typing_a_stranger_edge_is_a_404(
    bob_client: AsyncClient,
    session: AsyncSession,
    alice: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import connection_service

    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    edge = await make_connection(session, alice, a, b)
    monkeypatch.setattr(connection_service.connections_ai, "typing_available", lambda: True)

    assert (await bob_client.post(f"{API}/{edge.id}/retype")).status_code == 404


async def test_typing_is_capped_per_user(
    alice_client: AsyncClient,
    session: AsyncSession,
    alice: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only connection route that spends a model call, so the only one with a cap.
    Keyed by user id and not by IP: a cost belongs to the account that incurred it."""
    from app.services import connection_service

    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    edge = await make_connection(session, alice, a, b)

    # Only this namespace. `rate_limit.consume` is shared with the per-IP request
    # middleware, so a blanket stub answers 429 from the middleware before the route ever
    # runs -- which passes for the wrong reason and would keep passing if the service
    # stopped checking its cap entirely.
    async def _spent(namespace: str, *_: object, **__: object) -> bool:
        return namespace != "connection_typing"

    monkeypatch.setattr(connection_service.connections_ai, "typing_available", lambda: True)
    monkeypatch.setattr(connection_service.rate_limit, "consume", _spent)

    response = await alice_client.post(f"{API}/{edge.id}/retype")

    assert response.status_code == 503


# --------------------------------------------------------------------------------------
# Hubs: what the page opens on when nobody named a memory
# --------------------------------------------------------------------------------------


async def test_hubs_are_ordered_by_how_connected_they_are(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """The reason this exists: the newest capture is usually the *least* connected, since
    everything it links to was found from its side and nothing has been saved since. A
    picker ordered by date points at exactly the wrong memories."""
    hub = await make_item(session, alice, "hub")
    spoke_a = await make_item(session, alice, "spoke a")
    spoke_b = await make_item(session, alice, "spoke b")
    await make_connection(session, alice, hub, spoke_a)
    await make_connection(session, alice, spoke_b, hub)

    body = (await alice_client.get(f"{API}/hubs")).json()

    assert [h["memory"]["id"] for h in body["hubs"]][0] == str(hub.id)
    assert body["hubs"][0]["connection_count"] == 2
    assert {h["connection_count"] for h in body["hubs"][1:]} == {1}


async def test_an_empty_vault_has_no_hubs_and_that_is_not_an_error(
    alice_client: AsyncClient,
) -> None:
    response = await alice_client.get(f"{API}/hubs")

    assert response.status_code == 200
    assert response.json()["hubs"] == []


async def test_hubs_ignore_suggestions_and_dismissals(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """A hub's count has to mean "this many cards are waiting on that page". Counting
    undecided or declined edges promises memories the neighbourhood read will not show."""
    focus = await make_item(session, alice, "focus")
    other = await make_item(session, alice, "other")
    await make_connection(
        session, alice, focus, other, status=ConnectionStatus.suggested
    )

    assert (await alice_client.get(f"{API}/hubs")).json()["hubs"] == []


async def test_a_hub_count_never_includes_a_deleted_neighbour(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    hub = await make_item(session, alice, "hub")
    alive = await make_item(session, alice, "alive")
    doomed = await make_item(session, alice, "doomed")
    await make_connection(session, alice, hub, alive)
    await make_connection(session, alice, hub, doomed)
    await alice_client.delete(f"/api/v1/vault/{doomed.id}")

    body = (await alice_client.get(f"{API}/hubs")).json()

    assert body["hubs"][0]["connection_count"] == 1


async def test_hubs_are_scoped_to_the_caller(
    bob_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    a = await make_item(session, alice, "alice a")
    b = await make_item(session, alice, "alice b")
    await make_connection(session, alice, a, b)

    assert (await bob_client.get(f"{API}/hubs")).json()["hubs"] == []


# --------------------------------------------------------------------------------------
# Connecting by tap (the far side of the agent's proposal)
# --------------------------------------------------------------------------------------


async def test_accepting_a_connect_proposal_writes_the_edge(
    alice_client: AsyncClient,
    session: AsyncSession,
    alice: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write happens in a route with no model in it.

    A model may *ask* to connect two memories; this is the code that does it, and its
    inputs are a token and a resolved account. That separation is the whole reason the
    agent has no `Connect` tool -- tool results are scraped page text.
    """
    from app.api.v1 import chat as chat_api
    from app.services.chat_engine.proposals import Action, Proposal

    a = await make_item(session, alice, "second brain")
    b = await make_item(session, alice, "smart notes")
    token = _stub_store(
        monkeypatch,
        chat_api,
        Proposal(
            alice.id,
            Action.connect,
            {
                "source_id": str(a.id),
                "target_id": str(b.id),
                "relation": "expands",
            },
        ),
    )

    response = await alice_client.post(f"/api/v1/chat/proposals/{token}/accept")

    assert response.status_code == 200
    assert response.json()["status"] == "connected"
    rows = await _rows(session)
    assert len(rows) == 1
    assert rows[0].relation == Relation.expands.value
    assert rows[0].origin == "user"
    assert rows[0].status == ConnectionStatus.confirmed.value


async def test_a_connect_proposal_cannot_reach_a_stranger_memory(
    alice_client: AsyncClient,
    session: AsyncSession,
    alice: User,
    bob: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ids in a proposal are not trusted just because a proposal carried them.

    The service re-checks **both** ends against the tapping account, per item -- so a
    token that somehow named somebody else's memory writes nothing and reads as expired.
    """
    from app.api.v1 import chat as chat_api
    from app.services.chat_engine.proposals import Action, Proposal

    mine = await make_item(session, alice, "mine")
    theirs = await make_item(session, bob, "bob's")
    token = _stub_store(
        monkeypatch,
        chat_api,
        Proposal(
            alice.id,
            Action.connect,
            {
                "source_id": str(mine.id),
                "target_id": str(theirs.id),
                "relation": "related_to",
            },
        ),
    )

    response = await alice_client.post(f"/api/v1/chat/proposals/{token}/accept")

    assert response.status_code == 404
    assert await _rows(session) == []


async def test_a_connect_proposal_with_a_bad_relation_falls_back(
    alice_client: AsyncClient,
    session: AsyncSession,
    alice: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relation crosses Redis as a string. A value that is not a relation must never
    reach a column that everything downstream reads as one."""
    from app.api.v1 import chat as chat_api
    from app.services.chat_engine.proposals import Action, Proposal

    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    token = _stub_store(
        monkeypatch,
        chat_api,
        Proposal(
            alice.id,
            Action.connect,
            {"source_id": str(a.id), "target_id": str(b.id), "relation": "nonsense"},
        ),
    )

    await alice_client.post(f"/api/v1/chat/proposals/{token}/accept")

    rows = await _rows(session)
    assert rows[0].relation == Relation.related_to.value
