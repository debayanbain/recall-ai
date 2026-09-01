"""Space routes: create, curate, share, invite.

`404` covers both "no such Space" and "not yours", matching every other router here -- the
API never confirms an id exists to someone who may not see it. `403` is reserved for the
case where the caller *can* see the Space and simply may not do this to it, which is not a
disclosure: they already know it is there. Both are produced by the exception handlers
registered in `app/main.py`, so a route that forgets to catch cannot leak a stack trace.

Every mutating route carries `assert_same_site`. The session cookie is `SameSite=lax` in
the default deployment, which already blocks a cross-site POST, but a deployment whose SPA
and API are genuinely cross-site has to set `SESSION_COOKIE_SAMESITE=none`, and that hands
the browser back to whatever page issued the request. The Origin allowlist is the check
that survives both settings.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, SpaceServiceDep, assert_same_site
from app.models.base import SpaceRole
from app.models.user import User
from app.schemas.space import (
    AddItemsRequest,
    AddItemsResponse,
    CreateInviteRequest,
    CreateSpaceRequest,
    InviteResponse,
    SpaceDetail,
    SpaceMemberRead,
    SpaceRead,
    UpdateSpaceRequest,
)
from app.schemas.vault import VaultItemRead
from app.services.space_service import SpaceSummary

router = APIRouter(prefix="/spaces", tags=["spaces"])

#: State changes only. A GET carries no CSRF risk and adding the dependency there would
#: reject a legitimate cross-origin read for no benefit.
_WRITE = [Depends(assert_same_site)]

#: The columns a Space exposes. Named once so `SpaceRead` and `SpaceDetail` cannot drift
#: apart, and so adding a column to the model does not publish it by accident.
_PUBLIC_FIELDS = {
    "id",
    "name",
    "slug",
    "description",
    "visibility",
    "icon",
    "emoji",
    "accent",
    "pinned",
    "ai_overview",
    "ai_topics",
    "created_at",
    "connection_count",
}


def _read(summary: SpaceSummary) -> SpaceRead:
    return SpaceRead(
        **summary.space.model_dump(include=_PUBLIC_FIELDS),
        role=summary.role,
        memory_count=summary.memory_count,
        member_count=summary.member_count,
    )


@router.get("", response_model=list[SpaceRead])
async def list_spaces(user: CurrentUser, service: SpaceServiceDep) -> list[SpaceRead]:
    return [_read(s) for s in await service.list_for_user(user.id)]


@router.post(
    "", response_model=SpaceRead, status_code=status.HTTP_201_CREATED, dependencies=_WRITE
)
async def create_space(
    body: CreateSpaceRequest, user: CurrentUser, service: SpaceServiceDep
) -> SpaceRead:
    space, added = await service.create(
        user.id,
        body.name,
        description=body.description,
        visibility=body.visibility,
        icon=body.icon,
        emoji=body.emoji,
        accent=body.accent,
        item_ids=body.item_ids,
    )
    return _read(
        SpaceSummary(
            space=space, role=SpaceRole.owner, memory_count=added.added, member_count=1
        )
    )


@router.get("/for-item/{item_id}", response_model=list[uuid.UUID])
async def spaces_for_item(
    item_id: uuid.UUID, user: CurrentUser, service: SpaceServiceDep
) -> list[uuid.UUID]:
    """Which of the caller's Spaces already contain this memory.

    Drives the checkmarks in a card's "+" menu. Answers with ids only, and only for
    Spaces the caller is in, so it cannot be used to probe whether a stranger filed
    something. An id the caller cannot see is simply absent -- there is no 404 here
    because "this memory is in none of your Spaces" is a real answer.

    Declared above the `/{space_id}` routes for the same reason the invite route is: a
    literal segment that could be read as a parameter has to be matched first.
    """
    return await service.spaces_for_item(item_id, user.id)


@router.post(
    "/invites/{invite_token}/accept", response_model=SpaceRead, dependencies=_WRITE
)
async def accept_invite(
    invite_token: str, user: CurrentUser, service: SpaceServiceDep
) -> SpaceRead:
    """Spend an invite link.

    Every rejection is the same 404 -- unknown token, spent token, expired token, deleted
    Space. Distinguishing them tells whoever found the link in a screenshot exactly what
    they are holding.

    Declared **above** the `/{space_id}` routes, like `GET /vault/uploads/limits` is: a
    literal segment that could be read as a parameter has to be matched first. The path
    parameter is `invite_token` rather than `token` because FastAPI resolves path-parameter
    names across the whole dependency tree, and `get_current_user` already takes a `token`
    -- from a cookie, with a default, which a path parameter may not have.
    """
    space = await service.accept_invite(invite_token, user.id)
    return _read(await service.summary(space.id, user.id))


@router.get("/{space_id}", response_model=SpaceDetail)
async def get_space(
    space_id: uuid.UUID, user: CurrentUser, service: SpaceServiceDep
) -> SpaceDetail:
    summary, items, owner, members = await service.detail(space_id, user.id)
    detail = SpaceDetail(
        **summary.space.model_dump(include=_PUBLIC_FIELDS),
        role=summary.role,
        memory_count=summary.memory_count,
        member_count=summary.member_count,
    )
    # `VaultItemRead`, never `VaultItemDetail`. A member sees the *card* of another
    # member's memory -- title, summary, tags, thumbnail -- and never its body, its
    # highlights, its metadata or its stored file. Being in a shared Space is not a grant
    # on the memory itself, and this line is where that stops being true if it is widened.
    detail.items = [VaultItemRead.model_validate(i) for i in items]
    detail.members = _members(owner, members)
    return detail


@router.patch("/{space_id}", response_model=SpaceRead, dependencies=_WRITE)
async def update_space(
    space_id: uuid.UUID,
    body: UpdateSpaceRequest,
    user: CurrentUser,
    service: SpaceServiceDep,
) -> SpaceRead:
    await service.update(
        space_id,
        user.id,
        name=body.name,
        description=body.description,
        visibility=body.visibility,
        icon=body.icon,
        emoji=body.emoji,
        accent=body.accent,
        pinned=body.pinned,
    )
    return _read(await service.summary(space_id, user.id))


@router.delete("/{space_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_WRITE)
async def delete_space(
    space_id: uuid.UUID, user: CurrentUser, service: SpaceServiceDep
) -> None:
    await service.delete(space_id, user.id)


@router.post("/{space_id}/items", response_model=AddItemsResponse, dependencies=_WRITE)
async def add_items(
    space_id: uuid.UUID,
    body: AddItemsRequest,
    user: CurrentUser,
    service: SpaceServiceDep,
) -> AddItemsResponse:
    result = await service.add_items(space_id, user.id, body.item_ids)
    return AddItemsResponse(added=result.added, skipped=result.skipped)


@router.delete(
    "/{space_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_WRITE,
)
async def remove_item(
    space_id: uuid.UUID,
    item_id: uuid.UUID,
    user: CurrentUser,
    service: SpaceServiceDep,
) -> None:
    if not await service.remove_item(space_id, user.id, item_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Memory is not in this Space")


@router.get("/{space_id}/members", response_model=list[SpaceMemberRead])
async def list_members(
    space_id: uuid.UUID, user: CurrentUser, service: SpaceServiceDep
) -> list[SpaceMemberRead]:
    owner, members = await service.list_members(space_id, user.id)
    return _members(owner, members)


@router.post(
    "/{space_id}/invites",
    response_model=InviteResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_WRITE,
)
async def create_invite(
    space_id: uuid.UUID,
    body: CreateInviteRequest,
    user: CurrentUser,
    service: SpaceServiceDep,
) -> InviteResponse:
    issued = await service.create_invite(space_id, user.id, body.role)
    return InviteResponse(url=issued.url, role=issued.role, expires_at=issued.expires_at)


@router.delete(
    "/{space_id}/members/{member_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_WRITE,
)
async def remove_member(
    space_id: uuid.UUID,
    member_id: uuid.UUID,
    user: CurrentUser,
    service: SpaceServiceDep,
) -> None:
    if not await service.remove_member(space_id, user.id, member_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member of this Space")


def _members(owner: User, members: Sequence[tuple[User, str]]) -> list[SpaceMemberRead]:
    """Owner first, then the invited, in join order.

    The owner is assembled here rather than stored as a `space_members` row so there is
    exactly one source of truth for ownership -- a role row can never contradict
    `spaces.user_id`.
    """
    return [
        SpaceMemberRead(
            user_id=owner.id,
            name=owner.name,
            avatar_url=owner.avatar_url,
            role=SpaceRole.owner,
        ),
        *(
            SpaceMemberRead(
                user_id=member.id,
                name=member.name,
                avatar_url=member.avatar_url,
                role=_role(role),
            )
            for member, role in members
        ),
    ]


def _role(value: str) -> SpaceRole:
    """An unrecognised stored role reads as the least privilege, never the most."""
    try:
        return SpaceRole(value)
    except ValueError:
        return SpaceRole.viewer
