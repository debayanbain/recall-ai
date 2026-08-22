"""Collection use-cases: create, list, add item, public lookup, slug generation."""
from __future__ import annotations

import secrets
import uuid
from collections.abc import Sequence

from slugify import slugify

from app.models.base import Visibility
from app.models.collection import Collection
from app.models.vault import VaultItem
from app.repositories.collection import CollectionRepository
from app.repositories.vault import VaultRepository


class CollectionService:
    def __init__(self, repo: CollectionRepository, vault_repo: VaultRepository) -> None:
        self.repo = repo
        self.vault_repo = vault_repo

    async def create(
        self,
        user_id: uuid.UUID,
        name: str,
        description: str | None,
        visibility: Visibility,
    ) -> Collection:
        slug = await self._unique_slug(name)
        collection = Collection(
            user_id=user_id,
            name=name,
            slug=slug,
            description=description,
            visibility=visibility,
        )
        return await self.repo.add(collection)

    async def list(self, user_id: uuid.UUID) -> Sequence[Collection]:
        return await self.repo.list_for_user(user_id)

    async def add_item(
        self, collection_id: uuid.UUID, user_id: uuid.UUID, vault_item_id: uuid.UUID
    ) -> bool:
        collection = await self.repo.get(collection_id, user_id)
        if collection is None:
            return False
        # Owning the container must not grant access to someone else's content: the
        # collection's items are echoed back by the detail route and, once the
        # collection is public, by the unauthenticated share route as well.
        if await self.vault_repo.get(vault_item_id, user_id) is None:
            return False
        await self.repo.add_item(collection_id, vault_item_id)
        return True

    async def get_with_items(
        self, collection_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[Collection, Sequence[VaultItem]] | None:
        collection = await self.repo.get(collection_id, user_id)
        if collection is None:
            return None
        items = await self.repo.list_items(collection_id)
        return collection, items

    async def get_public(
        self, slug: str
    ) -> tuple[Collection, Sequence[VaultItem]] | None:
        collection = await self.repo.get_by_slug(slug)
        if collection is None or collection.visibility != Visibility.public:
            return None
        items = await self.repo.list_items(collection.id)
        return collection, items

    async def _unique_slug(self, name: str) -> str:
        base = slugify(name)[:80] or "collection"
        slug = base
        while await self.repo.slug_exists(slug):
            slug = f"{base}-{secrets.token_hex(3)}"
        return slug
