"""What a tapped button does. No model, no LangChain, no prompt.

This module is the other half of the proposal boundary. A model may *ask* to save a note
or retry a capture; the writing happens here, in code whose inputs are a token and a
resolved account. Tool results are scraped captions -- exactly the text an attacker gets
to write -- so a write tool bound to a model reading them is one caption away from filing
something the person never asked for. The tap is what stands between the two, and this
file is deliberately the far side of it.

`tests/integrations/test_telegram_callbacks.py` asserts that this module imports nothing
from `app.ai`, because "the handler does not call a model" is a claim that stops being
true silently.

Order matters in `execute`: **the token is spent before the write**. A failure between the
two leaves nothing to retry with, which is the safe direction -- the person taps again and
is told it expired, rather than a half-failed write being repeatable by anyone holding it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.core.logging import get_logger
from app.models.base import Relation
from app.services.chat_engine.proposals import Action, Proposal, ProposalStore
from app.services.telegram import formatting
from app.services.telegram.capture import TelegramCaptureService
from app.services.vault_service import ItemNotFound, ReprocessError, VaultService

log = get_logger("telegram")

#: `callback_data` is capped at 64 bytes by Telegram, so the payload is a prefix and a
#: token and never the text a person tapped -- the text lives in Redis beside the token.
_ACCEPT = "p:"
_DECLINE = "p:no:"
_ANSWER = "q:"


class ConnectionWriter(Protocol):
    """The one write a tapped `connect` needs, named structurally on purpose.

    Importing `ConnectionService` directly would pull `app.ai.connections` into this
    module's import graph -- the relation-typing capability, which this path never uses.
    The boundary test checks this file's **direct** imports only (a transitive walk was
    tried and flags `VaultService -> app.ai.spans`, which is legitimate), so keeping the
    import out is the whole of what keeps that test meaningful rather than decorative.
    """

    async def connect(
        self,
        user_id: uuid.UUID,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        *,
        relation: Relation,
        note: str | None,
    ) -> object: ...


class Tap(StrEnum):
    accept = "accept"
    decline = "decline"
    answer = "answer"


@dataclass(frozen=True, slots=True)
class Callback:
    kind: Tap
    token: str


@dataclass(slots=True)
class Outcome:
    """What the surface should do about a tap."""

    reply: str | None = None
    enqueue_item_ids: list[uuid.UUID] = field(default_factory=list)
    #: Set when the tap was an offered answer: this text re-enters the ordinary inbound
    #: path as if the person had typed it.
    reroute_text: str | None = None


def parse(data: str | None) -> Callback | None:
    """The prefix and the token, or `None` for anything this surface did not mint."""
    if not data:
        return None
    # Decline first: `p:no:` also starts with `p:`, so the more specific prefix has to be
    # tested before the general one or every No would be read as a Yes.
    if data.startswith(_DECLINE):
        return Callback(Tap.decline, data[len(_DECLINE) :])
    if data.startswith(_ACCEPT):
        return Callback(Tap.accept, data[len(_ACCEPT) :])
    if data.startswith(_ANSWER):
        return Callback(Tap.answer, data[len(_ANSWER) :])
    log.info("telegram_callback_unknown_prefix")
    return None


async def execute(
    tap: Callback,
    *,
    store: ProposalStore,
    vault: VaultService,
    capture: TelegramCaptureService,
    connections: ConnectionWriter | None,
    user_id: uuid.UUID,
    chat_id: str,
    message: dict[str, object],
) -> Outcome:
    """Spend the token and do exactly what it authorised. Nothing else."""
    proposal = await store.spend(tap.token, user_id)
    if proposal is None:
        # Unknown, expired, already spent, or minted for another account -- one reply for
        # all four, so a token found in a screenshot tells its finder nothing.
        return Outcome(reply=formatting.proposal_expired())

    if tap.kind is Tap.decline:
        return Outcome(reply=formatting.proposal_declined())

    if proposal.action is Action.answer:
        return Outcome(reroute_text=proposal.args.get("text", ""))

    if proposal.action is Action.note:
        return await _save_note(proposal, capture, user_id, chat_id, message)

    if proposal.action is Action.retry:
        return await _retry(proposal, vault, user_id)

    if proposal.action is Action.delete:
        return await _delete(proposal, vault, user_id)

    if proposal.action is Action.connect:
        return await _connect(proposal, connections, user_id)

    return Outcome(reply=formatting.proposal_expired())


async def _save_note(
    proposal: Proposal,
    capture: TelegramCaptureService,
    user_id: uuid.UUID,
    chat_id: str,
    message: dict[str, object],
) -> Outcome:
    """The ordinary capture path, reached by a tap instead of by `/note`.

    Deliberately the same call `/note` makes, rather than a second way to write a note:
    the title derivation, the length cap and the `source` metadata that triggers the
    completion reply all live there, and a copy of them here would be a copy that drifts.
    """
    outcome = await capture.capture_note(
        user_id, proposal.args.get("text", ""), message, chat_id
    )
    if outcome.item is None:
        return Outcome(reply=formatting.failed())
    return Outcome(
        reply=formatting.note_saved(outcome.item),
        enqueue_item_ids=[outcome.item.id],
    )


async def _connect(
    proposal: Proposal, connections: ConnectionWriter | None, user_id: uuid.UUID
) -> Outcome:
    """`ConnectionService.connect`, scoped to the tapping account.

    The service re-checks that **both** ends belong to this user, per item, so a token
    cannot link a row it did not name or a row belonging to somebody else -- the ids in
    the payload are not trusted just because a proposal carried them.

    The relation is re-derived from the enum rather than taken as text: it arrives through
    Redis as a string, and a value that is not a relation must not reach a column that
    everything downstream reads as one.
    """
    if connections is None:
        # No service wired up, so the tool was never bound and no token of this action
        # should exist. Answered like any unredeemable one rather than raising.
        return Outcome(reply=formatting.proposal_expired())
    try:
        source_id = uuid.UUID(proposal.args.get("source_id", ""))
        target_id = uuid.UUID(proposal.args.get("target_id", ""))
    except ValueError:
        return Outcome(reply=formatting.proposal_expired())
    try:
        relation = Relation(proposal.args.get("relation", Relation.related_to.value))
    except ValueError:
        relation = Relation.related_to
    try:
        await connections.connect(
            user_id, source_id, target_id, relation=relation, note=None
        )
    except LookupError:
        # `ConnectionNotFound`, caught by its base class so this module needs no import
        # from the service package. One end is gone, or was never theirs -- the same
        # sentence as any spent token, because from the person's side it is the same
        # thing: nothing left to do.
        return Outcome(reply=formatting.proposal_expired())
    return Outcome(reply=formatting.connected())


async def _delete(
    proposal: Proposal, vault: VaultService, user_id: uuid.UUID
) -> Outcome:
    """`VaultService.delete`, scoped to the tapping account.

    The service re-checks ownership through `repo.get`, so the token cannot remove a row
    it did not name or a row belonging to somebody else -- and a tap on an already-deleted
    memory answers the same "expired" sentence as any other spent token, because from the
    person's side it is the same thing: nothing left to do.
    """
    try:
        item_id = uuid.UUID(proposal.args.get("memory_id", ""))
    except ValueError:
        return Outcome(reply=formatting.proposal_expired())
    if not await vault.delete(item_id, user_id):
        return Outcome(reply=formatting.proposal_expired())
    return Outcome(reply=formatting.deleted())


async def _retry(proposal: Proposal, vault: VaultService, user_id: uuid.UUID) -> Outcome:
    """`reprocess`, with the same refusals the HTTP route gives.

    The service owns the rules -- already queued, already finished, still inside the
    cooldown -- and they are enforced against `user_id` there, so a token cannot re-drive
    a row it did not name or a row belonging to somebody else.
    """
    try:
        item_id = uuid.UUID(proposal.args.get("memory_id", ""))
    except ValueError:
        return Outcome(reply=formatting.proposal_expired())
    try:
        item = await vault.reprocess(item_id, user_id)
    except ItemNotFound:
        return Outcome(reply=formatting.proposal_expired())
    except ReprocessError as exc:
        # The service's own wording: "already being processed", "just tried that, give it
        # N more seconds". Both are things the person can act on.
        return Outcome(reply=formatting.escape(str(exc)))
    return Outcome(reply=formatting.saving(item))
