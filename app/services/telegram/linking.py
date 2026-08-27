"""Binding a Telegram identity to a RecallAI user.

The flow is a one-shot bearer token carried in a `t.me/<bot>?start=<token>` deep link:
the browser proves who the user is (session cookie), the token carries that proof across
to the phone, and the bot spends it once.

Every rejection is the same rejection. The user is told the link expired whether the
token was never real, already spent, or bound to someone else -- a distinguishing message
tells a token holder which of those is true, which is exactly the information an attacker
who found a link in a screenshot would want. The server log keeps the real reason.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_link_token, new_link_token
from app.models.telegram import TelegramAccount, TelegramLinkToken
from app.repositories.telegram import (
    TelegramAccountRepository,
    TelegramLinkTokenRepository,
)

log = get_logger("telegram")


class LinkResult(StrEnum):
    linked = "linked"
    already_linked = "already_linked"
    invalid_token = "invalid_token"
    taken_by_other_user = "taken_by_other_user"


@dataclass(slots=True)
class LinkOutcome:
    result: LinkResult
    account: TelegramAccount | None = None


@dataclass(slots=True)
class IssuedLink:
    deep_link: str
    expires_at: datetime
    expires_in: int


@dataclass(slots=True)
class TelegramIdentity:
    """The `from` object of a Telegram update, narrowed to what we store."""

    telegram_user_id: str
    chat_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    language_code: str | None = None

    @classmethod
    def from_update(cls, message: dict[str, Any]) -> TelegramIdentity | None:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        sender_id = sender.get("id")
        chat_id = chat.get("id")
        if sender_id is None or chat_id is None:
            return None
        return cls(
            telegram_user_id=str(sender_id),
            chat_id=str(chat_id),
            username=_text_or_none(sender.get("username")),
            first_name=_text_or_none(sender.get("first_name")),
            last_name=_text_or_none(sender.get("last_name")),
            language_code=_text_or_none(sender.get("language_code")),
        )


def _text_or_none(value: object) -> str | None:
    """Telegram sends strings, but an update is untrusted input like any other body."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed[:200] or None


class TelegramLinkService:
    def __init__(
        self,
        accounts: TelegramAccountRepository,
        tokens: TelegramLinkTokenRepository,
    ) -> None:
        self.accounts = accounts
        self.tokens = tokens

    @staticmethod
    def is_configured() -> bool:
        return settings.telegram_enabled

    async def create_link(self, user_id: uuid.UUID) -> IssuedLink:
        """Mint a single-use deep link for an authenticated user.

        Any link the user already holds is expired first: pressing Connect twice must not
        leave a live credential the user believes they discarded.
        """
        now = datetime.now(UTC)
        await self.tokens.invalidate_unused_for_user(user_id, now=now)

        raw = new_link_token()
        ttl = timedelta(minutes=settings.TELEGRAM_LINK_TOKEN_TTL_MINUTES)
        expires_at = now + ttl
        await self.tokens.add(
            TelegramLinkToken(
                user_id=user_id,
                token_hash=hash_link_token(raw),
                expires_at=expires_at,
            )
        )
        log.info("telegram_link_created", user_id=str(user_id))
        return IssuedLink(
            deep_link=f"https://t.me/{settings.TELEGRAM_BOT_USERNAME}?start={raw}",
            expires_at=expires_at,
            expires_in=int(ttl.total_seconds()),
        )

    async def consume(self, raw_token: str, identity: TelegramIdentity) -> LinkOutcome:
        """Spend a link token and bind the Telegram identity to its user.

        Fails closed on every branch, and the token is marked used before the binding is
        written, so a failure part-way through cannot leave a spendable token behind.
        """
        row = await self.tokens.get_by_hash(hash_link_token(raw_token))
        now = datetime.now(UTC)
        if row is None:
            log.warning("telegram_link_rejected", reason="unknown_token")
            return LinkOutcome(LinkResult.invalid_token)
        if row.used_at is not None:
            log.warning("telegram_link_rejected", reason="already_used", user_id=str(row.user_id))
            return LinkOutcome(LinkResult.invalid_token)
        if row.expires_at <= now:
            log.warning("telegram_link_rejected", reason="expired", user_id=str(row.user_id))
            return LinkOutcome(LinkResult.invalid_token)

        existing = await self.accounts.get_by_telegram_user_id(identity.telegram_user_id)
        if existing is not None and existing.user_id != row.user_id:
            # Refuse rather than rebind. The unique index would reject the write anyway;
            # this turns a database error into a decision with a log line.
            log.warning(
                "telegram_link_rejected",
                reason="telegram_id_bound_to_other_user",
                user_id=str(row.user_id),
            )
            await self.tokens.mark_used(row, now=now)
            return LinkOutcome(LinkResult.taken_by_other_user)

        await self.tokens.mark_used(row, now=now)

        if existing is not None:
            self._apply(existing, identity, now)
            account = await self.accounts.add(existing)
            log.info("telegram_relinked", user_id=str(row.user_id))
            return LinkOutcome(LinkResult.already_linked, account)

        # One binding per RecallAI user: re-linking from a different Telegram account
        # replaces the old one rather than accumulating chats that all receive replies.
        prior = await self.accounts.get_for_user(row.user_id)
        if prior is not None:
            await self.accounts.delete(prior)

        account = TelegramAccount(
            user_id=row.user_id,
            telegram_user_id=identity.telegram_user_id,
            telegram_chat_id=identity.chat_id,
        )
        self._apply(account, identity, now)
        account = await self.accounts.add(account)
        log.info("telegram_linked", user_id=str(row.user_id))
        return LinkOutcome(LinkResult.linked, account)

    async def resolve(self, telegram_user_id: str) -> TelegramAccount | None:
        """Turn a Telegram sender into a binding. The only unscoped lookup in the flow."""
        return await self.accounts.get_by_telegram_user_id(telegram_user_id)

    async def get_for_user(self, user_id: uuid.UUID) -> TelegramAccount | None:
        return await self.accounts.get_for_user(user_id)

    async def disconnect(self, user_id: uuid.UUID) -> bool:
        account = await self.accounts.get_for_user(user_id)
        if account is None:
            return False
        await self.accounts.delete(account)
        log.info("telegram_disconnected", user_id=str(user_id))
        return True

    @staticmethod
    def _apply(
        account: TelegramAccount, identity: TelegramIdentity, now: datetime
    ) -> None:
        account.telegram_chat_id = identity.chat_id
        account.username = identity.username
        account.first_name = identity.first_name
        account.last_name = identity.last_name
        account.language_code = identity.language_code
        account.linked_at = now
        account.updated_at = now
