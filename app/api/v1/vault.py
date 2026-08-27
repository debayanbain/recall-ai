"""Vault routes: save URL/note, list, get, delete, search."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile, status

from app.api.deps import CurrentUser, VaultServiceDep
from app.core.config import settings
from app.schemas.vault import (
    CreateNoteRequest,
    FileLinkResponse,
    SaveUrlRequest,
    UpdateContentRequest,
    VaultItemDetail,
    VaultItemRead,
    VaultListResponse,
)
from app.services.documents import DocumentError, allowed_extensions, max_upload_bytes
from app.services.editor_doc import EditorDocumentError
from app.storage import StorageError

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
async def upload_document(
    user: CurrentUser,
    service: VaultServiceDep,
    file: Annotated[UploadFile, File()],
) -> VaultItemRead:
    """Store an uploaded document in the bucket; index its text when it has any.

    Read with an explicit cap rather than trusting `file.size`: the client controls the
    Content-Length header, so a small declared size can still stream a large body. The
    file type is decided from the bytes inside `services/documents.py`, never from the
    filename or the browser's Content-Type.
    """
    limit = max_upload_bytes()
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Files must be under {limit // 1_048_576}MB.",
        )
    try:
        item = await service.save_document(user.id, data, file.filename)
    except DocumentError as exc:
        # These messages are written for humans and quote nothing from the file itself.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except StorageError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    return VaultItemRead.model_validate(item)


@router.get("/uploads/limits")
async def upload_limits(user: CurrentUser) -> dict[str, object]:
    """What the client may send, so the picker can filter before a 25MB round-trip."""
    return {
        "max_bytes": max_upload_bytes(),
        "extensions": allowed_extensions(),
        "storage_enabled": settings.storage_enabled,
    }


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


@router.patch("/{item_id}/content", response_model=VaultItemDetail)
async def update_item_content(
    item_id: uuid.UUID,
    body: UpdateContentRequest,
    user: CurrentUser,
    service: VaultServiceDep,
) -> VaultItemDetail:
    """Replace this item's body with what the user wrote in the editor.

    The request carries blocks and nothing else -- the plain text, the stored document
    and the surviving highlights are all derived server-side, so the browser cannot
    write a `content` that disagrees with the document it sent, nor touch any other
    column by adding it to the body.

    404 covers "no such item" and "not yours" alike: the repository scopes on `user_id`,
    so a guessed id from another account is indistinguishable from a missing one.
    """
    try:
        item = await service.update_content(item_id, user.id, body.blocks)
    except EditorDocumentError as exc:
        # Written for a human and quoting nothing back from the submitted document.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return VaultItemDetail.model_validate(item)


@router.get("/{item_id}/file", response_model=FileLinkResponse)
async def get_item_file(
    item_id: uuid.UUID, user: CurrentUser, service: VaultServiceDep, response: Response
) -> FileLinkResponse:
    """Mint a short-lived download URL for this item's stored file.

    404 covers "no such item", "not yours" and "has no file" alike -- the repository
    scopes on `user_id`, so another account's id is indistinguishable from a missing one
    and the endpoint cannot be used to probe which ids exist.
    """
    try:
        link = await service.file_link(item_id, user.id)
    except StorageError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    if link is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No file for this item")
    url, item = link
    # The URL is a bearer credential until it expires: keep it out of shared caches.
    response.headers["Cache-Control"] = "no-store"
    return FileLinkResponse(
        url=url,
        expires_in=settings.DOWNLOAD_LINK_TTL_SECONDS,
        file_name=item.file_name,
        file_size=item.file_size,
        mime_type=item.mime_type,
    )


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: uuid.UUID, user: CurrentUser, service: VaultServiceDep
) -> None:
    deleted = await service.delete(item_id, user.id)
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
