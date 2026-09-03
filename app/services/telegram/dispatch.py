"""One update in, one reply out.

This is the only place a Telegram message becomes an action, so it is also the only place
authorisation happens: an update carries no session, and the sole thing that turns a
sender into a RecallAI user is the `telegram_accounts` row looked up here. Anything that
runs before that lookup succeeds must not touch the vault.

Two hard rules:

* **Private chats only.** A bot added to a group receives that group's messages, and
  answering there would read one member's vault aloud to the room.
* **An unlinked sender learns nothing.** Not a count, not a title, not whether the
  account exists -- only how to connect.

Routing is by the *shape* of the message, never by asking a model what the person meant:

* **A link, or a file** -- saved, immediately, with no confirmation step. Someone who
  pastes a reel into a second brain is not opening a negotiation.
* **`/note <text>`** -- saved as a note. The only way plain text becomes a memory.
* **Anything else** -- answered by the chat model and **stored nowhere**. "hi" is not a
  memory, and a bot that quietly filed every greeting would fill the vault with rubbish
  the user then has to clean out.

That last rule is the one worth defending: capture is now *explicit* (a link, a file, or
`/note`), so the failure mode is a person having to retype a thought with `/note` in
front of it -- recoverable, and visible. The inverse default silently accumulates junk,
and the user only discovers it later, in bulk.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.services.chat_engine import status
from app.services.chat_engine.engine import (
    ChatEngine,
    RecallLanes,
    classify,
    session_id_for,
)
from app.services.chat_engine.proposals import ProposalStore
from app.services.chat_engine.router import Intent
from app.services.chat_engine.types import InboundMessage
from app.services.surfaces.telegram.parse import parse_message
from app.services.surfaces.telegram.render import render, render_markup
from app.services.telegram import confirm, formatting, limits
from app.services.telegram.capture import (
    CaptureKind,
    CaptureOutcome,
    TelegramCaptureService,
)
from app.services.telegram.client import TelegramClient
from app.services.telegram.linking import (
    LinkResult,
    TelegramIdentity,
    TelegramLinkService,
)
from app.services.vault_service import VaultService

log = get_logger("telegram")

_RECENT_LIMIT = 10


@dataclass(slots=True)
class DispatchResult:
    """What the caller still has to do after committing.

    Items are enqueued by the task, not here, and only once the transaction has
    committed -- otherwise a fast worker dequeues before the row is visible and the item
    is stranded at `pending` with the user waiting for a reply that never comes.
    """

    reply: str | None = None
    enqueue_item_ids: list[uuid.UUID] = field(default_factory=list)
    chat_id: str | None = None
    # Only ever a connect button, and only for a sender we could not identify. Built
    # from configuration in `formatting`, never from anything in the update.
    reply_markup: dict[str, Any] | None = None
    #: A turn to re-run through the agent after the reply has gone out, while the two
    #: systems are being compared. `None` in normal operation.
    shadow: ShadowTurn | None = None
    #: Set when this reply answers a tapped button. Telegram spins the button until the
    #: acknowledgement arrives, so it is feedback rather than bookkeeping.
    answer_callback_id: str | None = None
    #: The card whose buttons should come off, now that one of them has been acted on.
    clear_markup_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class ShadowTurn:
    """What the agent needs to answer the question the old lanes have just answered."""

    user_id: uuid.UUID
    question: str
    session_id: str
    lane: str


class TelegramDispatcher:
    def __init__(
        self,
        links: TelegramLinkService,
        vault: VaultService,
        client: TelegramClient,
        recall: RecallLanes | None = None,
        proposals: ProposalStore | None = None,
    ) -> None:
        self.links = links
        self.vault = vault
        self.client = client
        self.capture = TelegramCaptureService(vault, client)
        #: Where a tapped button's token is redeemed. `None` means this deployment has
        #: no store wired up, and a tap is then answered as expired rather than acted on
        #: -- the same answer an unknown token gets, which is the point.
        self.proposals = proposals
        # None when no chat model is configured. Plain text is then answered with
        # `chat_unavailable` rather than saved: links and files still capture, so nothing
        # a user meant to keep is lost, and nothing they meant as talk is kept.
        self.recall = recall

    async def handle(self, update: dict[str, Any]) -> DispatchResult:
        # A tapped button first: it is a different update shape entirely, and everything
        # below reads `message`. It goes through the same account lookup, the same
        # private-chat rule and the same rate limit -- a tap is a request like any other,
        # and the only thing that makes it privileged is a token it has to redeem.
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            return await self._handle_callback(callback)

        message = update.get("message")
        if not isinstance(message, dict):
            return DispatchResult()

        # The one hand-off from Telegram's payload shape to something portable. Nothing
        # below this line reads the update again.
        inbound = parse_message(message)
        if inbound is None:
            return DispatchResult()

        if not inbound.is_private:
            log.info("telegram_non_private_ignored")
            return DispatchResult()

        identity = TelegramIdentity.from_update(message)
        if identity is None:
            return DispatchResult()

        # The one shape decision, made once, by the engine. This module's remaining job
        # is to serve the two intents the engine cannot: its own commands, and capture.
        intent = classify(inbound)
        text = inbound.text or ""
        command, argument = _parse_command(text) if intent is Intent.COMMAND else (None, "")

        # /start is the only thing an unlinked sender may do, because it is the only
        # thing that can make them linked.
        if command == "start":
            return await self._handle_start(identity, argument)

        account = await self.links.resolve(identity.telegram_user_id)
        if account is None:
            return DispatchResult(
                formatting.not_linked(),
                chat_id=identity.chat_id,
                reply_markup=formatting.connect_markup(),
            )

        if command == "help":
            return DispatchResult(formatting.connected_help(), chat_id=identity.chat_id)
        if command == "disconnect":
            await self.links.disconnect(account.user_id)
            return DispatchResult(formatting.disconnected(), chat_id=identity.chat_id)
        if command == "recent":
            return await self._handle_recent(account.user_id, identity.chat_id)
        if command == "status":
            # The same lane "is it saved?" takes, reachable by typing rather than by
            # phrasing. Worth having explicitly: the phrase list is English-first, and a
            # command is the one form that works in every language.
            return await self._handle_status(account.user_id, identity.chat_id)
        if command == "note":
            return await self._handle_note(account.user_id, identity, message, argument)

        return await self._handle_message(
            account.user_id, identity, message, inbound, intent
        )

    async def _handle_start(
        self, identity: TelegramIdentity, token: str
    ) -> DispatchResult:
        if not token:
            existing = await self.links.resolve(identity.telegram_user_id)
            linked = existing is not None
            return DispatchResult(
                formatting.welcome(linked),
                chat_id=identity.chat_id,
                # A linked sender is looking at their own help text; there is nothing
                # for them to connect.
                reply_markup=None if linked else formatting.connect_markup(),
            )

        outcome = await self.links.consume(token, identity)
        if outcome.result is LinkResult.taken_by_other_user:
            return DispatchResult(formatting.link_taken(), chat_id=identity.chat_id)
        if outcome.result is LinkResult.invalid_token:
            return DispatchResult(
                formatting.link_expired(),
                chat_id=identity.chat_id,
                reply_markup=formatting.connect_markup(),
            )
        return DispatchResult(formatting.connected_help(), chat_id=identity.chat_id)

    async def _handle_recent(self, user_id: uuid.UUID, chat_id: str) -> DispatchResult:
        items, total = await self.vault.list_recent(user_id, _RECENT_LIMIT)
        return DispatchResult(formatting.recent(items, total), chat_id=chat_id)

    async def _handle_status(self, user_id: uuid.UUID, chat_id: str) -> DispatchResult:
        return DispatchResult(
            render(await status.reply(self.vault, user_id, "")), chat_id=chat_id
        )

    async def _handle_message(
        self,
        user_id: uuid.UUID,
        identity: TelegramIdentity,
        message: dict[str, Any],
        inbound: InboundMessage,
        intent: Intent,
    ) -> DispatchResult:
        # Capture stays here rather than moving into the engine: it needs this surface's
        # own file handles, and it writes -- the engine does neither. *Whether* a message
        # is one is no longer decided here.
        saves = intent is Intent.CAPTURE

        action = limits.Action.capture if saves else limits.Action.recall
        if not await limits.allow(identity.telegram_user_id, action):
            log.info("telegram_rate_limited", action=action.value)
            return DispatchResult(formatting.rate_limited(), chat_id=identity.chat_id)

        if saves:
            outcome = await self.capture.capture(user_id, message, identity.chat_id)
            return _capture_reply(outcome, identity.chat_id)

        # The user is resolved by now, and that lookup is the authorisation. The engine
        # is handed the result, never the means to do it. `recall` may be None -- no chat
        # model configured -- and the engine says so itself rather than this surface
        # second-guessing which lanes need a provider: the status lane needs none, and a
        # check here would have taken it away.
        engine = ChatEngine(self.recall, user_id, saves=self.vault)
        answer = await engine.handle(inbound)
        return DispatchResult(
            render(answer),
            chat_id=identity.chat_id,
            # A question's options and a proposal's Yes/No travel as a keyboard, which is
            # a different field of the same API call than the text. Rendering them into
            # the message body instead would offer buttons a person cannot press.
            reply_markup=render_markup(answer),
            shadow=_shadow_for(user_id, inbound, intent),
        )

    async def _handle_callback(self, callback: dict[str, Any]) -> DispatchResult:
        """One tapped button. Authorised exactly like a message, then redeemed.

        The order is the whole of it: private chat, then the account lookup that *is*
        this surface's access control, then the rate limit, and only then the token. A
        token is not an identity -- it says what may be done, never by whom -- so it is
        checked last and against the account this sender resolved to.
        """
        query_id = str(callback.get("id") or "")
        message = callback.get("message")
        if not isinstance(message, dict):
            return DispatchResult(answer_callback_id=query_id or None)

        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("type") != "private":
            # The bot does not act in a room, and a tap in one is the same disclosure a
            # reply in one would be.
            log.info("telegram_non_private_ignored")
            return DispatchResult(answer_callback_id=query_id or None)

        chat_id = str(chat.get("id"))
        message_id = message.get("message_id")
        sender = callback.get("from")
        sender_id = str(sender.get("id")) if isinstance(sender, dict) else ""
        if not sender_id:
            return DispatchResult(answer_callback_id=query_id or None)

        account = await self.links.resolve(sender_id)
        if account is None:
            # An unlinked sender learns nothing -- not that the token was real, not who
            # it belonged to. They are told how to connect, as everywhere else here.
            return DispatchResult(
                formatting.not_linked(),
                chat_id=chat_id,
                reply_markup=formatting.connect_markup(),
                answer_callback_id=query_id or None,
            )

        tap = confirm.parse(callback.get("data"))
        if tap is None or self.proposals is None:
            return DispatchResult(
                formatting.proposal_expired(),
                chat_id=chat_id,
                answer_callback_id=query_id or None,
            )

        if not await limits.allow(sender_id, limits.Action.capture):
            log.info("telegram_rate_limited", action=limits.Action.capture.value)
            return DispatchResult(
                formatting.rate_limited(),
                chat_id=chat_id,
                answer_callback_id=query_id or None,
            )

        outcome = await confirm.execute(
            tap,
            store=self.proposals,
            vault=self.vault,
            capture=self.capture,
            user_id=account.user_id,
            chat_id=chat_id,
            message=message,
        )

        if outcome.reroute_text is not None:
            # An offered answer. Fed back through the ordinary inbound path as if typed,
            # so there is no second routing path to keep in step -- and the text is the
            # person's own words from here on, which is what later lets it stand as the
            # provenance for a proposal.
            return await self._reroute(
                outcome.reroute_text, callback, chat_id, query_id, message_id
            )

        return DispatchResult(
            outcome.reply,
            enqueue_item_ids=outcome.enqueue_item_ids,
            chat_id=chat_id,
            answer_callback_id=query_id or None,
            clear_markup_message_id=message_id if isinstance(message_id, int) else None,
        )

    async def _reroute(
        self,
        text: str,
        callback: dict[str, Any],
        chat_id: str,
        query_id: str,
        message_id: object,
    ) -> DispatchResult:
        """A tapped option, replayed as an ordinary message."""
        replayed = {
            "chat": {"id": chat_id, "type": "private"},
            "from": callback.get("from"),
            "text": text,
        }
        result = await self.handle({"message": replayed})
        result.answer_callback_id = query_id or None
        result.clear_markup_message_id = (
            message_id if isinstance(message_id, int) else None
        )
        return result

    async def _handle_note(
        self,
        user_id: uuid.UUID,
        identity: TelegramIdentity,
        message: dict[str, Any],
        argument: str,
    ) -> DispatchResult:
        """`/note <text>` -- the explicit way to keep a thought.

        Rate-limited as a capture, because it is one.
        """
        if not argument.strip():
            return DispatchResult(formatting.note_usage(), chat_id=identity.chat_id)

        if not await limits.allow(identity.telegram_user_id, limits.Action.capture):
            log.info("telegram_rate_limited", action=limits.Action.capture.value)
            return DispatchResult(formatting.rate_limited(), chat_id=identity.chat_id)

        outcome = await self.capture.capture_note(
            user_id, argument, message, identity.chat_id
        )
        return _capture_reply(outcome, identity.chat_id)


def _shadow_for(
    user_id: uuid.UUID, inbound: InboundMessage, intent: Intent
) -> ShadowTurn | None:
    """The same turn, for the agent to answer out of sight -- when that is switched on.

    Only for messages the agent would have handled. A capture and a command never reach
    it, so re-running them would compare two systems on a question neither was asked.
    """
    if not settings.AGENT_SHADOW or not (inbound.text or "").strip():
        return None
    return ShadowTurn(
        user_id=user_id,
        question=inbound.text or "",
        session_id=session_id_for(user_id, inbound),
        lane=intent.value,
    )


def _capture_reply(outcome: CaptureOutcome, chat_id: str) -> DispatchResult:
    kind, item = outcome.kind, outcome.item
    if kind is CaptureKind.voice_unsupported:
        return DispatchResult(formatting.voice_unsupported(), chat_id=chat_id)
    if kind is CaptureKind.too_large:
        return DispatchResult(
            formatting.too_large(settings.TELEGRAM_MAX_FILE_MB), chat_id=chat_id
        )
    if kind in (CaptureKind.unsupported, CaptureKind.nothing):
        return DispatchResult(formatting.unsupported_file(), chat_id=chat_id)
    if item is None:
        return DispatchResult(formatting.failed(), chat_id=chat_id)
    if kind is CaptureKind.duplicate:
        return DispatchResult(formatting.duplicate(item), chat_id=chat_id)
    if kind is CaptureKind.stored_only:
        # Nothing to process, so nothing to enqueue and no later reply.
        return DispatchResult(formatting.stored_without_text(item), chat_id=chat_id)

    # The acknowledgement is deliberately terse: the real reply -- summary, category,
    # tags -- is sent by `deliver_telegram_result` once the pipeline finishes.
    ack = formatting.note_saved(item) if kind is CaptureKind.note else formatting.saving(item)
    return DispatchResult(ack, enqueue_item_ids=[item.id], chat_id=chat_id)


def _parse_command(text: str) -> tuple[str | None, str]:
    """Split `/command@botname argument` into its parts."""
    if not text.startswith("/"):
        return None, ""
    head, _, rest = text[1:].partition(" ")
    command = head.split("@", 1)[0].strip().lower()
    return (command or None), rest.strip()
