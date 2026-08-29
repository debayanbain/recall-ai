"""One message in, structured blocks out. The half of the bot that has no platform.

This is `RecallChatService.respond`'s routing, lifted out of the surface that used to own
it. Nothing here knows what a chat window looks like: the return value is a list of
blocks describing *what to say*, and turning those into markup is a renderer's job, one
per surface. A second surface is then a renderer and an adapter, not a second copy of
this file -- which is what `tests/chat_engine/test_boundaries.py` exists to keep true.

Two lanes, chosen by `router.route`, which costs no tokens:

* **RECALL** -- a question about the vault, answered from retrieved memories.
* **META and CHAT** -- a conversational reply with no retrieval at all. Both take the
  same lane: the chat model already knows what it is, so "what can you do" needs no
  branch of its own, only a guarantee that it never reaches the retrieval prompt and
  comes back "I couldn't find anything about you".

**The engine is handed an already-authorised user.** `user_id` arrives through the
constructor because working out *which* account an external sender belongs to needs a
table this package must not know exists. A caller that has not resolved that lookup has
no business constructing this.
"""
from __future__ import annotations

import re
import uuid
from typing import Protocol

import structlog

from app.core.logging import get_logger
from app.services.chat_engine.router import Intent, route
from app.services.chat_engine.types import (
    ErrorBlock,
    ErrorKind,
    InboundMessage,
    ItemListBlock,
    OutboundReply,
    TextBlock,
)
from app.services.recall_chat import RecallAnswer

log = get_logger("chat.engine")

#: Presence-only link detection, for the router's `url` argument. The router does no URL
#: finding of its own, because every surface carries links differently -- some hand over
#: parsed entities, and a surface that does should pass what it parsed. This is the
#: fallback for a message that is nothing but text. Nothing is ever fetched from the
#: result; it only decides a branch.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def first_url(text: str | None) -> str | None:
    match = _URL_RE.search(text or "")
    return match.group(0) if match else None


def classify(msg: InboundMessage) -> Intent:
    """What this message is. Pure, and the only shape decision in the system.

    Exposed separately from `handle` because two of the five intents cannot be served
    here. A COMMAND is the caller's own vocabulary, and a CAPTURE needs the caller's file
    handles and writes to its vault -- so the caller asks this what it is holding and
    hands those two to the machinery it already has. What it must not do is work that out
    for itself: a surface that decides "this looks like a link" on its own is a second
    router, and the two drift.
    """
    return route(
        msg.text,
        url=msg.url or first_url(msg.text),
        has_attachment=bool(msg.attachments),
    )


class RecallLanes(Protocol):
    """The retrieval and conversation halves, injected so this never imports the AI stack."""

    async def answer(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> RecallAnswer: ...

    async def chat(self, message: str, session_id: str) -> RecallAnswer: ...


class ChatEngine:
    def __init__(self, recall: RecallLanes, user_id: uuid.UUID) -> None:
        self.recall = recall
        self.user_id = user_id

    async def handle(self, msg: InboundMessage) -> OutboundReply:
        """Route one message and return what to say. Never writes anything."""
        if not msg.is_private:
            # Belt and braces: the caller is expected to have dropped this already, and
            # the cost of being wrong is one person's memories read aloud to a room.
            log.info("chat_engine_non_private_ignored", surface=msg.surface)
            return OutboundReply()

        intent = classify(msg)
        text = msg.text or ""
        session_id = msg.external_chat_id

        # Stamped once, here, so every model call made downstream carries which surface
        # asked and which lane it took. Passing them down four signatures to reach one
        # log line is how those signatures rot; the log stream already merges contextvars
        # and this is the only place that knows both facts.
        structlog.contextvars.bind_contextvars(surface=msg.surface, intent=intent.value)

        if intent is Intent.RECALL:
            return _to_reply(await self.recall.answer(self.user_id, text, session_id))

        if intent is Intent.COMMAND or intent is Intent.CAPTURE:
            # The caller claims these through `classify` before it gets here, so what
            # reaches this point is the remainder: a command the caller has no handler
            # for, `/froobulate`. Answered conversationally rather than raised -- this
            # runs behind a webhook that redelivers on any non-2xx, so an exception is an
            # infinite retry loop over a message we could have replied to. Nothing is
            # lost either way: nothing here can save anything.
            log.warning(
                "chat_engine_unexpected_intent",
                intent=intent.value,
                surface=msg.surface,
            )

        return _to_reply(await self.recall.chat(text, session_id))


def _to_reply(answer: RecallAnswer) -> OutboundReply:
    """One answer as blocks. Structure only -- not one character of markup."""
    if answer.failed:
        return OutboundReply([ErrorBlock(ErrorKind.provider_failure)])
    if answer.items:
        return OutboundReply([ItemListBlock(items=list(answer.items), total=answer.total)])
    return OutboundReply([TextBlock(text=answer.text or "")])
