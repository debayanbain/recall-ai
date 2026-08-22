"""Connected-Instagram-account data access.

Every read is scoped by `user_id`: an integration row is as sensitive as the vault it
feeds, and a lookup by id alone would be an IDOR straight to someone else's Page token.
"""
from __future__ import annotations

import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.instagram_account import InstagramAccount


class InstagramAccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_user(self, user_id: uuid.UUID) -> list[InstagramAccount]:
        result = await self.session.exec(
            select(InstagramAccount)
            .where(InstagramAccount.user_id == user_id)
            .order_by(InstagramAccount.created_at)  # type: ignore[arg-type]
        )
        return list(result.all())

    async def get(
        self, account_id: uuid.UUID, user_id: uuid.UUID
    ) -> InstagramAccount | None:
        """Tenant-scoped fetch. Returns None on a mismatch rather than raising."""
        result = await self.session.exec(
            select(InstagramAccount).where(
                InstagramAccount.id == account_id,
                InstagramAccount.user_id == user_id,
            )
        )
        return result.first()

    async def get_by_instagram_id(
        self, user_id: uuid.UUID, instagram_user_id: str
    ) -> InstagramAccount | None:
        result = await self.session.exec(
            select(InstagramAccount).where(
                InstagramAccount.user_id == user_id,
                InstagramAccount.instagram_user_id == instagram_user_id,
            )
        )
        return result.first()

    async def add(self, account: InstagramAccount) -> InstagramAccount:
        self.session.add(account)
        await self.session.flush()
        await self.session.refresh(account)
        return account

    async def delete(self, account: InstagramAccount) -> None:
        await self.session.delete(account)
