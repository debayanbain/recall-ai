"""Tenant isolation: one user must never reach another user's data.

Planner Phase 1 lists "cross-user data access rejection" as a required test. Every case
here drives the real HTTP stack with a real signed session cookie, so it covers the
route, the dependency, the service and the repository filter together.

Convention under test: a resource owned by someone else answers 404, not 403, so the
API never confirms that an id exists.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import Visibility
from app.models.user import User
from app.models.vault import VaultItem
from tests.conftest import make_collection, make_item

# --------------------------------------------------------------------------------------
# Unauthenticated access
# --------------------------------------------------------------------------------------

PROTECTED: list[tuple[str, str]] = [
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/vault"),
    ("GET", f"/api/v1/vault/{uuid.uuid4()}"),
    ("DELETE", f"/api/v1/vault/{uuid.uuid4()}"),
    ("GET", "/api/v1/search?q=anything"),
    ("GET", "/api/v1/collections"),
    ("POST", "/api/v1/collections"),
    ("GET", f"/api/v1/collections/{uuid.uuid4()}"),
]


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_no_cookie_is_rejected(client: AsyncClient, method: str, path: str) -> None:
    response = await client.request(method, path, json={})
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path"), PROTECTED)
async def test_forged_cookie_is_rejected(
    client: AsyncClient, method: str, path: str
) -> None:
    """A token this server did not sign must not authenticate anyone."""
    client.cookies.set("recall_session", "not.a.valid.jwt")
    response = await client.request(method, path, json={})
    assert response.status_code == 401


async def test_token_for_deleted_user_is_rejected(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """A validly signed token whose subject no longer exists must not authenticate."""
    from tests.conftest import authenticate

    authenticate(client, alice)
    await session.delete(alice)
    await session.commit()

    assert (await client.get("/api/v1/auth/me")).status_code == 401


# --------------------------------------------------------------------------------------
# Vault items
# --------------------------------------------------------------------------------------


async def test_cannot_read_another_users_item(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    item = await make_item(session, alice, "alice private note")

    response = await bob_client.get(f"/api/v1/vault/{item.id}")

    assert response.status_code == 404
    assert "alice private note" not in response.text


async def test_cannot_delete_another_users_item(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    item = await make_item(session, alice, "alice keeps this")

    response = await bob_client.delete(f"/api/v1/vault/{item.id}")

    assert response.status_code == 404
    assert await session.get(VaultItem, item.id) is not None, "item was destroyed"


async def test_list_excludes_other_users_items(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    await make_item(session, alice, "alice item")
    mine = await make_item(session, bob, "bob item")

    body = (await bob_client.get("/api/v1/vault")).json()

    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [str(mine.id)]


async def test_search_excludes_other_users_items(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    await make_item(session, alice, "quarterly revenue plan")

    body = (await bob_client.get("/api/v1/search?q=quarterly")).json()

    assert body["total"] == 0
    assert body["items"] == []


async def test_owner_can_read_own_item(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """The negative cases above must fail for the right reason, not because reads break."""
    item = await make_item(session, alice, "alice own note")

    response = await alice_client.get(f"/api/v1/vault/{item.id}")

    assert response.status_code == 200
    assert response.json()["title"] == "alice own note"


# --------------------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------------------


async def test_cannot_read_another_users_collection(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    collection = await make_collection(session, alice, "Alice Research")

    assert (await bob_client.get(f"/api/v1/collections/{collection.id}")).status_code == 404


async def test_collection_list_excludes_other_users(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    await make_collection(session, alice, "Alice Space")
    mine = await make_collection(session, bob, "Bob Space")

    body = (await bob_client.get("/api/v1/collections")).json()

    assert [c["id"] for c in body] == [str(mine.id)]


async def test_cannot_add_item_to_another_users_collection(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    collection = await make_collection(session, alice, "Alice Space")
    mine = await make_item(session, bob, "bob item")

    response = await bob_client.post(
        f"/api/v1/collections/{collection.id}/items",
        json={"vault_item_id": str(mine.id)},
    )

    assert response.status_code == 404


async def test_cannot_add_another_users_item_to_own_collection(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """The collection is Bob's, but the item is Alice's.

    Ownership of the container must not grant ownership of what is put inside it --
    otherwise a stranger's item becomes readable through the collection detail route.
    """
    stolen = await make_item(session, alice, "alice confidential")
    mine = await make_collection(session, bob, "Bob Space")

    response = await bob_client.post(
        f"/api/v1/collections/{mine.id}/items",
        json={"vault_item_id": str(stolen.id)},
    )

    assert response.status_code == 404

    detail = await bob_client.get(f"/api/v1/collections/{mine.id}")
    assert "alice confidential" not in detail.text


# --------------------------------------------------------------------------------------
# Public sharing
# --------------------------------------------------------------------------------------


async def test_private_collection_is_not_public(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    collection = await make_collection(session, alice, "Alice Private", Visibility.private)

    assert (await client.get(f"/api/v1/public/{collection.slug}")).status_code == 404


async def test_unlisted_collection_is_not_public(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    collection = await make_collection(session, alice, "Alice Unlisted", Visibility.unlisted)

    assert (await client.get(f"/api/v1/public/{collection.slug}")).status_code == 404


async def test_public_collection_is_readable_without_auth(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    collection = await make_collection(session, alice, "Alice Public", Visibility.public)

    response = await client.get(f"/api/v1/public/{collection.slug}")

    assert response.status_code == 200
    assert response.json()["name"] == "Alice Public"


async def test_public_collection_never_exposes_another_users_item(
    client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """Worst case of the container/content split: a public collection makes any item
    inside it world-readable, so a foreign item must never be able to land in one."""
    from tests.conftest import authenticate

    stolen = await make_item(session, alice, "alice confidential")
    shared = await make_collection(session, bob, "Bob Public", Visibility.public)

    authenticate(client, bob)
    await client.post(
        f"/api/v1/collections/{shared.id}/items",
        json={"vault_item_id": str(stolen.id)},
    )
    client.cookies.clear()

    response = await client.get(f"/api/v1/public/{shared.slug}")

    assert "alice confidential" not in response.text
