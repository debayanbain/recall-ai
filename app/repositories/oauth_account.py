"""Linked-identity data access."""
from __future__ import annotations

import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.oauth_account import OAuthAccount


class OAuthAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_provider(self, provider: str, account_id: str) -> OAuthAccount | None:
        result = await self.session.exec(
            select(OAuthAccount).where(
                OAuthAccount.provider == provider,
                OAuthAccount.provider_account_id == account_id,
            )
        )
        return result.first()

    async def list_for_user(self, user_id: uuid.UUID) -> list[OAuthAccount]:
        result = await self.session.exec(
            select(OAuthAccount).where(OAuthAccount.user_id == user_id)
        )
        return list(result.all())

    async def add(self, account: OAuthAccount) -> OAuthAccount:
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account
