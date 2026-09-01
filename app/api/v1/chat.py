"""Asking the vault a question over HTTP, answered as it is written.

The bot answers into a chat window nobody watches type; a web page is the opposite -- the
reader is looking straight at the spot where the answer will appear, and four seconds of
nothing there is the single most common reason someone reloads and asks again, paying for
the whole thing twice. So this endpoint streams.

**Streaming does not mean unchecked.** Every fragment sent has been through the same
output rules the non-streaming reply gets: a citation naming a memory that was never
retrieved is stripped, a URL that appears in no memory is replaced, and the length cap is
applied as it goes (`chat_engine/validation.StreamValidator`). That is not a nicety.
Correcting afterwards is not an option here -- the entire point of the URL rule is that a
fabricated link is one a person is invited to *tap*, and by the time a correction arrives
they have tapped it. The validator holds back the trailing unfinished word for exactly
this reason: an id and a URL both contain no whitespace, so a fragment released at a
whitespace boundary has been seen whole.

Three protections beyond authentication, all of them because this endpoint spends money
on request:

* **`assert_same_site`**, like the auth and Spaces writes and unlike the read-only vault
  routes. A GET that costs nothing is a different risk from a POST that costs a model
  call per hit, and the session cookie is what a cross-origin page would be riding.
* **A per-user hourly cap.** The middleware limiter keys on client IP, which throttles a
  whole office behind one NAT and nobody behind a botnet. What costs money is a person
  asking, so that is what is counted.
* **A bounded question.** The text reaches a prompt and a log line.

Routing is `ChatEngine`'s, unchanged -- the same classifier the bot uses, so "is it
saved?" is a database read here too, and a message that is not about this product is
declined here for the same reason it is there.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, SessionDep, assert_same_site
from app.core import rate_limit
from app.core.config import settings
from app.core.logging import get_logger
from app.repositories.vault import VaultRepository
from app.schemas.vault import VaultItemRead
from app.services.chat_engine.engine import ChatEngine
from app.services.chat_engine.types import (
    Delta,
    InboundMessage,
    ItemsEvent,
    StatusEvent,
    StreamEvent,
)
from app.services.recall_chat import build_recall_responder
from app.services.vault_service import VaultService

log = get_logger("api.chat")

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(assert_same_site)])

#: The surface name stamped on every log line and model call from this endpoint, so a
#: question asked from the web is distinguishable from the same question asked in the bot.
_SURFACE = "web"


class AskRequest(BaseModel):
    """One question. Deliberately the only field.

    No `user_id`, no memory ids, no filters, no model or prompt override: everything that
    decides *which rows are read* comes from the session, and everything that decides how
    they are read is server-side. A request body with nothing in it to tamper with is one
    that cannot be tampered with.
    """

    question: str = Field(min_length=1, max_length=2000)
    #: Keeps one browser tab's follow-ups ("what about last month?") together. It is a
    #: client-chosen label and never an identity: the server binds the real user into the
    #: conversation key, so two people cannot land in one history by choosing the same
    #: string.
    conversation_id: str = Field(default="web", min_length=1, max_length=64)


def _event(name: str, payload: dict[str, Any]) -> str:
    """One SSE frame.

    `json.dumps` is what makes this safe: a `data:` field may not contain a raw newline,
    and answer text is full of them.
    """
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _frame(event: StreamEvent) -> str:
    if isinstance(event, Delta):
        return _event("delta", {"text": event.text})
    if isinstance(event, StatusEvent):
        # A retrieval is a database round trip and a provider call, and a page showing
        # nothing during it reads as a hang rather than as work.
        return _event("status", {"stage": event.stage})
    if isinstance(event, ItemsEvent):
        return _event(
            "items",
            {
                # `VaultItemRead` is the same card shape every other listing returns, so
                # nothing reaches the browser here that a listing would not already show
                # -- in particular not `content`, `item_metadata` or `storage_key`.
                "items": [
                    VaultItemRead.model_validate(item).model_dump(mode="json")
                    for item in event.items
                ],
                "total": event.total,
            },
        )
    return _event(
        "end",
        {
            "memory_ids": list(event.memory_ids),
            "corrected": event.corrected,
            "error": event.error.value if event.error else None,
        },
    )


@router.post("/ask")
async def ask(
    payload: AskRequest,
    user: CurrentUser,
    session: SessionDep,
) -> StreamingResponse:
    """Ask about your own memories.

    Server-sent events: `status` while it searches, `delta` as the answer is written,
    `items` for a listing, and `end`.

    `end` is always sent, including after a provider failure -- a stream that simply
    stops leaves the reader watching a cursor that will never move.
    """
    if not await rate_limit.consume("ask", str(user.id), settings.ASK_PER_HOUR):
        log.info("ask_rate_limited", user_id=str(user.id))
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "That's a lot of questions at once — try again in a little while.",
        )

    repo = VaultRepository(session)
    vault = VaultService(repo, None)
    engine = ChatEngine(build_recall_responder(repo), user.id, saves=vault)
    message = InboundMessage(
        surface=_SURFACE,
        # The session's user is the identity; these two carry the *conversation*, not
        # authorisation. The engine is handed `user.id` separately and never derives it
        # from anything in the body.
        external_user_id=str(user.id),
        external_chat_id=payload.conversation_id,
        text=payload.question,
    )

    async def _body() -> AsyncIterator[str]:
        async for event in engine.stream(message):
            yield _frame(event)

    return StreamingResponse(
        _body(),
        media_type="text/event-stream",
        headers={
            # An answer about one person's vault must never be held by a shared cache,
            # and `no-store` is the form that also covers a browser's back-forward cache.
            "Cache-Control": "no-store",
            # nginx buffers proxied responses by default, which turns a stream into one
            # delivery at the end -- the exact thing this endpoint exists to avoid.
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
