"""Vault routes: save URL/note, list, get, delete, search."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, VaultServiceDep
from app.schemas.vault import (
    CreateNoteRequest,
    SaveUrlRequest,
    VaultItemDetail,
    VaultItemRead,
    VaultListResponse,
)

router = APIRouter(prefix="/vault", tags=["vault"])


@router.post("/save", response_model=VaultItemRead, status_code=status.HTTP_201_CREATED)
async def save_url(
    body: SaveUrlRequest, user: CurrentUser, service: VaultServiceDep
) -> VaultItemRead:
    """Save a URL. Returns immediately; AI runs async in the worker."""
    item = await service.save_url(user.id, str(body.url), body.title)
    return VaultItemRead.model_validate(item)


@router.post("/note", response_model=VaultItemRead, status_code=status.HTTP_201_CREATED)
async def save_note(
    body: CreateNoteRequest, user: CurrentUser, service: VaultServiceDep
) -> VaultItemRead:
    item = await service.create_note(user.id, body.title, body.content)
    return VaultItemRead.model_validate(item)


@router.get("", response_model=VaultListResponse)
async def list_vault(
    user: CurrentUser,
    service: VaultServiceDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> VaultListResponse:
    items, total = await service.list(user.id, limit, offset)
    return VaultListResponse(
        items=[VaultItemRead.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{item_id}", response_model=VaultItemDetail)
async def get_item(
    item_id: uuid.UUID, user: CurrentUser, service: VaultServiceDep
) -> VaultItemDetail:
    item = await service.get(item_id, user.id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return VaultItemDetail.model_validate(item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: uuid.UUID, user: CurrentUser, service: VaultServiceDep
) -> None:
    deleted = await service.delete(item_id, user.id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
