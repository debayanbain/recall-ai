"""Where connections stop.

A connection has exactly one owner, which makes this simpler than the Space boundary and
sharper: there is no role, no membership and no 403. "No such edge", "not yours" and "no
such memory" are one answer, because any other arrangement confirms an id exists to
somebody who may not see it.

The rule with the most history behind it is the one about *both* endpoints. Owning one
memory has never granted anything over another, and a request carrying two ids is exactly
where a stranger's is easiest to slip in -- Spaces had a real cross-tenant IDOR of that
shape once (`SpaceService._attach`). So the check is per item, not per request, and these
tests come at it from every direction a caller has.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import ConnectionStatus
from app.models.connection import MemoryConnection
from app.models.user import User
from tests.conftest import make_connection, make_item

API = "/api/v1/connections"


async def _count(session: AsyncSession) -> int:
    return len((await session.exec(select(MemoryConnection))).all())


# --------------------------------------------------------------------------------------
# You may only connect your own memories -- checked per item
# --------------------------------------------------------------------------------------


async def test_cannot_connect_a_stranger_memory_to_your_own(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """The half-owned case, which is the one a naive check lets through: the caller owns
    the memory they named first, so "does this user own something here" says yes."""
    mine = await make_item(session, bob, "bob item")
    theirs = await make_item(session, alice, "alice item")

    response = await bob_client.post(
        API, json={"source_id": str(mine.id), "target_id": str(theirs.id)}
    )

    assert response.status_code == 404
    assert await _count(session) == 0


async def test_cannot_connect_a_stranger_memory_to_your_own_the_other_way_round(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """Same request, ids swapped. A check that only looks at `source_id` passes this one."""
    mine = await make_item(session, bob, "bob item")
    theirs = await make_item(session, alice, "alice item")

    response = await bob_client.post(
        API, json={"source_id": str(theirs.id), "target_id": str(mine.id)}
    )

    assert response.status_code == 404
    assert await _count(session) == 0


async def test_cannot_connect_two_memories_that_are_both_a_strangers(
    bob_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    a = await make_item(session, alice, "alice a")
    b = await make_item(session, alice, "alice b")

    response = await bob_client.post(
        API, json={"source_id": str(a.id), "target_id": str(b.id)}
    )

    assert response.status_code == 404
    assert await _count(session) == 0


async def test_cannot_connect_to_a_memory_that_does_not_exist(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """An unknown id and a stranger's id answer identically -- the API never says which."""
    mine = await make_item(session, alice, "mine")

    response = await alice_client.post(
        API, json={"source_id": str(mine.id), "target_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# You may only read your own neighbourhoods
# --------------------------------------------------------------------------------------


async def test_cannot_read_a_stranger_neighbourhood(
    bob_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    focus = await make_item(session, alice, "alice focus")
    other = await make_item(session, alice, "alice other")
    await make_connection(session, alice, focus, other)

    response = await bob_client.get(f"{API}/for-item/{focus.id}")

    # 404 and never 403: this feature has one owner per row, so "you may not" and "there
    # is nothing" are the same fact and must read the same.
    assert response.status_code == 404


async def test_suggestions_are_scoped_to_the_caller(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    a = await make_item(session, alice, "alice a")
    b = await make_item(session, alice, "alice b")
    await make_connection(session, alice, a, b, status=ConnectionStatus.suggested)

    body = (await bob_client.get(f"{API}/suggestions")).json()

    assert body["suggestions"] == []
    assert body["total"] == 0


# --------------------------------------------------------------------------------------
# You may only change your own edges
# --------------------------------------------------------------------------------------


async def test_cannot_touch_a_stranger_edge(
    bob_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """Every mutating route, one assertion each. The tenant predicate lives in the
    statement itself, so a route that forgot it would show up here and only here."""
    a = await make_item(session, alice, "alice a")
    b = await make_item(session, alice, "alice b")
    edge = await make_connection(session, alice, a, b)

    assert (
        await bob_client.patch(f"{API}/{edge.id}", json={"relation": "contradicts"})
    ).status_code == 404
    assert (await bob_client.post(f"{API}/{edge.id}/confirm")).status_code == 404
    assert (await bob_client.post(f"{API}/{edge.id}/dismiss")).status_code == 404
    assert (await bob_client.delete(f"{API}/{edge.id}")).status_code == 404

    await session.refresh(edge)
    assert edge.relation == "related_to"
    assert edge.dismissed_at is None
    assert await _count(session) == 1


async def test_an_unknown_edge_id_reads_the_same_as_a_stranger_one(
    bob_client: AsyncClient,
) -> None:
    assert (await bob_client.delete(f"{API}/{uuid.uuid4()}")).status_code == 404


# --------------------------------------------------------------------------------------
# Anonymous
# --------------------------------------------------------------------------------------


async def test_every_route_requires_a_session(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    item = await make_item(session, alice, "alice item")
    other = await make_item(session, alice, "alice other")
    edge = await make_connection(session, alice, item, other)

    assert (await client.get(f"{API}/for-item/{item.id}")).status_code == 401
    assert (await client.get(f"{API}/suggestions")).status_code == 401
    assert (
        await client.post(
            API, json={"source_id": str(item.id), "target_id": str(other.id)}
        )
    ).status_code == 401
    assert (await client.patch(f"{API}/{edge.id}", json={"note": "x"})).status_code == 401
    assert (await client.post(f"{API}/{edge.id}/confirm")).status_code == 401
    assert (await client.post(f"{API}/{edge.id}/dismiss")).status_code == 401
    assert (await client.delete(f"{API}/{edge.id}")).status_code == 401


# --------------------------------------------------------------------------------------
# Deleting a memory takes its edges with it
# --------------------------------------------------------------------------------------


async def test_deleting_a_memory_removes_every_edge_it_touched(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """Hard, not soft, like the chunks beside them.

    An edge asserts "this memory is about the same thing as that one", which is a
    statement about content somebody asked to be rid of. It also frees the pair, so a
    deletion cannot permanently block a connection drawn later between what remains.
    """
    doomed = await make_item(session, alice, "doomed")
    left = await make_item(session, alice, "left")
    right = await make_item(session, alice, "right")
    await make_connection(session, alice, doomed, left)
    await make_connection(session, alice, right, doomed)
    survivor = await make_connection(session, alice, left, right)

    assert (await alice_client.delete(f"/api/v1/vault/{doomed.id}")).status_code == 204

    rows = (await session.exec(select(MemoryConnection))).all()
    assert [row.id for row in rows] == [survivor.id]


async def test_the_pair_is_reusable_after_a_deletion(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """The other half of removing the rows rather than tombstoning them: a dismissed edge
    is meant to block its pair forever, and a *deleted memory* is not."""
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    await make_connection(session, alice, a, b)
    await alice_client.delete(f"/api/v1/vault/{b.id}")

    revived = await make_item(session, alice, "b again")
    response = await alice_client.post(
        API, json={"source_id": str(a.id), "target_id": str(revived.id)}
    )

    assert response.status_code == 200
    assert response.json()["created"] is True


# --------------------------------------------------------------------------------------
# Cross-site writes
# --------------------------------------------------------------------------------------


async def test_a_cross_origin_write_is_refused(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """`assert_same_site` on every mutation, and on no read.

    SameSite=lax already covers the default deployment; the Origin allowlist is what still
    covers one that must set `SESSION_COOKIE_SAMESITE=none`. Worth knowing in development:
    browsing localhost while `CORS_ORIGINS` points at a tunnel gets exactly this 403 on
    writes while every read keeps working, which reads as a bug the first time.
    """
    a = await make_item(session, alice, "a")
    b = await make_item(session, alice, "b")
    evil = {"origin": "https://evil.example"}

    write = await alice_client.post(
        API, json={"source_id": str(a.id), "target_id": str(b.id)}, headers=evil
    )
    read = await alice_client.get(f"{API}/for-item/{a.id}", headers=evil)

    assert write.status_code == 403
    assert read.status_code == 200
    assert await _count(session) == 0
