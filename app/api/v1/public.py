"""Public, unauthenticated Space sharing by slug (SEO-friendly).

The only route in the service that answers without a session, so the shape of what it
returns *is* the access-control boundary. `PublicSpace` is hand-built and narrower than
`SpaceRead` -- no id, no visibility, no members, no role -- and the items are
`VaultItemRead`, so a shared Space discloses cards and never bodies, exactly as it does
to a signed-in member.

Two filters do the real work and both live in the repository: `visibility == public`
(an `unlisted` Space is not a public one), and `deleted_at IS NULL` on both the Space and
every item. Without the second, deleting a memory left it readable by the whole internet.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import SpaceServiceDep
from app.schemas.space import PublicSpace
from app.schemas.vault import VaultItemRead

router = APIRouter(prefix="/public", tags=["public"])


@router.get("/{slug}", response_model=PublicSpace)
async def public_space(slug: str, service: SpaceServiceDep) -> PublicSpace:
    result = await service.get_public(slug)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Space not found")
    space, items = result
    return PublicSpace(
        name=space.name,
        description=space.description,
        icon=space.icon,
        emoji=space.emoji,
        accent=space.accent,
        ai_overview=space.ai_overview,
        items=[VaultItemRead.model_validate(i) for i in items],
    )
