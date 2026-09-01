"""Space data access.

Two things here are load-bearing and neither is obvious from the method names.

**`get_for_viewer` is the only way in.** It replaces the owner-only `get()` this file used
to have, and it returns the caller's *role* alongside the row. Every route calls it, so
there is exactly one query in the codebase that decides whether a person may see a Space,
and adding a route cannot accidentally skip the check by forgetting a `user_id` argument
-- the signature has nowhere to put one that would be ignored.

**Membership of a memory is deduplicated in Postgres, not in Python.** `add_items` uses
`ON CONFLICT DO NOTHING` against the composite primary key. The previous implementation
issued a bare INSERT, so adding a memory that was already in the Space raised a
`UniqueViolation` and the request 500'd -- and the batch paths this feature adds ("add all
suggestions", "add to Space" over a selection that overlaps) hit that case as the *normal*
outcome rather than the edge one.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, func, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import SpaceRole
from app.models.space import Space, SpaceInvite, SpaceItem, SpaceMember
from app.models.user import User
from app.models.vault import VaultItem


class SpaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- spaces -------------------------------------------------------------

    async def add(self, space: Space) -> Space:
        self.session.add(space)
        await self.session.flush()
        await self.session.refresh(space)
        return space

    async def get_for_viewer(
        self, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[Space, SpaceRole] | None:
        """The Space and the caller's effective role, or None if they may not see it.

        Owner beats membership: `spaces.user_id` is the single source of truth for
        ownership, so a stray `space_members` row claiming otherwise cannot demote the
        person who created it.
        """
        result = await self.session.exec(
            select(Space, SpaceMember.role)
            .join(
                SpaceMember,
                (col(SpaceMember.space_id) == col(Space.id))
                & (col(SpaceMember.user_id) == user_id),
                isouter=True,
            )
            .where(col(Space.id) == space_id, col(Space.deleted_at).is_(None))
        )
        row = result.first()
        if row is None:
            return None
        space, member_role = row
        if space.user_id == user_id:
            return space, SpaceRole.owner
        if member_role is None:
            return None
        return space, _role_or_viewer(member_role)

    async def get_live(self, space_id: uuid.UUID) -> Space | None:
        """The row by id, with no viewer check -- the invite-accept path only.

        Deliberately narrow and deliberately named: acceptance has to load a Space the
        caller is by definition not yet a member of, and that is the one legitimate
        reason to skip `get_for_viewer`. Anything else must go through the viewer check.
        """
        space = await self.session.get(Space, space_id)
        if space is None or space.deleted_at is not None:
            return None
        return space

    async def get_owner(self, space_id: uuid.UUID) -> User | None:
        result = await self.session.exec(
            select(User)
            .join(Space, col(Space.user_id) == col(User.id))
            .where(col(Space.id) == space_id)
        )
        return result.first()

    async def get_by_slug(self, slug: str) -> Space | None:
        """Unscoped by design -- this backs the unauthenticated share page.

        The `deleted_at` filter is the whole safety of it: without it, deleting a Space
        left its public URL serving the same contents forever.
        """
        result = await self.session.exec(
            select(Space).where(Space.slug == slug, col(Space.deleted_at).is_(None))
        )
        return result.first()

    async def slug_exists(self, slug: str) -> bool:
        result = await self.session.exec(select(Space.id).where(Space.slug == slug))
        return result.first() is not None

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[Space]:
        """Spaces this person owns or has been invited into, newest first."""
        result = await self.session.exec(
            select(Space)
            .join(
                SpaceMember,
                (col(SpaceMember.space_id) == col(Space.id))
                & (col(SpaceMember.user_id) == user_id),
                isouter=True,
            )
            .where(
                col(Space.deleted_at).is_(None),
                or_(Space.user_id == user_id, col(SpaceMember.user_id).is_not(None)),
            )
            .order_by(col(Space.pinned).desc(), col(Space.created_at).desc())
        )
        return result.all()

    async def roles_for_user(
        self, user_id: uuid.UUID, space_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, SpaceRole]:
        """Membership roles for a batch of Spaces, so the listing is two queries not N."""
        if not space_ids:
            return {}
        result = await self.session.exec(
            select(SpaceMember.space_id, SpaceMember.role).where(
                SpaceMember.user_id == user_id,
                col(SpaceMember.space_id).in_(list(space_ids)),
            )
        )
        return {sid: _role_or_viewer(role) for sid, role in result.all()}

    # ---- items --------------------------------------------------------------

    async def add_items(
        self,
        space_id: uuid.UUID,
        vault_item_ids: Sequence[uuid.UUID],
        added_by: uuid.UUID,
    ) -> int:
        """Attach memories, ignoring the ones already attached. Returns how many landed.

        `ON CONFLICT DO NOTHING` on the composite PK, so this is idempotent: the caller
        may hand it a selection that overlaps the Space and gets a truthful count back
        rather than an integrity error.
        """
        if not vault_item_ids:
            return 0
        statement = (
            pg_insert(SpaceItem)
            .values(
                [
                    {
                        "space_id": space_id,
                        "vault_item_id": item_id,
                        "added_by": added_by,
                    }
                    for item_id in dict.fromkeys(vault_item_ids)
                ]
            )
            .on_conflict_do_nothing(index_elements=["space_id", "vault_item_id"])
        )
        result = await self.session.exec(statement)
        return int(result.rowcount or 0)

    async def remove_item(self, space_id: uuid.UUID, vault_item_id: uuid.UUID) -> bool:
        row = await self.session.get(SpaceItem, (space_id, vault_item_id))
        if row is None:
            return False
        await self.session.delete(row)
        return True

    async def list_items(self, space_id: uuid.UUID) -> Sequence[VaultItem]:
        """The Space's memories, newest addition first.

        Filters `deleted_at`: without it a memory its owner deleted stayed visible inside
        the Space *and* on the unauthenticated share page, which is the worst place for a
        deletion not to take effect.
        """
        result = await self.session.exec(
            select(VaultItem)
            .join(SpaceItem, col(SpaceItem.vault_item_id) == col(VaultItem.id))
            .where(
                SpaceItem.space_id == space_id,
                col(VaultItem.deleted_at).is_(None),
            )
            .order_by(col(SpaceItem.added_at).desc())
        )
        return result.all()

    async def list_item_ids(self, space_id: uuid.UUID) -> list[uuid.UUID]:
        """Just the ids -- what the vector paths (suggestions, connections, ask) need."""
        result = await self.session.exec(
            select(SpaceItem.vault_item_id)
            .join(VaultItem, col(SpaceItem.vault_item_id) == col(VaultItem.id))
            .where(
                SpaceItem.space_id == space_id,
                col(VaultItem.deleted_at).is_(None),
            )
        )
        return list(result.all())

    async def count_items_bulk(self, space_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        """One grouped COUNT for a whole page of Space cards."""
        if not space_ids:
            return {}
        result = await self.session.exec(
            select(SpaceItem.space_id, func.count())
            .join(VaultItem, col(SpaceItem.vault_item_id) == col(VaultItem.id))
            .where(
                col(SpaceItem.space_id).in_(list(space_ids)),
                col(VaultItem.deleted_at).is_(None),
            )
            .group_by(col(SpaceItem.space_id))
        )
        return {sid: int(count) for sid, count in result.all()}

    async def spaces_for_item(
        self, vault_item_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Which of the caller's Spaces already contain this memory.

        Powers the checkmarks in the card's "+" menu. Scoped through the same
        owner-or-member predicate as `list_for_user`, so it cannot be used to probe
        whether a stranger filed something.
        """
        result = await self.session.exec(
            select(SpaceItem.space_id)
            .join(Space, col(Space.id) == col(SpaceItem.space_id))
            .join(
                SpaceMember,
                (col(SpaceMember.space_id) == col(Space.id))
                & (col(SpaceMember.user_id) == user_id),
                isouter=True,
            )
            .where(
                SpaceItem.vault_item_id == vault_item_id,
                col(Space.deleted_at).is_(None),
                or_(Space.user_id == user_id, col(SpaceMember.user_id).is_not(None)),
            )
        )
        return list(result.all())

    # ---- members ------------------------------------------------------------

    async def list_members(self, space_id: uuid.UUID) -> Sequence[tuple[User, str]]:
        result = await self.session.exec(
            select(User, SpaceMember.role)
            .join(SpaceMember, col(SpaceMember.user_id) == col(User.id))
            .where(SpaceMember.space_id == space_id)
            .order_by(col(SpaceMember.created_at).asc())
        )
        return result.all()

    async def get_member(
        self, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> SpaceMember | None:
        return await self.session.get(SpaceMember, (space_id, user_id))

    async def add_member(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
        role: SpaceRole,
        invited_by: uuid.UUID | None = None,
    ) -> None:
        """Upsert, so accepting a second invite updates the role instead of colliding."""
        statement = (
            pg_insert(SpaceMember)
            .values(
                space_id=space_id,
                user_id=user_id,
                role=role.value,
                invited_by=invited_by,
            )
            .on_conflict_do_update(
                index_elements=["space_id", "user_id"], set_={"role": role.value}
            )
        )
        await self.session.exec(statement)

    async def remove_member(self, space_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        row = await self.session.get(SpaceMember, (space_id, user_id))
        if row is None:
            return False
        await self.session.delete(row)
        return True

    async def count_members_bulk(self, space_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not space_ids:
            return {}
        result = await self.session.exec(
            select(SpaceMember.space_id, func.count())
            .where(col(SpaceMember.space_id).in_(list(space_ids)))
            .group_by(col(SpaceMember.space_id))
        )
        return {sid: int(count) for sid, count in result.all()}

    # ---- invites ------------------------------------------------------------

    async def add_invite(self, invite: SpaceInvite) -> SpaceInvite:
        self.session.add(invite)
        await self.session.flush()
        await self.session.refresh(invite)
        return invite

    async def get_invite_by_hash(self, token_hash: str) -> SpaceInvite | None:
        result = await self.session.exec(
            select(SpaceInvite).where(SpaceInvite.token_hash == token_hash)
        )
        return result.first()


def _role_or_viewer(value: str) -> SpaceRole:
    """A role the database does not recognise reads as the least privilege, never the most.

    The column is Text so a new role never needs an ALTER TYPE; the cost of that is that
    a typo or a downgrade could leave a value this build has never heard of. Failing to
    `viewer` means the unknown case loses write access rather than gaining it.
    """
    try:
        return SpaceRole(value)
    except ValueError:
        return SpaceRole.viewer
