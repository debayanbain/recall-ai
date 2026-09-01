"""Vault routes: save URL/note, list, get, delete, search."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status

from app.api.deps import CurrentUser, VaultServiceDep
from app.core.config import settings
from app.schemas.vault import (
    CreateNoteRequest,
    FileLinkResponse,
    ReprocessRequest,
    SaveUrlRequest,
    UpdateContentRequest,
    VaultItemDetail,
    VaultItemRead,
    VaultListResponse,
)
from app.services.documents import DocumentError, allowed_extensions, max_upload_bytes
from app.services.editor_doc import EditorDocumentError
from app.services.transcription import (
    LANGUAGES,
    TranscriptionError,
    TranscriptionFailed,
    TranscriptionUnavailable,
    max_voice_bytes,
    parse_waveform,
)
from app.services.vault_service import ItemNotFound, ReprocessError
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


@router.post("/voice", response_model=VaultItemRead, status_code=status.HTTP_201_CREATED)
async def save_voice_note(
    user: CurrentUser,
    service: VaultServiceDep,
    audio: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form()] = None,
    peaks: Annotated[str | None, Form()] = None,
    language: Annotated[str | None, Form()] = None,
    duration: Annotated[float | None, Form()] = None,
) -> VaultItemRead:
    """Transcribe a spoken note and save what was said.

    The transcript is the memory: it is summarised, tagged and embedded like any other
    item, so a voice note is searchable by its words rather than by its filename. The
    recording is filed alongside it when a bucket is configured, and its absence never
    costs the user the transcript.

    Read with an explicit cap rather than trusting `audio.size` -- the client writes
    Content-Length, so a small declared size can still stream a large body. The container
    is decided from the bytes in `services/transcription.py`; the filename a browser
    invents for a `MediaRecorder` blob is never consulted.

    `language` (ISO-639-1) pins the transcription instead of letting the model detect it.
    Auto-detection is the default and is right for a multilingual vault, but it *is* a
    guess -- a short Bengali clip has been observed coming back as fluent Traditional
    Chinese -- so the recorder offers the choice and this removes the guess entirely.
    Unknown codes fall back to auto-detect rather than being refused: the value is
    forwarded to a provider and rendered on a page, so it is re-derived from a closed
    allowlist, and a client bug must not cost the user their words.

    `duration` is the recorder's own measurement, kept because only the whisper-* models
    report one and the player needs a length regardless.

    `peaks` is the amplitude shape the recorder measured while recording, used to draw the
    waveform on the memory page. It is client-written data bound for a JSONB column, so
    `parse_waveform` re-derives it rather than trusting it and answers None for anything
    malformed -- the picture is decoration beside a transcript and must never cost the
    user the words.
    """
    limit = max_voice_bytes()
    data = await audio.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"Recordings must be under {limit // 1_048_576}MB.",
        )
    try:
        item = await service.save_voice_note(
            user.id,
            data,
            title=title,
            waveform=parse_waveform(peaks),
            language=language,
            # Sanity-bounded rather than trusted: this only labels the player's scrubber,
            # and a negative or absurd value would render as a broken control.
            duration=duration if duration and 0 < duration < 86_400 else None,
        )
    except TranscriptionUnavailable:
        # A deployment fact, not the caller's mistake, and worded so nobody re-records.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Voice notes aren't available on this server yet.",
        ) from None
    except TranscriptionError as exc:
        # Written for a human and quoting nothing from the audio.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except TranscriptionFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    except StorageError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from None
    return VaultItemRead.model_validate(item)


@router.get("/uploads/limits")
async def upload_limits(user: CurrentUser, response: Response) -> dict[str, object]:
    """What the client may send, so the picker can filter before a 25MB round-trip.

    This is deployment configuration -- it changes when the server restarts, never
    between two requests -- but the picker asks for it on every mount, and against a
    database in another region that was a ~600ms wait before the user could choose a
    file. `private` keeps it out of shared caches (it is behind a session cookie and
    describes that user's own limits); the max-age is short enough that a config change
    is picked up within a minute of a deploy.
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    return {
        "max_bytes": max_upload_bytes(),
        "extensions": allowed_extensions(),
        "storage_enabled": settings.storage_enabled,
        # Voice is capped separately and can be off while uploads are on -- it needs a
        # speech key, not a bucket. The client reads `enabled` to decide whether to show
        # a record button at all, rather than offering one that can only ever error.
        "voice": {
            "enabled": settings.transcription_enabled,
            "max_bytes": max_voice_bytes(),
            "max_seconds": settings.MAX_VOICE_NOTE_SECONDS,
            # The picker is built from this rather than a list copied into the client:
            # a code the client offers that the server does not accept would silently
            # fall back to auto-detect, which is the bug it exists to prevent.
            "languages": [
                {"code": code, "label": name.title()} for code, name in LANGUAGES.items()
            ],
            "default_language": settings.TRANSCRIBE_LANGUAGE or None,
        },
        # Read by the detail page to decide whether to offer a re-read of a memory whose
        # video was never looked at. Same reasoning as `voice.enabled`: the alternative
        # is a button whose only possible outcome is the server refusing it, on a page
        # where the refusal costs a round trip to learn.
        "video": {"enabled": settings.video_understanding_enabled},
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


@router.post("/{item_id}/reprocess", response_model=VaultItemRead)
async def reprocess_item(
    item_id: uuid.UUID,
    user: CurrentUser,
    service: VaultServiceDep,
    body: ReprocessRequest | None = None,
) -> VaultItemRead:
    """Put a failed or skipped item back on the queue.

    The pipeline can fail for reasons that have nothing to do with the item -- a provider
    timeout, a rate limit, a worker that was killed mid-job -- and until now the only
    thing a person could do about it was delete the memory and save it again, losing the
    id, the link and anything they had edited. This is the same work, re-driven.

    404 covers "no such item" and "not yours" alike: the repository scopes on `user_id`,
    so a guessed id from another account is indistinguishable from a missing one.

    409 is the honest answer for an item that is already queued or already finished --
    the client hides the button in both states, so reaching here means a double click.
    429 is the cooldown. Neither is an error worth a stack trace.

    A **voice note** is the one kind that may be re-driven after it *succeeded*, because
    its transcript is the one output that can be confidently and fluently wrong, and its
    audio is still in the bucket to redo it from. `language` (ISO-639-1) pins that re-run,
    which is the point: repeating a failed auto-detection unchanged is the same coin flip.
    """
    try:
        item = await service.reprocess(item_id, user.id, body.language if body else None)
    except ItemNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
    except ReprocessError as exc:
        # Two different "no" answers, so the client can tell "wait a moment" from
        # "there is nothing to do here".
        code = (
            status.HTTP_429_TOO_MANY_REQUESTS
            if "seconds" in str(exc)
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(code, str(exc)) from None
    return VaultItemRead.model_validate(item)


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
