"""Space use-cases: create, curate, share, and who is allowed to do which.

This is the first feature in the codebase where one user deliberately reads another's
rows, so three rules are stated here rather than left to be inferred at each call site.

**A member sees cards, not bodies.** `detail` returns whole `VaultItem` rows to
the router, which serialises them as `VaultItemRead` -- title, summary, tags, thumbnail.
`GET /vault/{id}` and the file-download route are untouched and stay owner-only, so being
in a shared Space is not a grant on the memory itself. If that ever changes it must change
in `VaultRepository`, visibly, and not by widening what this file hands back.

**You may only add your own memories.** `add_items` filters every id through
`VaultRepository.get(item_id, actor_id)`. Owning the container has never granted access to
someone else's content -- that was a real cross-tenant IDOR here once (a stranger's item
could be attached to your own collection and read back through the detail route, and
through the *public* route once the collection was shared). The check survives the rename
and now also has to survive editors: a person with write access to a Space must not be
able to pull a third party's memory into it.

**Roles are compared, not enumerated.** `_RANK` orders viewer < editor < owner, so a new
capability is one `_require` call and not a new branch in every method. Ownership is
`spaces.user_id` and is never stored as a membership row, so the two can never disagree.
"""
from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from slugify import slugify

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_invite_token, new_invite_token
from app.models.base import SpaceRole, Visibility
from app.models.space import Space, SpaceInvite
from app.models.user import User
from app.models.vault import VaultItem
from app.repositories.space import SpaceRepository
from app.repositories.vault import VaultRepository

log = get_logger("spaces")

#: Least privilege first. Comparing ranks keeps every gate to one line and makes
#: "editor or better" impossible to write as "not viewer", which is the phrasing that
#: silently admits an unknown role.
_RANK: dict[SpaceRole, int] = {
    SpaceRole.viewer: 0,
    SpaceRole.editor: 1,
    SpaceRole.owner: 2,
}

#: A batch big enough for "add all suggestions", small enough that one request cannot
#: ask Postgres for an unbounded multi-row insert.
MAX_ITEMS_PER_REQUEST = 100


class SpaceNotFound(LookupError):
    """No such Space, or the caller may not see it. The API answers 404 to both."""


class SpaceForbidden(PermissionError):
    """The caller can see the Space but not do this to it."""


@dataclass(slots=True)
class SpaceSummary:
    """A Space plus the counts a card renders, gathered without a query per row."""

    space: Space
    role: SpaceRole
    memory_count: int
    member_count: int


@dataclass(slots=True)
class AddResult:
    added: int
    skipped: int


@dataclass(slots=True)
class IssuedInvite:
    url: str
    expires_at: datetime
    role: SpaceRole


class SpaceService:
    def __init__(self, repo: SpaceRepository, vault_repo: VaultRepository) -> None:
        self.repo = repo
        self.vault_repo = vault_repo

    # ---- reading ------------------------------------------------------------

    async def list_for_user(self, user_id: uuid.UUID) -> list[SpaceSummary]:
        spaces = await self.repo.list_for_user(user_id)
        ids = [s.id for s in spaces]
        # Three queries for the whole page rather than three per card.
        counts = await self.repo.count_items_bulk(ids)
        members = await self.repo.count_members_bulk(ids)
        roles = await self.repo.roles_for_user(user_id, ids)
        return [
            SpaceSummary(
                space=s,
                role=SpaceRole.owner if s.user_id == user_id else roles.get(s.id, SpaceRole.viewer),
                memory_count=counts.get(s.id, 0),
                # +1 for the owner, who is not a `space_members` row.
                member_count=members.get(s.id, 0) + 1,
            )
            for s in spaces
        ]

    async def summary(self, space_id: uuid.UUID, user_id: uuid.UUID) -> SpaceSummary:
        """One Space with its counts, for the routes that answer with a single card."""
        space, role = await self._viewable(space_id, user_id)
        counts = await self.repo.count_items_bulk([space_id])
        members = await self.repo.count_members_bulk([space_id])
        return SpaceSummary(
            space=space,
            role=role,
            memory_count=counts.get(space_id, 0),
            member_count=members.get(space_id, 0) + 1,
        )

    async def detail(
        self, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[SpaceSummary, Sequence[VaultItem], User, Sequence[tuple[User, str]]]:
        """Everything the Space page needs, behind a single membership check.

        The check runs once here rather than once per sub-fetch: three calls that each
        re-authorise are three chances for one of them to be added later without.
        """
        space, role = await self._viewable(space_id, user_id)
        items = await self.repo.list_items(space_id)
        members = await self.repo.list_members(space_id)
        owner = await self.repo.get_owner(space_id)
        if owner is None:  # pragma: no cover - the FK makes this unreachable
            raise SpaceNotFound
        return (
            SpaceSummary(
                space=space,
                role=role,
                memory_count=len(items),
                member_count=len(members) + 1,
            ),
            items,
            owner,
            members,
        )

    async def get_public(self, slug: str) -> tuple[Space, Sequence[VaultItem]] | None:
        """The unauthenticated share page. `unlisted` is not `public`."""
        space = await self.repo.get_by_slug(slug)
        if space is None or space.visibility != Visibility.public:
            return None
        return space, await self.repo.list_items(space.id)

    async def spaces_for_item(
        self, vault_item_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[uuid.UUID]:
        return await self.repo.spaces_for_item(vault_item_id, user_id)

    async def list_members(
        self, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[User, Sequence[tuple[User, str]]]:
        """The owner and the invited members. Any role may see who else is here."""
        await self._viewable(space_id, user_id)
        owner = await self.repo.get_owner(space_id)
        if owner is None:  # pragma: no cover - the FK makes this unreachable
            raise SpaceNotFound
        return owner, await self.repo.list_members(space_id)

    # ---- writing ------------------------------------------------------------

    async def create(
        self,
        user_id: uuid.UUID,
        name: str,
        *,
        description: str | None = None,
        visibility: Visibility = Visibility.private,
        icon: str | None = None,
        emoji: str | None = None,
        accent: str | None = None,
        item_ids: Sequence[uuid.UUID] | None = None,
    ) -> tuple[Space, AddResult]:
        """Create, and optionally fill in the same request.

        Taking `item_ids` here is what makes "approve the proposal" one click and one
        round trip -- a create followed by a separate add can half-succeed, leaving an
        empty Space nobody asked for.
        """
        space = await self.repo.add(
            Space(
                user_id=user_id,
                name=name,
                slug=await self._unique_slug(name),
                description=description,
                visibility=visibility,
                icon=icon,
                emoji=emoji,
                accent=accent,
            )
        )
        result = AddResult(0, 0)
        if item_ids:
            result = await self._attach(space.id, user_id, item_ids)
        log.info("space_created", space_id=str(space.id), items=result.added)
        return space, result

    async def update(
        self,
        space_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        visibility: Visibility | None = None,
        icon: str | None = None,
        emoji: str | None = None,
        accent: str | None = None,
        pinned: bool | None = None,
    ) -> Space:
        """Explicit keyword fields, never a dict copied onto the row.

        The router's request model has no `user_id`, `slug` or `ai_overview` in it and
        this method has no way to reach them, so there is nothing an extra key in the
        JSON body could set. That is the same doctrine `UpdateContentRequest` states.
        """
        space, role = await self._viewable(space_id, user_id)
        # Visibility is the field that turns a private Space into a public web page, so
        # it needs the owner even though every other field here only needs an editor.
        _require(role, SpaceRole.owner if visibility is not None else SpaceRole.editor)

        if name is not None:
            space.name = name
        if description is not None:
            space.description = description
        if icon is not None:
            space.icon = icon or None
        if emoji is not None:
            space.emoji = emoji or None
        if accent is not None:
            space.accent = accent or None
        if pinned is not None:
            space.pinned = pinned
        if visibility is not None and visibility != space.visibility:
            space.visibility = visibility
            log.info(
                "space_visibility_changed",
                space_id=str(space_id),
                visibility=visibility.value,
            )
        space.updated_at = datetime.now(UTC)
        return await self.repo.add(space)

    async def delete(self, space_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Soft, and owner-only.

        Deliberately unlike `VaultRepository.delete`, which hard-deletes. A Space can hold
        memories other people contributed and can be someone's only route to a shared
        page; removing the rows outright is not a decision one person makes silently.
        """
        space, role = await self._viewable(space_id, user_id)
        _require(role, SpaceRole.owner)
        space.deleted_at = datetime.now(UTC)
        await self.repo.add(space)
        log.info("space_deleted", space_id=str(space_id))

    async def add_items(
        self, space_id: uuid.UUID, user_id: uuid.UUID, item_ids: Sequence[uuid.UUID]
    ) -> AddResult:
        _, role = await self._viewable(space_id, user_id)
        _require(role, SpaceRole.editor)
        return await self._attach(space_id, user_id, item_ids)

    async def remove_item(
        self, space_id: uuid.UUID, user_id: uuid.UUID, item_id: uuid.UUID
    ) -> bool:
        _, role = await self._viewable(space_id, user_id)
        _require(role, SpaceRole.editor)
        return await self.repo.remove_item(space_id, item_id)

    # ---- sharing ------------------------------------------------------------

    async def create_invite(
        self, space_id: uuid.UUID, user_id: uuid.UUID, role: SpaceRole
    ) -> IssuedInvite:
        """Mint a single-use link granting `role` in this Space.

        Owner-only, and `owner` itself is not grantable: transferring a Space is a
        different operation with different consequences (billing, deletion rights) and
        must not be reachable by choosing a value in an invite dropdown.
        """
        _, actor_role = await self._viewable(space_id, user_id)
        _require(actor_role, SpaceRole.owner)
        if role is SpaceRole.owner:
            raise SpaceForbidden("Ownership cannot be granted by invite")

        raw = new_invite_token()
        expires_at = datetime.now(UTC) + timedelta(days=settings.SPACE_INVITE_EXPIRE_DAYS)
        await self.repo.add_invite(
            SpaceInvite(
                space_id=space_id,
                role=role.value,
                token_hash=hash_invite_token(raw),
                created_by=user_id,
                expires_at=expires_at,
            )
        )
        log.info("space_invite_created", space_id=str(space_id), role=role.value)
        return IssuedInvite(
            url=f"{settings.FRONTEND_URL.rstrip('/')}/spaces/join/{raw}",
            expires_at=expires_at,
            role=role,
        )

    async def accept_invite(self, raw_token: str, user_id: uuid.UUID) -> Space:
        """Spend an invite. Every rejection is the same rejection.

        Unknown, expired and already-spent all raise `SpaceNotFound`, so whoever found a
        link in a screenshot learns nothing about what they are holding. The reason is in
        the log, where it is useful and not disclosed.

        Constant-time compare is not needed: the lookup is by digest, so the token is
        never compared against a stored secret byte by byte.
        """
        row = await self.repo.get_invite_by_hash(hash_invite_token(raw_token))
        now = datetime.now(UTC)
        if row is None:
            log.warning("space_invite_rejected", reason="unknown_token")
            raise SpaceNotFound
        if row.accepted_at is not None:
            log.warning("space_invite_rejected", reason="already_used", space_id=str(row.space_id))
            raise SpaceNotFound
        if row.expires_at <= now:
            log.warning("space_invite_rejected", reason="expired", space_id=str(row.space_id))
            raise SpaceNotFound

        space = await self.repo.get_live(row.space_id)
        if space is None:
            # The Space was deleted after the link was sent. Spend the token anyway --
            # leaving it live is a credential pointing at nothing that may point at
            # something again if the row is ever restored.
            row.accepted_at = now
            row.accepted_by = user_id
            log.warning("space_invite_rejected", reason="space_deleted")
            raise SpaceNotFound

        # Spend first, then grant. A failure between the two must not leave a token that
        # can be presented again.
        row.accepted_at = now
        row.accepted_by = user_id

        if space.user_id != user_id:
            await self.repo.add_member(
                space.id,
                user_id,
                _role_or_viewer(row.role),
                invited_by=row.created_by,
            )
        log.info("space_invite_accepted", space_id=str(space.id))
        return space

    async def remove_member(
        self, space_id: uuid.UUID, user_id: uuid.UUID, member_id: uuid.UUID
    ) -> bool:
        """Owner removes anyone; anyone removes themselves.

        Self-removal is not a privilege check that can be skipped -- a person who wants
        out of a Space should never have to ask its owner.
        """
        space, role = await self._viewable(space_id, user_id)
        if member_id != user_id:
            _require(role, SpaceRole.owner)
        if member_id == space.user_id:
            raise SpaceForbidden("The owner cannot be removed from their own Space")
        return await self.repo.remove_member(space_id, member_id)

    # ---- internals ----------------------------------------------------------

    async def _viewable(
        self, space_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[Space, SpaceRole]:
        found = await self.repo.get_for_viewer(space_id, user_id)
        if found is None:
            raise SpaceNotFound
        return found

    async def _attach(
        self, space_id: uuid.UUID, actor_id: uuid.UUID, item_ids: Sequence[uuid.UUID]
    ) -> AddResult:
        """Filter to the actor's own memories, then insert what survives.

        The ownership check is per item and not per request: a batch is exactly where a
        stranger's id is easiest to slip in among twenty of your own.
        """
        wanted = list(dict.fromkeys(item_ids))[:MAX_ITEMS_PER_REQUEST]
        owned = [i for i in wanted if await self.vault_repo.get(i, actor_id) is not None]
        if len(owned) != len(wanted):
            log.warning(
                "space_add_items_filtered",
                space_id=str(space_id),
                asked=len(wanted),
                allowed=len(owned),
            )
        added = await self.repo.add_items(space_id, owned, added_by=actor_id)
        return AddResult(added=added, skipped=len(wanted) - added)

    async def _unique_slug(self, name: str) -> str:
        base = slugify(name)[:80] or "space"
        slug = base
        while await self.repo.slug_exists(slug):
            slug = f"{base}-{secrets.token_hex(3)}"
        return slug


def _require(role: SpaceRole, needed: SpaceRole) -> None:
    if _RANK[role] < _RANK[needed]:
        raise SpaceForbidden(f"This action needs the {needed.value} role")


def _role_or_viewer(value: str) -> SpaceRole:
    try:
        return SpaceRole(value)
    except ValueError:
        return SpaceRole.viewer
