"""The raw Telegram update, turned into something the engine can read.

This is the only place that understands Telegram's payload shape. Everything downstream
sees an `InboundMessage`: no `Update`, no entity offsets, no `chat.type` string.

**Attachments are described, not downloaded.** The spec's inbound type carries bytes;
this one carries Telegram's `file_id` instead, because fetching the bytes is already
`telegram/capture.py`'s job -- it enforces the size cap, sniffs the real type from the
magic bytes and streams to storage. Downloading here would either duplicate that or
replace it, and replacing the capture pipeline is explicitly out of scope. The engine
only ever reads this list for its length.
"""
from __future__ import annotations

from typing import Any

from app.services.chat_engine.types import Attachment, InboundMessage
from app.services.telegram.capture import first_url

SURFACE = "telegram"

#: Every key Telegram uses for a file. Order fixes which one is named first when a
#: message somehow carries two.
_ATTACHMENT_KEYS = ("document", "photo", "voice", "audio", "video", "video_note")


def parse_message(message: dict[str, Any]) -> InboundMessage | None:
    """One `message` object as an `InboundMessage`, or None if it is not addressable.

    None means there is no sender or no chat to answer -- an edited-message stub, a
    channel post, a shape Telegram has not documented. The caller acknowledges those and
    does nothing, which is the only safe reading of a message we cannot attribute.
    """
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None

    chat_id, user_id = chat.get("id"), sender.get("id")
    if chat_id is None or user_id is None:
        return None

    return InboundMessage(
        surface=SURFACE,
        external_user_id=str(user_id),
        external_chat_id=str(chat_id),
        text=message_text(message),
        attachments=attachments(message),
        # Telegram's own entity offsets, not a scan of the words: a `text_link` entity
        # hides its target behind a label, which reading the text would miss entirely.
        url=first_url(message),
        # Anything that is not a one-to-one chat is a room. Answering there would read
        # one member's vault aloud to everyone in it.
        is_private=chat.get("type") == "private",
    )


def message_text(message: dict[str, Any]) -> str:
    """A photo's words arrive as `caption`; a plain message's as `text`."""
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def attachments(message: dict[str, Any]) -> list[Attachment]:
    """Describe what came with the message, without fetching any of it.

    A photo arrives as a list of sizes, largest last, which is the one worth naming.
    `file_id` means something only to Telegram; turning it into bytes is `capture`'s job.
    """
    found = []
    for kind in _ATTACHMENT_KEYS:
        payload = message.get(kind)
        if not payload:
            continue
        blob = payload[-1] if isinstance(payload, list) else payload
        if not isinstance(blob, dict):
            blob = {}
        found.append(
            Attachment(
                kind=kind,
                file_id=blob.get("file_id"),
                file_name=blob.get("file_name"),
                mime_type=blob.get("mime_type"),
                size_bytes=blob.get("file_size"),
            )
        )
    return found
