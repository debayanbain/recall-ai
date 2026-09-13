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


class Relation(StrEnum):
    """How one memory relates to another.

    Stored as `Text` rather than as a PG enum, like `SpaceRole` and
    `extraction_runs.status`: adding `refutes` one day must not need an `ALTER TYPE`
    inside a migration. The column is not the check -- values are re-validated on the way
    in, and an unrecognised one is read as `related_to`.

    `related_to` is the weakest claim in the list and doubles as this vocabulary's
    "Other". A derived edge is always this one: a cosine distance says two memories are
    *close*, which is the only thing it can say -- two documents that flatly contradict
    each other are maximally close. Anything stronger is a person's judgement, or a
    model's, and is labelled as whichever it was.
    """

    related_to = "related_to"
    expands = "expands"
    supports = "supports"
    contradicts = "contradicts"
    inspired_by = "inspired_by"
    depends_on = "depends_on"
    example_of = "example_of"
    part_of = "part_of"


#: The relations that read the same from both ends. Everything else is directional --
#: "A is part of B" and "B is part of A" are different claims -- which is why an edge is
#: stored with a direction and read from either side. For the two below the direction
#: carries no meaning, so the service normalises them (`source_item_id == pair_low`) and
#: both ends render one label. Without that, a `contradicts` edge reads backwards half
#: the time and somebody eventually "fixes" it.
SYMMETRIC_RELATIONS = frozenset({Relation.related_to, Relation.contradicts})


class ConnectionOrigin(StrEnum):
    """Who drew the edge. Never an authorization input -- only a thing to render."""

    user = "user"
    ai = "ai"


class ConnectionStatus(StrEnum):
    """Where an edge is in its life.

    `dismissed` lives here rather than in a table of its own, and that is load-bearing:
    the unique constraint that stops a duplicate edge is the same one that has to stop a
    re-suggestion, so the derivation's `ON CONFLICT DO NOTHING` already refuses to
    re-propose a pair somebody declined. Two tables would mean two checks, and the second
    is the one that gets forgotten.
    """

    suggested = "suggested"
    confirmed = "confirmed"
    dismissed = "dismissed"
