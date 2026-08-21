"""User data access."""
from __future__ import annotations

import uuid

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_provider(self, provider: str, account_id: str) -> User | None:
        result = await self.session.exec(
            select(User).where(
                User.auth_provider == provider,
                User.provider_account_id == account_id,
            )
        )
        return result.first()

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.exec(select(User).where(User.email == email))
        return result.first()

    async def create(self, user: User) -> User:
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user
