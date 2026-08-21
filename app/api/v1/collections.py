"""Collection routes: create, list, add item, detail."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CollectionServiceDep, CurrentUser
from app.schemas.collection import (
    AddItemRequest,
    CollectionDetail,
    CollectionRead,
    CreateCollectionRequest,
)
from app.schemas.vault import VaultItemRead

router = APIRouter(prefix="/collections", tags=["collections"])


@router.post("", response_model=CollectionRead, status_code=status.HTTP_201_CREATED)
async def create_collection(
    body: CreateCollectionRequest, user: CurrentUser, service: CollectionServiceDep
) -> CollectionRead:
    c = await service.create(user.id, body.name, body.description, body.visibility)
    return CollectionRead.model_validate(c)


@router.get("", response_model=list[CollectionRead])
async def list_collections(
    user: CurrentUser, service: CollectionServiceDep
) -> list[CollectionRead]:
    items = await service.list(user.id)
    return [CollectionRead.model_validate(c) for c in items]


@router.get("/{collection_id}", response_model=CollectionDetail)
async def get_collection(
    collection_id: uuid.UUID, user: CurrentUser, service: CollectionServiceDep
) -> CollectionDetail:
    result = await service.get_with_items(collection_id, user.id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Collection not found")
    collection, items = result
    detail = CollectionDetail.model_validate(collection)
    detail.items = [VaultItemRead.model_validate(i) for i in items]
    return detail


@router.post("/{collection_id}/items", status_code=status.HTTP_204_NO_CONTENT)
async def add_item(
    collection_id: uuid.UUID,
    body: AddItemRequest,
    user: CurrentUser,
    service: CollectionServiceDep,
) -> None:
    ok = await service.add_item(collection_id, user.id, body.vault_item_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Collection not found")
