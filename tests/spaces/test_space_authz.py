"""Where sharing stops.

A Space is the one place a person deliberately reads someone else's rows, so these tests
pin the edges of that rather than the happy path:

* a non-member sees nothing, and cannot tell the Space exists
* a member sees the *card* of another member's memory and never its body
* an editor may add their own memories and never a stranger's
* a viewer may not write, and only the owner may share, invite or delete
* an invite works exactly once, and every rejection reads identically
"""
from __future__ import annotations

from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import SpaceRole, Visibility
from app.models.space import Space, SpaceItem
from app.models.user import User
from app.models.vault import VaultItem
from tests.conftest import (
    authenticate,
    make_item,
    make_member,
    make_space,
    make_user,
)

# --------------------------------------------------------------------------------------
# Non-members
# --------------------------------------------------------------------------------------


async def test_non_member_cannot_read_a_space(
    bob_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    space = await make_space(session, alice, "Alice Research")

    assert (await bob_client.get(f"/api/v1/spaces/{space.id}")).status_code == 404


async def test_listing_excludes_spaces_you_are_not_in(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    await make_space(session, alice, "Alice Space")
    mine = await make_space(session, bob, "Bob Space")

    body = (await bob_client.get("/api/v1/spaces")).json()

    assert [s["id"] for s in body] == [str(mine.id)]


async def test_non_member_cannot_add_to_a_space(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    space = await make_space(session, alice, "Alice Space")
    mine = await make_item(session, bob, "bob item")

    response = await bob_client.post(
        f"/api/v1/spaces/{space.id}/items", json={"item_ids": [str(mine.id)]}
    )

    # 404 rather than 403: a stranger must not learn that the id is real.
    assert response.status_code == 404


# --------------------------------------------------------------------------------------
# Members: what sharing does and does not grant
# --------------------------------------------------------------------------------------


async def test_member_sees_the_card_of_another_members_memory(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    space = await make_space(session, alice, "Shared")
    await make_member(session, space, bob, SpaceRole.viewer)
    item = await make_item(session, alice, "alice shared note")
    await _add_as(session, space, alice, item)

    body = (await bob_client.get(f"/api/v1/spaces/{space.id}")).json()

    assert [i["title"] for i in body["items"]] == ["alice shared note"]
    assert body["role"] == "viewer"


async def test_member_does_not_get_the_body_of_another_members_memory(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """The line this whole feature is built against.

    Being in a shared Space grants the card -- title, summary, tags -- and never the
    memory itself. `content`, `ai_highlights`, `item_metadata` and the stored file stay
    with the owner, so `GET /vault/{id}` still answers 404 to everyone else.
    """
    space = await make_space(session, alice, "Shared")
    await make_member(session, space, bob, SpaceRole.editor)
    item = await make_item(session, alice, "alice shared note")
    await _add_as(session, space, alice, item)

    listed = await bob_client.get(f"/api/v1/spaces/{space.id}")
    assert "content" not in listed.json()["items"][0]
    assert "body of alice shared note" not in listed.text

    assert (await bob_client.get(f"/api/v1/vault/{item.id}")).status_code == 404


async def test_editor_cannot_add_a_strangers_memory(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """Write access to a Space is not read access to the vault.

    This was a real cross-tenant IDOR once, when the Space was called a Collection and
    only the container's owner was checked. Editors make it worse, not better: a person
    who may write to a Space someone else owns must still not be able to pull a third
    party's memory into it.
    """
    carol = await make_user(session, "carol@example.com")
    secret = await make_item(session, carol, "carol confidential")
    space = await make_space(session, alice, "Shared")
    await make_member(session, space, bob, SpaceRole.editor)

    response = await bob_client.post(
        f"/api/v1/spaces/{space.id}/items", json={"item_ids": [str(secret.id)]}
    )

    assert response.status_code == 200
    assert response.json() == {"added": 0, "skipped": 1}
    assert "carol confidential" not in (await bob_client.get(f"/api/v1/spaces/{space.id}")).text


async def test_viewer_cannot_write(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    space = await make_space(session, alice, "Shared")
    await make_member(session, space, bob, SpaceRole.viewer)
    mine = await make_item(session, bob, "bob item")

    response = await bob_client.post(
        f"/api/v1/spaces/{space.id}/items", json={"item_ids": [str(mine.id)]}
    )

    # 403, not 404: Bob can see this Space, so naming the missing role discloses nothing
    # he did not already know and tells him what to ask for.
    assert response.status_code == 403


async def test_editor_cannot_change_visibility_or_delete(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """Publishing and deleting are owner-only even for someone who may curate.

    Visibility is the field that turns a private Space into a public web page; an editor
    flipping it publishes the owner's memories on the owner's behalf.
    """
    space = await make_space(session, alice, "Shared")
    await make_member(session, space, bob, SpaceRole.editor)

    assert (
        await bob_client.patch(
            f"/api/v1/spaces/{space.id}", json={"visibility": "public"}
        )
    ).status_code == 403
    assert (await bob_client.delete(f"/api/v1/spaces/{space.id}")).status_code == 403
    assert (
        await bob_client.post(f"/api/v1/spaces/{space.id}/invites", json={"role": "viewer"})
    ).status_code == 403


async def test_editor_may_rename(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """The negative cases above must fail for the right reason, not because writes break."""
    space = await make_space(session, alice, "Shared")
    await make_member(session, space, bob, SpaceRole.editor)

    response = await bob_client.patch(f"/api/v1/spaces/{space.id}", json={"name": "Renamed"})

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"


async def test_member_may_remove_themselves_but_not_others(
    bob_client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """Leaving must never require asking the owner."""
    carol = await make_user(session, "carol2@example.com")
    space = await make_space(session, alice, "Shared")
    await make_member(session, space, bob, SpaceRole.viewer)
    await make_member(session, space, carol, SpaceRole.viewer)

    assert (
        await bob_client.delete(f"/api/v1/spaces/{space.id}/members/{carol.id}")
    ).status_code == 403
    assert (
        await bob_client.delete(f"/api/v1/spaces/{space.id}/members/{bob.id}")
    ).status_code == 204


async def test_owner_cannot_be_removed_from_their_own_space(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    space = await make_space(session, alice, "Mine")

    response = await alice_client.delete(f"/api/v1/spaces/{space.id}/members/{alice.id}")

    assert response.status_code == 403


# --------------------------------------------------------------------------------------
# Invites
# --------------------------------------------------------------------------------------


async def test_invite_works_exactly_once(
    client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """One client, re-authenticated between actors.

    `alice_client` and `bob_client` are the *same* AsyncClient -- both fixtures set a
    cookie on the shared `client` -- so a test that needs two people has to switch
    identities explicitly rather than hold two handles.
    """
    space = await make_space(session, alice, "Shared")

    authenticate(client, alice)
    issued = await client.post(f"/api/v1/spaces/{space.id}/invites", json={"role": "editor"})
    assert issued.status_code == 201
    token = issued.json()["url"].rsplit("/", 1)[-1]

    authenticate(client, bob)
    joined = await client.post(f"/api/v1/spaces/invites/{token}/accept")
    assert joined.status_code == 200
    assert joined.json()["role"] == "editor"
    assert (await client.get(f"/api/v1/spaces/{space.id}")).status_code == 200

    # Replay: the same opaque rejection an unknown token gets.
    assert (await client.post(f"/api/v1/spaces/invites/{token}/accept")).status_code == 404


async def test_unknown_and_replayed_invites_are_indistinguishable(
    client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """Same status and same body, so a link found in a screenshot reveals nothing."""
    space = await make_space(session, alice, "Shared")

    authenticate(client, alice)
    issued = await client.post(f"/api/v1/spaces/{space.id}/invites", json={"role": "viewer"})
    token = issued.json()["url"].rsplit("/", 1)[-1]

    authenticate(client, bob)
    await client.post(f"/api/v1/spaces/invites/{token}/accept")

    replayed = await client.post(f"/api/v1/spaces/invites/{token}/accept")
    unknown = await client.post("/api/v1/spaces/invites/not-a-real-token/accept")

    assert replayed.status_code == unknown.status_code == 404
    assert replayed.json() == unknown.json()


async def test_ownership_cannot_be_granted_by_invite(
    alice_client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    space = await make_space(session, alice, "Shared")

    response = await alice_client.post(
        f"/api/v1/spaces/{space.id}/invites", json={"role": "owner"}
    )

    assert response.status_code == 403


# --------------------------------------------------------------------------------------
# Public sharing
# --------------------------------------------------------------------------------------


async def test_private_space_is_not_public(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    space = await make_space(session, alice, "Alice Private", Visibility.private)

    assert (await client.get(f"/api/v1/public/{space.slug}")).status_code == 404


async def test_unlisted_space_is_not_public(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    space = await make_space(session, alice, "Alice Unlisted", Visibility.unlisted)

    assert (await client.get(f"/api/v1/public/{space.slug}")).status_code == 404


async def test_public_space_is_readable_without_auth(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    space = await make_space(session, alice, "Alice Public", Visibility.public)

    response = await client.get(f"/api/v1/public/{space.slug}")

    assert response.status_code == 200
    assert response.json()["name"] == "Alice Public"


async def test_deleted_space_stops_being_public(
    alice_client: AsyncClient, client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """Soft delete has to reach the share page, or a "deleted" Space stays on the web."""
    space = await make_space(session, alice, "Alice Public", Visibility.public)
    assert (await client.get(f"/api/v1/public/{space.slug}")).status_code == 200

    assert (await alice_client.delete(f"/api/v1/spaces/{space.id}")).status_code == 204

    assert (await client.get(f"/api/v1/public/{space.slug}")).status_code == 404


async def test_deleted_memory_disappears_from_a_public_space(
    alice_client: AsyncClient, client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    """The worst place for a deletion not to take effect.

    A memory removed from the vault must not keep being served to the whole internet
    because it happens to sit in a shared Space.
    """
    space = await make_space(session, alice, "Alice Public", Visibility.public)
    item = await make_item(session, alice, "regrettable note")
    await alice_client.post(f"/api/v1/spaces/{space.id}/items", json={"item_ids": [str(item.id)]})
    assert "regrettable note" in (await client.get(f"/api/v1/public/{space.slug}")).text

    await alice_client.delete(f"/api/v1/vault/{item.id}")

    assert "regrettable note" not in (await client.get(f"/api/v1/public/{space.slug}")).text


async def test_public_space_never_exposes_a_strangers_memory(
    client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """Worst case of the container/content split: a public Space makes anything inside it
    world-readable, so a foreign memory must never be able to land in one."""
    stolen = await make_item(session, alice, "alice confidential")
    shared = await make_space(session, bob, "Bob Public", Visibility.public)

    authenticate(client, bob)
    await client.post(f"/api/v1/spaces/{shared.id}/items", json={"item_ids": [str(stolen.id)]})
    client.cookies.clear()

    response = await client.get(f"/api/v1/public/{shared.slug}")

    assert "alice confidential" not in response.text


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


async def _add_as(
    session: AsyncSession, space: Space, owner: User, item: VaultItem
) -> None:
    """Attach a memory directly, so a test about *reading* does not depend on writing."""
    session.add(SpaceItem(space_id=space.id, vault_item_id=item.id, added_by=owner.id))
    await session.commit()


async def test_for_item_lists_only_spaces_you_can_see(
    client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """The card's "+" menu must not become a probe for what a stranger filed."""
    item = await make_item(session, bob, "bob note")
    mine = await make_space(session, bob, "Bob Space")
    theirs = await make_space(session, alice, "Alice Space")
    await _add_as(session, mine, bob, item)
    await _add_as(session, theirs, alice, item)

    authenticate(client, bob)
    body = (await client.get(f"/api/v1/spaces/for-item/{item.id}")).json()

    assert body == [str(mine.id)]
