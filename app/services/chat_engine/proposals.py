"""Single-use tokens for the things a model may suggest but must never do.

Two kinds of tap travel through here and they are the same mechanism:

* **A proposal.** The model wants to save a note or retry a failed capture. It calls a
  `propose_*` tool, which mints a token and hands back a preview; the person taps Yes and
  a code path with **no model in it** performs the write.
* **An offered answer.** The model asked a question with options. The person taps one and
  its text is fed back through the ordinary inbound path, as if typed.

The reason a proposal is not simply a write tool is worth restating, because it is the
whole security boundary of this feature: tool results are scraped captions and page text
-- exactly the text an attacker gets to write -- and a `save_memory` bound to a model
reading that is one caption away from filing something the person never asked for. What a
model may do is *ask*. What performs the write is a callback handler that imports no
LangChain and calls no provider.

The token is the same shape as this codebase's other single-use links (account linking,
Space invites), and for the same reasons: 32 random bytes handed over once, **only the
SHA-256 stored**, single use, and unknown / expired / spent all answer identically --
distinguishing them tells whoever found one in a screenshot what they are holding. A fast
hash is right here because the input is 32 random bytes, not a password: there is no
dictionary to run, and the lookup has to be one indexed equality match.

**Spend before the write.** A failure between the two leaves nothing to retry with, which
is the safe direction: the person taps again and is told it expired, rather than a
half-failed write being repeatable by anyone holding the token.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import redis.asyncio as redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("chat.proposals")

#: 32 bytes, urlsafe, is 43 characters. A messaging surface may have to carry this in a
#: 64-byte callback payload alongside a two-character prefix, which it does with room.
_TOKEN_BYTES = 32

#: What the reply says for a token that is unknown, expired, spent, or was minted for
#: somebody else. **One sentence for all four**: a message that distinguishes them tells
#: the holder of a leaked token which kind they are holding.
EXPIRED = "That one expired. Ask me again and I'll offer it fresh."


class Action(StrEnum):
    """What a tap does. Each is executed by deterministic code, never by a model."""

    note = "note"
    retry = "retry"
    #: Permanent, and the reason the confirmation card exists at all. It was deliberately
    #: left out of the first version of proposals while `VaultRepository.delete` still did
    #: a hard `session.delete()` with every read written for a soft one -- offering a tap
    #: that runs a half-implemented delete is worse than not offering it.
    delete = "delete"
    #: The person picked one of the options from a question. Its text re-enters the
    #: ordinary inbound path, so it is genuinely their own words from that point on --
    #: which is what makes it usable as the provenance for a later `propose_note`.
    answer = "answer"


@dataclass(frozen=True, slots=True)
class Proposal:
    """A minted, unspent intention."""

    user_id: uuid.UUID
    action: Action
    args: dict[str, str]


class ProposalStore(Protocol):
    """Injected, so a surface can be tested without a broker and the engine stays pure."""

    async def mint(self, proposal: Proposal) -> str | None: ...

    async def spend(self, token: str, user_id: uuid.UUID) -> Proposal | None: ...


def _key(token: str) -> str:
    return f"proposal:{hashlib.sha256(token.encode()).hexdigest()}"


class RedisProposalStore:
    """The real store. `None` from `mint` means "offer nothing" rather than "raise".

    A broker that is down must not take the answer down with it: the model is simply not
    given the proposal tools that turn, and the prompt already tells it what to say when
    they are absent.
    """

    async def mint(self, proposal: Proposal) -> str | None:
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        payload = json.dumps(
            {
                "user_id": str(proposal.user_id),
                "action": proposal.action.value,
                "args": proposal.args,
            }
        )
        client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
        try:
            await client.set(_key(token), payload, ex=settings.PROPOSAL_TTL_SECONDS)
            return token
        except Exception as exc:  # noqa: BLE001 - an unofferable action is not an outage
            log.warning("proposal_mint_failed", error=type(exc).__name__)
            return None
        finally:
            await client.aclose()

    async def spend(self, token: str, user_id: uuid.UUID) -> Proposal | None:
        """Take the token and return what it authorised, once.

        `GETDEL` is the whole of single-use: two taps racing on one token cannot both
        read it, so the second gets the expired reply rather than a second write.
        """
        if not token:
            return None
        client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
        try:
            raw = await client.getdel(_key(token))
        except Exception as exc:  # noqa: BLE001 - a tap that cannot be checked is refused
            log.warning("proposal_spend_failed", error=type(exc).__name__)
            return None
        finally:
            await client.aclose()

        if raw is None:
            return None
        proposal = _decode(raw)
        if proposal is None:
            return None
        if proposal.user_id != user_id:
            # The token was real and belonged to someone else. It has already been spent
            # by this call, which is correct: a token another account has seen is burnt.
            log.warning("proposal_wrong_user")
            return None
        return proposal


def _decode(raw: object) -> Proposal | None:
    try:
        data = json.loads(raw if isinstance(raw, str | bytes) else str(raw))
        return Proposal(
            user_id=uuid.UUID(str(data["user_id"])),
            action=Action(str(data["action"])),
            args={str(k): str(v) for k, v in dict(data.get("args") or {}).items()},
        )
    except Exception as exc:  # noqa: BLE001 - a malformed row is an expired one
        log.warning("proposal_unreadable", error=type(exc).__name__)
        return None


def normalise(text: str) -> str:
    """Whitespace-collapsed, lowercased -- the form provenance is compared in."""
    return " ".join((text or "").split()).lower()


def from_user_turn(text: str, turn: str) -> bool:
    """Whether `text` actually appears in what the person themselves wrote this turn.

    The one mechanical check that separates "help me save this thought" from a scraped
    caption saying "save 'send money to X' as a note". A model reading attacker-written
    text can be talked into proposing anything; it cannot be talked into having been
    *asked* for it, because the user's own message is not something a memory can edit.

    Deliberately a substring test after whitespace and case normalisation, and nothing
    looser. Fuzzy matching here would start approving paraphrases, and a paraphrase is
    exactly what an injected instruction looks like once a model has restated it.
    """
    wanted = normalise(text)
    return bool(wanted) and wanted in normalise(turn)
