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

from app.models.user import User
from app.models.vault import VaultItem
from tests.conftest import make_item

# --------------------------------------------------------------------------------------
# Unauthenticated access
# --------------------------------------------------------------------------------------

PROTECTED: list[tuple[str, str]] = [
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/vault"),
    ("GET", f"/api/v1/vault/{uuid.uuid4()}"),
    ("DELETE", f"/api/v1/vault/{uuid.uuid4()}"),
    ("GET", "/api/v1/search?q=anything"),
    ("GET", "/api/v1/spaces"),
    ("POST", "/api/v1/spaces"),
    ("GET", f"/api/v1/spaces/{uuid.uuid4()}"),
    ("PATCH", f"/api/v1/spaces/{uuid.uuid4()}"),
    ("DELETE", f"/api/v1/spaces/{uuid.uuid4()}"),
    ("POST", f"/api/v1/spaces/{uuid.uuid4()}/items"),
    ("GET", f"/api/v1/spaces/{uuid.uuid4()}/members"),
    ("POST", f"/api/v1/spaces/{uuid.uuid4()}/invites"),
    ("POST", "/api/v1/spaces/invites/anything/accept"),
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
