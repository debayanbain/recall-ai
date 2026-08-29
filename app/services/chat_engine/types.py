"""The shapes that cross the engine's boundary.

Everything here is data. An inbound message says what arrived and where from, in terms
any messaging surface can supply; a reply says *what to say*, never how it looks. That
separation is the whole point of the package: the moment a reply carries markup, the
engine has picked a surface, and the second surface either re-parses that markup or gets
a fork of the engine.

**Identity is external and stays external.** `external_user_id` and `external_chat_id`
are the sender and conversation as that surface numbers them -- not a RecallAI user. The
engine is never handed the means to turn one into the other: resolving an external sender
into an account *is* the access control, it needs the surface's own account table, and it
belongs to the caller that owns that table.

`text` is `None` on a message that carries only an attachment, which is an ordinary
capture rather than an edge case. `is_private` is carried because a group conversation is
a different thing entirely -- answering there reads one person's vault aloud to a room.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from app.models.vault import VaultItem


@dataclass(frozen=True, slots=True)
class Attachment:
    """A file that came with a message, described but not fetched.

    `file_id` is a handle the *surface* understands and only it can redeem; nothing here
    is a URL and nothing here is bytes. The engine reads this list for its length -- a
    message with a file attached is a capture -- and leaves the downloading to the caller
    that knows how.
    """

    kind: str
    file_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """One message, as any surface can describe it."""

    surface: str
    external_user_id: str
    external_chat_id: str
    text: str | None
    attachments: list[Attachment] = field(default_factory=list)
    is_private: bool = True
    #: The first link in the message, if the surface parsed one. Surfaces that mark up
    #: their own text know things a scan of the words cannot recover -- a hyperlinked
    #: label hides its target entirely -- so the one that parsed the message is the one
    #: that should say. Left unset, the engine falls back to scanning the text.
    url: str | None = None


@dataclass(frozen=True, slots=True)
class TextBlock:
    """Prose to say. Plain sentences -- no markup, and none is added downstream either."""

    text: str


@dataclass(frozen=True, slots=True)
class ItemListBlock:
    """Memories to show as a list. The rows, not the rendering.

    Carries the model rows rather than pre-formatted lines precisely so each surface can
    decide what a list is there: a linked title in one, a numbered list in another, a
    carousel in a third.
    """

    items: Sequence[VaultItem]
    total: int = 0


class ErrorKind(StrEnum):
    """What went wrong, not what to say about it.

    The wording belongs to the surface -- it is the thing that knows how much room there
    is and what tone the rest of its replies take -- so this names the situation and
    stops there.
    """

    provider_failure = "provider_failure"
    chat_unavailable = "chat_unavailable"


@dataclass(frozen=True, slots=True)
class ErrorBlock:
    kind: ErrorKind


Block: TypeAlias = TextBlock | ItemListBlock | ErrorBlock


@dataclass(frozen=True, slots=True)
class OutboundReply:
    """What to say back. Empty means say nothing at all, which is a real answer."""

    blocks: list[Block] = field(default_factory=list)
