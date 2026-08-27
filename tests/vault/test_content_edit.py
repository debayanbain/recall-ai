"""PATCH /vault/{id}/content — the manual editor's save path, over the real HTTP stack.

Needs Postgres, so these skip with the rest of the DB suite. The sanitizing rules
themselves are pinned offline in `test_editor_doc.py`.
"""
from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User
from app.models.vault import VaultItem
from tests.conftest import make_item

BODY = {"blocks": [{"type": "paragraph", "data": {"text": "rewritten by hand"}}]}
SURVIVOR = "a sentence that will survive the edit intact"


async def test_owner_overwrites_their_own_content(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    item = await make_item(session, alice, "alice note")

    response = await alice_client.patch(f"/api/v1/vault/{item.id}/content", json=BODY)

    assert response.status_code == 200
    assert response.json()["content"] == "rewritten by hand"
    await session.refresh(item)
    assert item.content == "rewritten by hand"
    assert item.item_metadata["editor_doc"]["blocks"][0]["data"]["text"] == "rewritten by hand"


async def test_cannot_edit_another_users_item(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """404, not 403 — the API must not confirm that someone else's id is real."""
    item = await make_item(session, alice, "alice keeps this")

    response = await bob_client.patch(f"/api/v1/vault/{item.id}/content", json=BODY)

    assert response.status_code == 404
    await session.refresh(item)
    assert item.content == "body of alice keeps this", "another user rewrote this"


async def test_unknown_item_is_404(alice_client: AsyncClient, alice: User) -> None:
    response = await alice_client.patch(f"/api/v1/vault/{uuid.uuid4()}/content", json=BODY)
    assert response.status_code == 404


async def test_no_cookie_is_rejected(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    item = await make_item(session, alice, "alice note")
    assert (await client.patch(f"/api/v1/vault/{item.id}/content", json=BODY)).status_code == 401


async def test_extra_fields_cannot_reach_other_columns(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """Overposting: only `blocks` is read, so nothing else in the body can be written."""
    item = await make_item(session, alice, "alice note")
    other = await make_item(session, alice, "decoy")

    response = await alice_client.patch(
        f"/api/v1/vault/{item.id}/content",
        json={
            **BODY,
            "user_id": str(uuid.uuid4()),
            "id": str(other.id),
            "ai_category": "hijacked",
            "processing_status": "failed",
            "summary": "hijacked",
        },
    )

    assert response.status_code == 200
    await session.refresh(item)
    assert item.user_id == alice.id
    assert item.ai_category is None
    assert item.summary == "summary of alice note"
    assert item.processing_status.value == "completed"
    await session.refresh(other)
    assert other.content == "body of decoy"


async def test_stale_highlights_are_dropped(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """A highlight is a verbatim quote of `content`; after a rewrite some no longer are."""
    item = await make_item(session, alice, "alice note")
    item.ai_highlights = [
        SURVIVOR,
        "a sentence the user is about to delete entirely",
    ]
    session.add(item)
    await session.commit()

    response = await alice_client.patch(
        f"/api/v1/vault/{item.id}/content",
        json={
            "blocks": [
                {"type": "paragraph", "data": {"text": SURVIVOR}}
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["ai_highlights"] == [SURVIVOR]


async def test_empty_document_is_refused(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    item = await make_item(session, alice, "alice note")

    response = await alice_client.patch(
        f"/api/v1/vault/{item.id}/content", json={"blocks": [{"type": "paragraph", "data": {}}]}
    )

    assert response.status_code == 422
    assert await session.get(VaultItem, item.id) is not None
    await session.refresh(item)
    assert item.content == "body of alice note", "an empty save wiped the body"
