"""Search routes (Phase 1: ILIKE over title/summary/content)."""
from __future__ import annotations

from fastapi import APIRouter, Query, Response

from app.api import cards
from app.api.deps import CurrentUser, VaultServiceDep
from app.schemas.vault import VaultListResponse

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=VaultListResponse)
async def search(
    user: CurrentUser,
    service: VaultServiceDep,
    response: Response,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> VaultListResponse:
    items, total = await service.search(user.id, q, limit, offset)
    # The same cards the vault listing and the detail page serve. Serialising directly
    # here left search results pointing at the expired scraped still while every other
    # surface showed the mirror -- the same memory, the same card component, two different
    # pictures, and the broken one is the search result.
    cards.no_store(response)
    return VaultListResponse(
        items=await cards.read_cards(items),
        total=total,
        limit=limit,
        offset=offset,
    )
