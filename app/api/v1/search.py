"""Search routes (Phase 1: ILIKE over title/summary/content)."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, VaultServiceDep
from app.schemas.vault import VaultItemRead, VaultListResponse

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=VaultListResponse)
async def search(
    user: CurrentUser,
    service: VaultServiceDep,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> VaultListResponse:
    items, total = await service.search(user.id, q, limit, offset)
    return VaultListResponse(
        items=[VaultItemRead.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )
