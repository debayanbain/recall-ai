"""The trash over HTTP: what each route answers, and to whom.

The service-level rules are pinned in `test_trash.py`. What these add is the part only the
real stack can show: that `GET /vault/trash` is not swallowed by `GET /vault/{item_id}`,
and that every trash route answers a stranger with 404 rather than confirming an id
exists -- the convention `test_authz.py` sets for the rest of the vault.

Needs a real PostgreSQL with pgvector; skipped otherwise, like every DB-backed test here.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from tests.conftest import make_item


async def test_a_deleted_memory_moves_to_the_trash(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    item = await make_item(session, alice, "Redis persistence")

    assert (await alice_client.delete(f"/api/v1/vault/{item.id}")).status_code == 204

    listing = await alice_client.get("/api/v1/vault")
    assert listing.json()["total"] == 0
    trash = await alice_client.get("/api/v1/vault/trash")
    assert trash.status_code == 200
    body = trash.json()
    assert [row["id"] for row in body["items"]] == [str(item.id)]
    assert body["retention_days"] >= 1
    # The two dates the page is about: when it went, and when it stops being recoverable.
    assert body["items"][0]["deleted_at"] is not None
    assert body["items"][0]["purge_after"] > body["items"][0]["deleted_at"]


async def test_the_trash_path_is_not_read_as_an_item_id(
    alice_client: AsyncClient,
) -> None:
    """Declared above `/{item_id}`. Served as a listing, never as a lookup that 404s."""
    response = await alice_client.get("/api/v1/vault/trash")

    assert response.status_code == 200
    assert "items" in response.json()


async def test_restore_puts_it_back_in_the_vault(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    item = await make_item(session, alice, "Redis persistence")
    await alice_client.delete(f"/api/v1/vault/{item.id}")

    restored = await alice_client.post(f"/api/v1/vault/{item.id}/restore")

    assert restored.status_code == 200
    assert restored.json()["title"] == "Redis persistence"
    listing = await alice_client.get("/api/v1/vault")
    assert [row["id"] for row in listing.json()["items"]] == [str(item.id)]
    assert (await alice_client.get("/api/v1/vault/trash")).json()["total"] == 0


async def test_another_user_cannot_see_or_restore_your_trash(
    alice_client: AsyncClient, bob_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """404, not 403: the API never confirms that an id exists."""
    item = await make_item(session, alice, "Redis persistence")
    await alice_client.delete(f"/api/v1/vault/{item.id}")

    assert (await bob_client.get("/api/v1/vault/trash")).json()["total"] == 0
    assert (await bob_client.post(f"/api/v1/vault/{item.id}/restore")).status_code == 404
    assert (
        await bob_client.delete(f"/api/v1/vault/{item.id}/permanent")
    ).status_code == 404
    # And it is still alice's to restore afterwards.
    assert (await alice_client.post(f"/api/v1/vault/{item.id}/restore")).status_code == 200


async def test_permanent_delete_needs_the_memory_to_be_in_the_trash_already(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """"Delete forever" is a second, deliberate action -- not a one-call destroy."""
    item = await make_item(session, alice, "Redis persistence")

    assert (
        await alice_client.delete(f"/api/v1/vault/{item.id}/permanent")
    ).status_code == 404
    assert (await alice_client.get("/api/v1/vault")).json()["total"] == 1


async def test_permanent_delete_empties_one_memory(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    item = await make_item(session, alice, "Redis persistence")
    await alice_client.delete(f"/api/v1/vault/{item.id}")

    assert (
        await alice_client.delete(f"/api/v1/vault/{item.id}/permanent")
    ).status_code == 204

    assert (await alice_client.get("/api/v1/vault/trash")).json()["total"] == 0
    # A purged row is not restorable, and says so the same way a missing one does.
    assert (await alice_client.post(f"/api/v1/vault/{item.id}/restore")).status_code == 404


async def test_empty_trash_reports_what_it_destroyed(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    first = await make_item(session, alice, "Redis persistence")
    second = await make_item(session, alice, "Celery prefork")
    await alice_client.delete(f"/api/v1/vault/{first.id}")
    await alice_client.delete(f"/api/v1/vault/{second.id}")

    emptied = await alice_client.delete("/api/v1/vault/trash")

    assert emptied.status_code == 200
    assert emptied.json()["purged"] == 2
    assert (await alice_client.get("/api/v1/vault/trash")).json()["total"] == 0


async def test_the_trash_routes_require_a_session(client: AsyncClient) -> None:
    """Same rule as the rest of the vault: no cookie, no answer."""
    item_id = uuid.uuid4()

    assert (await client.get("/api/v1/vault/trash")).status_code == 401
    assert (await client.delete("/api/v1/vault/trash")).status_code == 401
    assert (await client.post(f"/api/v1/vault/{item_id}/restore")).status_code == 401
    assert (await client.delete(f"/api/v1/vault/{item_id}/permanent")).status_code == 401
