"""Telegram linkage data access.

`TelegramAccountRepository.get_by_telegram_user_id` is deliberately *not* scoped by
`user_id` -- it is the lookup that establishes which user a message belongs to, so it has
nothing to scope by yet. It is the one entry point that turns an unauthenticated Telegram
sender into a RecallAI identity, and every other read here is scoped by the `user_id` it
returns.

Link tokens are addressed only by digest or by `user_id`; there is no lookup by row id,
so no object reference reaches a caller to tamper with.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.telegram import TelegramAccount, TelegramLinkToken


class TelegramAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_user_id(self, telegram_user_id: str) -> TelegramAccount | None:
        """Resolve a Telegram sender to their binding. The unique index makes this exact."""
        result = await self.session.exec(
            select(TelegramAccount).where(
                TelegramAccount.telegram_user_id == telegram_user_id
            )
        )
        return result.first()

    async def get_for_user(self, user_id: uuid.UUID) -> TelegramAccount | None:
        result = await self.session.exec(
            select(TelegramAccount).where(TelegramAccount.user_id == user_id)
        )
        return result.first()

    async def add(self, account: TelegramAccount) -> TelegramAccount:
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account

    async def delete(self, account: TelegramAccount) -> None:
        await self.session.delete(account)


class TelegramLinkTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, token: TelegramLinkToken) -> TelegramLinkToken:
        self.session.add(token)
        await self.session.flush()
        await self.session.refresh(token)
        return token

    async def get_by_hash(self, token_hash: str) -> TelegramLinkToken | None:
        """Look a presented link token up by digest.

        Used and expired rows are returned too, so the caller can log *why* a redemption
        failed even though the user is told only that the link expired.
        """
        result = await self.session.exec(
            select(TelegramLinkToken).where(TelegramLinkToken.token_hash == token_hash)
        )
        return result.first()

    async def invalidate_unused_for_user(self, user_id: uuid.UUID, *, now: datetime) -> int:
        """Expire a user's outstanding links before minting a new one.

        Pressing Connect twice must not leave two live tokens: the first one is then a
        credential the user believes they discarded.
        """
        result = await self.session.execute(
            update(TelegramLinkToken)
            .where(
                col(TelegramLinkToken.user_id) == user_id,
                col(TelegramLinkToken.used_at).is_(None),
                col(TelegramLinkToken.expires_at) > now,
            )
            .values(expires_at=now)
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def mark_used(self, token: TelegramLinkToken, *, now: datetime) -> None:
        token.used_at = now
        self.session.add(token)
        await self.session.flush()

    async def delete_expired(self, before: datetime) -> int:
        """Drop tokens that can no longer prove anything, keeping the table bounded."""
        result = await self.session.execute(
            delete(TelegramLinkToken).where(col(TelegramLinkToken.expires_at) < before)
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)
