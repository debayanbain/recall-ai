"""VaultItem data access including search and chunk embeddings."""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlmodel import col, func, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.vault import VaultChunk, VaultItem


class VaultRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, item: VaultItem) -> VaultItem:
        self.session.add(item)
        await self.session.flush()
        await self.session.refresh(item)
        return item

    async def get(self, item_id: uuid.UUID, user_id: uuid.UUID) -> VaultItem | None:
        item = await self.session.get(VaultItem, item_id)
        if item is None or item.user_id != user_id:
            return None
        return item

    async def get_unscoped(self, item_id: uuid.UUID) -> VaultItem | None:
        """For workers: fetch without user scoping."""
        return await self.session.get(VaultItem, item_id)

    async def list_for_user(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        base = select(VaultItem).where(
            VaultItem.user_id == user_id,
            col(VaultItem.deleted_at).is_(None),
        )
        total = await self.session.exec(
            select(func.count()).select_from(base.subquery())
        )
        rows = await self.session.exec(
            base.order_by(col(VaultItem.created_at).desc()).limit(limit).offset(offset)
        )
        return rows.all(), total.one()

    async def search(
        self, user_id: uuid.UUID, query: str, limit: int = 20, offset: int = 0
    ) -> tuple[Sequence[VaultItem], int]:
        """Phase 1 search: case-insensitive ILIKE over title/summary/content."""
        pattern = f"%{query}%"
        base = select(VaultItem).where(
            VaultItem.user_id == user_id,
            col(VaultItem.deleted_at).is_(None),
            or_(
                col(VaultItem.title).ilike(pattern),
                col(VaultItem.summary).ilike(pattern),
                col(VaultItem.content).ilike(pattern),
            ),
        )
        total = await self.session.exec(
            select(func.count()).select_from(base.subquery())
        )
        rows = await self.session.exec(
            base.order_by(col(VaultItem.created_at).desc()).limit(limit).offset(offset)
        )
        return rows.all(), total.one()

    async def delete(self, item: VaultItem) -> None:
        await self.session.delete(item)

    async def upsert_chunk(
        self,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        vector: list[float],
        content: str,
        chunk_index: int = 0,
        token_count: int | None = None,
    ) -> None:
        result = await self.session.exec(
            select(VaultChunk).where(
                VaultChunk.vault_item_id == item_id,
                VaultChunk.chunk_index == chunk_index,
            )
        )
        existing = result.first()
        if existing is None:
            self.session.add(
                VaultChunk(
                    vault_item_id=item_id,
                    user_id=user_id,
                    chunk_index=chunk_index,
                    content=content,
                    embedding=vector,
                    token_count=token_count,
                )
            )
        else:
            existing.embedding = vector
            existing.content = content
            existing.token_count = token_count
            self.session.add(existing)
