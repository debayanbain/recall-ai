"""Collection data access."""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.collection import Collection, CollectionItem
from app.models.vault import VaultItem


class CollectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, collection: Collection) -> Collection:
        self.session.add(collection)
        await self.session.flush()
        await self.session.refresh(collection)
        return collection

    async def get(self, collection_id: uuid.UUID, user_id: uuid.UUID) -> Collection | None:
        c = await self.session.get(Collection, collection_id)
        if c is None or c.user_id != user_id:
            return None
        return c

    async def get_by_slug(self, slug: str) -> Collection | None:
        result = await self.session.exec(select(Collection).where(Collection.slug == slug))
        return result.first()

    async def slug_exists(self, slug: str) -> bool:
        result = await self.session.exec(select(Collection.id).where(Collection.slug == slug))
        return result.first() is not None

    async def list_for_user(self, user_id: uuid.UUID) -> Sequence[Collection]:
        result = await self.session.exec(
            select(Collection)
            .where(Collection.user_id == user_id)
            .order_by(col(Collection.created_at).desc())
        )
        return result.all()

    async def add_item(self, collection_id: uuid.UUID, vault_item_id: uuid.UUID) -> None:
        self.session.add(
            CollectionItem(collection_id=collection_id, vault_item_id=vault_item_id)
        )

    async def list_items(self, collection_id: uuid.UUID) -> Sequence[VaultItem]:
        result = await self.session.exec(
            select(VaultItem)
            .join(CollectionItem, col(CollectionItem.vault_item_id) == col(VaultItem.id))
            .where(CollectionItem.collection_id == collection_id)
            .order_by(col(CollectionItem.added_at).desc())
        )
        return result.all()
