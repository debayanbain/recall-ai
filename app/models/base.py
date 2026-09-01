"""Shared model mixins and enums."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class ContentType(StrEnum):
    youtube = "youtube"
    article = "article"
    pdf = "pdf"
    # Any uploaded file that is not a PDF: docx, xlsx, csv, txt, ... Kept distinct from
    # `pdf` because the pipeline can read a PDF's text and cannot read a .docx's.
    document = "document"
    note = "note"
    instagram = "instagram"
    facebook = "facebook"
    tiktok = "tiktok"
    linkedin = "linkedin"
    voice = "voice"
    image = "image"


class ProcessingStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"


class Plan(StrEnum):
    free = "free"
    pro = "pro"
    team = "team"


class SpaceRole(StrEnum):
    """What a person may do inside a Space they did not create.

    Ordered least-to-most on purpose so `_RANK` in the service is a comparison rather than
    a table of cases. `owner` is never stored in `space_members` -- it is `spaces.user_id`
    -- but it appears here because the API reports a caller's effective role and the
    owner's answer has to be sayable.
    """

    viewer = "viewer"
    editor = "editor"
    owner = "owner"


class Visibility(StrEnum):
    private = "private"
    unlisted = "unlisted"
    public = "public"
