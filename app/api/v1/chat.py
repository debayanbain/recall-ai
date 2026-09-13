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
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, SessionDep, assert_same_site
from app.core import rate_limit
from app.core.config import settings
from app.core.logging import get_logger
from app.models.base import Relation
from app.queue.client import enqueue_shadow_agent_turn
from app.repositories.connection import ConnectionRepository
from app.repositories.vault import VaultRepository
from app.schemas.vault import VaultItemRead
from app.services.chat_engine.engine import ChatEngine, classify, session_id_for
from app.services.chat_engine.proposals import Action, RedisProposalStore
from app.services.chat_engine.types import (
    Delta,
    InboundMessage,
    ItemsEvent,
    ProposalEvent,
    QuestionEvent,
    StatusEvent,
    StreamEvent,
)
from app.services.connection_service import ConnectionNotFound, ConnectionService
from app.services.recall_chat import build_recall_responder
from app.services.vault_service import (
    NOTE_CONTENT_MAX,
    ItemNotFound,
    ReprocessError,
    VaultService,
    note_title,
)
from app.storage import get_storage

log = get_logger("api.chat")

router = APIRouter(prefix="/chat", tags=["chat"], dependencies=[Depends(assert_same_site)])

#: The surface name stamped on every log line and model call from this endpoint, so a
#: question asked from the web is distinguishable from the same question asked in the bot.
_SURFACE = "web"

#: One sentence for unknown, expired, spent and wrong-owner alike. Distinguishing them
#: tells the holder of a leaked token which kind they are holding.
_EXPIRED = "That one expired. Ask again and I'll offer it fresh."


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
    if isinstance(event, QuestionEvent):
        # The client renders the choices as chips. A tapped chip posts its own label as
        # the next question, so an answered question is an ordinary turn from there on --
        # no second routing path, and the text is genuinely the person's own words.
        return _event(
            "question",
            {
                "question": event.question,
                "options": [
                    {"label": choice.label, "token": choice.token}
                    for choice in event.choices
                ],
            },
        )
    if isinstance(event, ProposalEvent):
        # `preview` is the exact text that will be written, never a summary of it: if a
        # scraped caption talked the model into proposing something, this is where the
        # person reads the real words and declines.
        return _event(
            "proposal",
            {
                "token": event.accept_token,
                "action": event.action,
                "preview": event.preview,
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


class ProposalResult(BaseModel):
    """What a tapped confirmation did. Deliberately thin.

    It reports an outcome and, when one was created, the card for it -- the same
    `VaultItemRead` every other listing returns, so nothing reaches the browser here that
    a listing would not already show.
    """

    status: str
    message: str
    item: VaultItemRead | None = None


@router.post("/proposals/{proposal_token}/accept", response_model=ProposalResult)
async def accept_proposal(
    proposal_token: str, user: CurrentUser, session: SessionDep
) -> ProposalResult:
    """Do the thing the assistant offered to do. No model runs in this path.

    The whole point of a proposal is that the write happens here rather than in a tool a
    model can call: tool results are scraped page text, and a write tool bound to a model
    reading them is one caption away from filing something the person never asked for.

    The path parameter is `proposal_token` and not `token` on purpose. FastAPI resolves
    path-parameter names across the entire dependency tree, and `get_current_user` already
    takes a `token` -- from a cookie, with a default, which a path parameter may not have.
    Same trap as `/spaces/invites/{invite_token}/accept`.
    """
    store = RedisProposalStore()
    # Spent before anything is written. A failure between the two leaves nothing to retry
    # with, which is the safe direction: the person is told it expired rather than a
    # half-failed write staying repeatable by anyone holding the token.
    proposal = await store.spend(proposal_token, user.id)
    if proposal is None:
        # Unknown, expired, already spent, or minted for another account: one answer for
        # all four, so a token found in a screenshot tells its finder nothing.
        raise HTTPException(status.HTTP_404_NOT_FOUND, _EXPIRED)

    service = VaultService(VaultRepository(session), get_storage())

    if proposal.action is Action.note:
        text = proposal.args.get("text", "")[:NOTE_CONTENT_MAX]
        item = await service.create_note(
            user.id, note_title(text), text, enqueue=False
        )
        # Committed before the job is queued, not after: a worker that dequeues before
        # the row is visible logs `process_missing_item` and leaves the item stuck at
        # `pending`. The request boundary would commit this anyway -- doing it here is
        # what puts it *before* the enqueue.
        await session.commit()
        await service.enqueue(item)
        return ProposalResult(
            status="saved",
            message="Saved. I'll tag it in a moment.",
            item=VaultItemRead.model_validate(item),
        )

    if proposal.action is Action.retry:
        try:
            item = await service.reprocess(uuid.UUID(proposal.args["memory_id"]), user.id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _EXPIRED) from exc
        except ItemNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _EXPIRED) from exc
        except ReprocessError as exc:
            # The service's own wording and the same codes the manual retry route gives.
            code = (
                status.HTTP_429_TOO_MANY_REQUESTS
                if "seconds" in str(exc)
                else status.HTTP_409_CONFLICT
            )
            raise HTTPException(code, str(exc)) from exc
        return ProposalResult(
            status="retrying",
            message="Retrying that one now.",
            item=VaultItemRead.model_validate(item),
        )

    if proposal.action is Action.delete:
        try:
            removed = await service.delete(
                uuid.UUID(proposal.args["memory_id"]), user.id
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _EXPIRED) from exc
        if not removed:
            # Already gone, or never theirs. The same answer either way, for the same
            # reason `repo.get` returns None for both.
            raise HTTPException(status.HTTP_404_NOT_FOUND, _EXPIRED)
        await session.commit()
        return ProposalResult(status="deleted", message="Deleted. That one's gone.")

    if proposal.action is Action.connect:
        # Both ends re-checked against the tapping account, per item, inside the service:
        # the ids in a proposal are not trusted just because a proposal carried them.
        connections = ConnectionService(
            ConnectionRepository(session), VaultRepository(session)
        )
        try:
            relation = Relation(proposal.args.get("relation", Relation.related_to.value))
        except ValueError:
            # A value that is not a relation must never reach a column everything
            # downstream reads as one. `related_to` is the weakest claim and the
            # fail-closed direction.
            relation = Relation.related_to
        try:
            await connections.connect(
                user.id,
                uuid.UUID(proposal.args["source_id"]),
                uuid.UUID(proposal.args["target_id"]),
                relation=relation,
                note=None,
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, _EXPIRED) from exc
        except ConnectionNotFound as exc:
            # One end is gone, or was never theirs. Same answer as a spent token.
            raise HTTPException(status.HTTP_404_NOT_FOUND, _EXPIRED) from exc
        await session.commit()
        return ProposalResult(
            status="connected", message="Connected. It shows on both memories."
        )

    # `answer` proposals are a messaging-surface affordance: on the web a tapped chip
    # posts its own text as the next question, so there is nothing to redeem here.
    raise HTTPException(status.HTTP_404_NOT_FOUND, _EXPIRED)


@router.post("/proposals/{proposal_token}/decline", response_model=ProposalResult)
async def decline_proposal(
    proposal_token: str, user: CurrentUser
) -> ProposalResult:
    """Burn the token without doing anything with it.

    Spending it on a decline is the point: it stops a card left open in a tab from being
    tappable later by anyone who reaches that tab.
    """
    await RedisProposalStore().spend(proposal_token, user.id)
    return ProposalResult(status="declined", message="Okay, nothing saved.")


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
        # After the last frame: the shadow run answers nobody, so it must not be in front
        # of anyone's answer. Its failure is logged and never reaches this response --
        # the reader has already had theirs.
        if settings.AGENT_SHADOW and payload.question.strip():
            try:
                await enqueue_shadow_agent_turn(
                    str(user.id),
                    payload.question,
                    session_id_for(user.id, message),
                    _SURFACE,
                    classify(message).value,
                )
            except Exception as exc:  # noqa: BLE001 - an experiment is not worth a 500
                log.warning("agent_shadow_enqueue_failed", error=type(exc).__name__)

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
