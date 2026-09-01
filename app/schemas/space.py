"""Space request/response DTOs.

Two shapes of care here, both borrowed from `app/schemas/vault.py`.

**Nothing is a passthrough of the model.** Every write model lists exactly the fields a
caller may set, so `user_id`, `slug`, `ai_overview` and `deleted_at` are not reachable by
adding them to a JSON body -- there is nothing to over-post into.

**A member is not an email address.** `SpaceMemberRead` carries the display name and
avatar the UI needs and stops there. A Space is a place where people who may not know each
other end up in the same list; handing every member everyone else's address makes joining
one a disclosure.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.base import SpaceRole, Visibility
from app.schemas.vault import VaultItemRead

#: Matches `Space.emoji`. One or two glyphs; the column is 16 bytes because a single
#: emoji with a skin-tone modifier and a variation selector is already several.
_EMOJI_MAX = 16
_ACCENT_MAX = 24


class CreateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    visibility: Visibility = Visibility.private
    emoji: str | None = Field(default=None, max_length=_EMOJI_MAX)
    accent: str | None = Field(default=None, max_length=_ACCENT_MAX)
    #: Fill the Space in the same request. This is what makes accepting an AI proposal
    #: one click and one round trip; a create followed by a separate add can half-fail
    #: and leave an empty Space nobody asked for.
    item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class UpdateSpaceRequest(BaseModel):
    """Every field optional, and every field one a caller is allowed to set.

    `None` means "leave it alone" rather than "clear it", except for `emoji` and `accent`
    where the empty string is the way to clear -- a nullable field with two meanings for
    null is a field nobody can use correctly.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    visibility: Visibility | None = None
    emoji: str | None = Field(default=None, max_length=_EMOJI_MAX)
    accent: str | None = Field(default=None, max_length=_ACCENT_MAX)
    pinned: bool | None = None


class AddItemsRequest(BaseModel):
    item_ids: list[uuid.UUID] = Field(min_length=1, max_length=100)


class AddItemsResponse(BaseModel):
    """`skipped` counts what was already there or was not the caller's to add.

    Reported rather than raised: re-adding a memory is the normal outcome of "add all
    suggestions", not an error, and the old behaviour -- a primary-key violation surfacing
    as a 500 -- made the obvious user action look like a broken server.
    """

    added: int
    skipped: int


class CreateInviteRequest(BaseModel):
    #: `owner` is rejected by the service. Transferring a Space has consequences an
    #: invite dropdown should not be able to reach.
    role: SpaceRole = SpaceRole.viewer


class InviteResponse(BaseModel):
    """The raw token exists only in this response and in the link the user copies.

    Treat it as a credential: anyone holding it can join the Space until it is spent or
    expires. It is never stored -- only its SHA-256 is.
    """

    url: str
    role: SpaceRole
    expires_at: datetime


class SpaceMemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    name: str | None
    avatar_url: str | None
    role: SpaceRole


class SpaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str | None
    visibility: Visibility
    emoji: str | None
    accent: str | None
    pinned: bool
    ai_overview: str | None
    ai_topics: list[str]
    created_at: datetime

    #: The caller's own role, so the UI can hide controls it would only be refused for.
    #: Hiding is a courtesy; the server refuses regardless.
    role: SpaceRole
    memory_count: int
    member_count: int
    #: Null when connections have never been computed for this Space. The UI renders
    #: nothing rather than a zero -- "no connections" and "not measured" are different
    #: claims and only one of them is true.
    connection_count: int | None = None


class SpaceDetail(SpaceRead):
    items: list[VaultItemRead] = Field(default_factory=list)
    members: list[SpaceMemberRead] = Field(default_factory=list)


class PublicSpace(BaseModel):
    """The unauthenticated share page.

    Hand-built and narrower than `SpaceRead` on purpose: no id, no slug echo, no
    visibility, no member list, no role. A public page is read by people who are not in
    the Space, and the shape of what it returns is the whole access-control boundary.
    """

    name: str
    description: str | None
    emoji: str | None
    accent: str | None
    ai_overview: str | None
    items: list[VaultItemRead]
