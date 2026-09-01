"""One message in, structured blocks out. The half of the bot that has no platform.

This is `RecallChatService.respond`'s routing, lifted out of the surface that used to own
it. Nothing here knows what a chat window looks like: the return value is a list of
blocks describing *what to say*, and turning those into markup is a renderer's job, one
per surface. A second surface is then a renderer and an adapter, not a second copy of
this file -- which is what `tests/chat_engine/test_boundaries.py` exists to keep true.

Three lanes, chosen by `router.route`, which costs no tokens:

* **STATUS** -- "did that save?", answered from the row itself with **no model call**
  (`status.py`). It is a separate lane rather than a retrieval question because the
  answer is a fact the application already holds: asking a generator whether a save
  happened is the one shape of question this product must never guess at.
* **RECALL** -- a question about the vault, answered from retrieved memories.
* **META and CHAT** -- a conversational reply with no retrieval at all. Both take the
  same lane: the chat model already knows what it is, so "what can you do" needs no
  branch of its own, only a guarantee that it never reaches the retrieval prompt and
  comes back "I couldn't find anything about you".

**The conversation lane is not a general-purpose assistant**, and that is enforced here
as well as asked for in the prompt. A message that is plainly somebody else's product --
write me a function, translate this, what is the capital of France -- is declined without
a model call (`scope.py`). Answering it would cost a provider call per turn, widen the
prompt-injection surface to "do X with this text", and, worst of the three, teach the
user that fluency is the signal: the next answer is about their vault, sounds exactly the
same, and is believed for the same reason. The point of everything in this package is
that a claim about the vault is backed by evidence, and a confident answer about
world capitals sitting beside it quietly erases the distinction.

**The engine is handed an already-authorised user.** `user_id` arrives through the
constructor because working out *which* account an external sender belongs to needs a
table this package must not know exists. A caller that has not resolved that lookup has
no business constructing this.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from typing import Protocol, cast

import structlog

from app.core.logging import get_logger
from app.services.chat_engine import scope, status
from app.services.chat_engine.router import Intent, route
from app.services.chat_engine.status import SaveStatusReader
from app.services.chat_engine.types import (
    Delta,
    ErrorBlock,
    ErrorKind,
    InboundMessage,
    ItemListBlock,
    ItemsEvent,
    OutboundReply,
    StreamEnd,
    StreamEvent,
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


class StreamingLanes(Protocol):
    """The same two lanes, delivered as they are written.

    Separate from `RecallLanes` and detected with `hasattr` rather than added to it: the
    Protocol is structural, so widening it would break every fake in the test suite at
    runtime instead of at type-check time -- and a surface that cannot stream (a bot that
    sends whole messages) must not be made to care that another one can.
    """

    def stream(
        self, user_id: uuid.UUID, question: str, session_id: str
    ) -> AsyncIterator[StreamEvent]: ...

    def stream_chat(self, message: str, session_id: str) -> AsyncIterator[StreamEvent]: ...


class ChatEngine:
    """The lanes, and which of them needs a model.

    `recall` is optional because a deployment with no chat model configured is a real
    state rather than an error -- capture still works, and so does the status lane, which
    is the point of it having no provider call. `saves` is optional for the same reason
    in reverse: a caller that cannot supply a vault reader gets the retrieval lane for a
    status question, which is grounded and honest, rather than the conversation lane,
    which would answer "I can't check what you've saved" while the row sat there
    completed.
    """

    def __init__(
        self,
        recall: RecallLanes | None,
        user_id: uuid.UUID,
        *,
        saves: SaveStatusReader | None = None,
    ) -> None:
        self.recall = recall
        self.user_id = user_id
        self.saves = saves

    def _session_id(self, msg: InboundMessage) -> str:
        """The conversation key: this account, in this chat -- never the chat alone.

        Short-term chat history is stored under this key and fed back into the next
        prompt, so it carries the titles and summaries the assistant has already spoken
        aloud. A key made only of the external chat id would survive a change of account
        on that chat: the same messaging identity, unlinked from one RecallAI account and
        linked to another, would open its first conversation prefilled with the previous
        account's memories. Binding the user id into the key makes that impossible by
        construction rather than by remembering to clear something on disconnect.
        """
        return f"{self.user_id}:{msg.external_chat_id}"

    async def handle(self, msg: InboundMessage) -> OutboundReply:
        """Route one message and return what to say. Never writes anything."""
        if not msg.is_private:
            # Belt and braces: the caller is expected to have dropped this already, and
            # the cost of being wrong is one person's memories read aloud to a room.
            log.info("chat_engine_non_private_ignored", surface=msg.surface)
            return OutboundReply()

        intent = classify(msg)
        text = msg.text or ""
        session_id = self._session_id(msg)

        # Stamped once, here, so every model call made downstream carries which surface
        # asked and which lane it took. Passing them down four signatures to reach one
        # log line is how those signatures rot; the log stream already merges contextvars
        # and this is the only place that knows both facts.
        structlog.contextvars.bind_contextvars(surface=msg.surface, intent=intent.value)

        if intent is Intent.STATUS and self.saves is not None:
            # The only lane in this package that calls no model at all. "Did that save?"
            # is a question about a row, and the row is the authority on it -- asking a
            # generator instead is how a completed capture gets reported as unknown.
            return await status.reply(self.saves, self.user_id, text)

        if self.recall is None:
            # No chat model configured. Said plainly rather than answered: a lane that
            # needs a provider and has none must not fall back to inventing something.
            log.info("chat_engine_no_chat_model", surface=msg.surface)
            return OutboundReply([ErrorBlock(ErrorKind.chat_unavailable)])

        if intent is Intent.RECALL or intent is Intent.STATUS:
            # STATUS reaches here only with no vault reader wired up. Retrieval is the
            # safe degradation: it answers from the vault or says it found nothing.
            if intent is Intent.STATUS:
                log.warning("chat_engine_status_reader_missing", surface=msg.surface)
            return _to_reply(await self.recall.answer(self.user_id, text, session_id))

        if intent is Intent.CHAT:
            # Checked only for CHAT: a CAPTURE is already being saved, a COMMAND is the
            # caller's own, RECALL has been recognised as a question about the vault,
            # and META is this assistant being asked about itself -- in scope by
            # definition. So nothing here can intercept a message that was going to
            # become a memory or a search.
            #
            # The lane is closed by default: `scope.check` allows a recognised social,
            # self-referential or domain message and refuses everything else. The
            # reason is logged because it is the one number worth watching -- a rise in
            # `no_domain_signal` is either an attack surface or a gate that has become
            # too tight, and the two are told apart by reading the messages.
            verdict = scope.check(text)
            if not verdict.allowed:
                log.info(
                    "chat_engine_out_of_scope",
                    surface=msg.surface,
                    reason=verdict.reason,
                )
                return OutboundReply([TextBlock(text=scope.DECLINE)])

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


    async def stream(self, msg: InboundMessage) -> AsyncIterator[StreamEvent]:
        """The same routing, emitting events instead of one finished reply.

        Every lane that does not produce prose gradually -- a status answer, a listing, a
        refusal, a fixed "nothing found" -- emits one event and ends, which is not a
        degraded stream but the honest shape of those replies: they are finished the
        moment they exist.

        A caller whose lanes cannot stream still gets a working answer here: the reply is
        produced whole and delivered as a single delta. That keeps a surface from having
        to know which capability its injected lanes happen to have.
        """
        if not msg.is_private:
            log.info("chat_engine_non_private_ignored", surface=msg.surface)
            yield StreamEnd()
            return

        intent = classify(msg)
        text = msg.text or ""
        session_id = self._session_id(msg)
        structlog.contextvars.bind_contextvars(surface=msg.surface, intent=intent.value)

        if intent is Intent.STATUS and self.saves is not None:
            # No model call, so nothing to stream: the answer is a row read and a
            # sentence, and it is finished before the first byte goes out.
            for block in (await status.reply(self.saves, self.user_id, text)).blocks:
                if isinstance(block, TextBlock):
                    yield Delta(text=block.text)
            yield StreamEnd()
            return

        if self.recall is None:
            log.info("chat_engine_no_chat_model", surface=msg.surface)
            yield StreamEnd(error=ErrorKind.chat_unavailable)
            return

        if intent is Intent.CHAT:
            verdict = scope.check(text)
            if not verdict.allowed:
                log.info(
                    "chat_engine_out_of_scope", surface=msg.surface, reason=verdict.reason
                )
                yield Delta(text=scope.DECLINE)
                yield StreamEnd()
                return

        streaming = cast("StreamingLanes | None", self.recall) if _streams(self.recall) else None
        if streaming is None:
            # Lanes that do not stream. Answered whole and delivered as one delta rather
            # than refused -- the reply is the same reply, it simply arrives at once.
            for event in _as_events(await self.handle(msg)):
                yield event
            return

        source = (
            streaming.stream(self.user_id, text, session_id)
            if intent is Intent.RECALL or intent is Intent.STATUS
            else streaming.stream_chat(text, session_id)
        )
        async for event in source:
            yield event


def _streams(lanes: RecallLanes) -> bool:
    """Whether these lanes can deliver as they are written.

    A capability check rather than an isinstance: `StreamingLanes` is structural, and the
    thing being asked is exactly what structural typing means -- does this object have
    the two methods.
    """
    return hasattr(lanes, "stream") and hasattr(lanes, "stream_chat")


def _as_events(reply: OutboundReply) -> list[StreamEvent]:
    """A finished reply as the events a stream would have produced."""
    events: list[StreamEvent] = []
    error: ErrorKind | None = None
    for block in reply.blocks:
        if isinstance(block, TextBlock):
            events.append(Delta(text=block.text))
        elif isinstance(block, ItemListBlock):
            events.append(ItemsEvent(items=block.items, total=block.total))
        else:
            error = block.kind
    events.append(StreamEnd(error=error))
    return events


def _to_reply(answer: RecallAnswer) -> OutboundReply:
    """One answer as blocks. Structure only -- not one character of markup."""
    if answer.failed:
        return OutboundReply([ErrorBlock(ErrorKind.provider_failure)])
    if answer.items:
        return OutboundReply([ItemListBlock(items=list(answer.items), total=answer.total)])
    return OutboundReply([TextBlock(text=answer.text or "")])
