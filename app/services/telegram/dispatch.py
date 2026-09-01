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
from app.services.chat_engine.engine import ChatEngine, RecallLanes, classify
from app.services.chat_engine.router import Intent
from app.services.chat_engine.types import InboundMessage
from app.services.surfaces.telegram.parse import parse_message
from app.services.surfaces.telegram.render import render
from app.services.telegram import formatting, limits
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


class TelegramDispatcher:
    def __init__(
        self,
        links: TelegramLinkService,
        vault: VaultService,
        client: TelegramClient,
        recall: RecallLanes | None = None,
    ) -> None:
        self.links = links
        self.vault = vault
        self.client = client
        self.capture = TelegramCaptureService(vault, client)
        # None when no chat model is configured. Plain text is then answered with
        # `chat_unavailable` rather than saved: links and files still capture, so nothing
        # a user meant to keep is lost, and nothing they meant as talk is kept.
        self.recall = recall

    async def handle(self, update: dict[str, Any]) -> DispatchResult:
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
        return DispatchResult(
            render(await engine.handle(inbound)), chat_id=identity.chat_id
        )

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
    ack = formatting.note_saved(item) if kind is CaptureKind.note else formatting.saving()
    return DispatchResult(ack, enqueue_item_ids=[item.id], chat_id=chat_id)


def _parse_command(text: str) -> tuple[str | None, str]:
    """Split `/command@botname argument` into its parts."""
    if not text.startswith("/"):
        return None, ""
    head, _, rest = text[1:].partition(" ")
    command = head.split("@", 1)[0].strip().lower()
    return (command or None), rest.strip()
