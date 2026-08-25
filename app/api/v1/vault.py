"""Vault routes: save URL/note, list, get, delete, search."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status

from app.api.deps import CurrentUser, VaultServiceDep
from app.schemas.vault import (
    CreateNoteRequest,
    SaveUrlRequest,
    VaultItemDetail,
    VaultItemRead,
    VaultListResponse,
)
from app.services.pdf import MAX_UPLOAD_BYTES, PdfError

router = APIRouter(prefix="/vault", tags=["vault"])


@router.post("/save", response_model=VaultItemRead, status_code=status.HTTP_201_CREATED)
async def save_url(
    body: SaveUrlRequest,
    user: CurrentUser,
    service: VaultServiceDep,
    response: Response,
) -> VaultItemRead:
    """Save a URL. Returns immediately; AI runs async in the worker.

    Answers 200 instead of 201 when the link is already in the vault, so the client can
    say "already saved" rather than implying a second copy was created.
    """
    item, created = await service.save_url(user.id, str(body.url), body.title)
    if not created:
        response.status_code = status.HTTP_200_OK
    return VaultItemRead.model_validate(item)


@router.post("/note", response_model=VaultItemRead, status_code=status.HTTP_201_CREATED)
async def save_note(
    body: CreateNoteRequest, user: CurrentUser, service: VaultServiceDep
) -> VaultItemRead:
    item = await service.create_note(user.id, body.title, body.content)
    return VaultItemRead.model_validate(item)


@router.post("/upload", response_model=VaultItemRead, status_code=status.HTTP_201_CREATED)
async def upload_pdf(
    user: CurrentUser,
    service: VaultServiceDep,
    file: Annotated[UploadFile, File()],
) -> VaultItemRead:
    """Accept a PDF, keep its text, discard the file.

    Read with an explicit cap rather than trusting `file.size`: the client controls the
    Content-Length header, so a small declared size can still stream a large body.
    """
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"PDFs must be under {MAX_UPLOAD_BYTES // 1_048_576}MB.",
        )
    try:
        item = await service.save_pdf(user.id, data, file.filename)
    except PdfError as exc:
        # These messages are written for humans and contain nothing from the file itself.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
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
