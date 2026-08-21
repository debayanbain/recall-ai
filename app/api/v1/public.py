"""Public, unauthenticated collection sharing by slug (SEO-friendly)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CollectionServiceDep
from app.schemas.collection import PublicCollection
from app.schemas.vault import VaultItemRead

router = APIRouter(prefix="/public", tags=["public"])


@router.get("/{slug}", response_model=PublicCollection)
async def public_collection(slug: str, service: CollectionServiceDep) -> PublicCollection:
    result = await service.get_public(slug)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Collection not found")
    collection, items = result
    return PublicCollection(
        name=collection.name,
        description=collection.description,
        items=[VaultItemRead.model_validate(i) for i in items],
    )
