"""A LangChain retriever backed by the vault's own pgvector index.

Not a LangChain vector store. The vectors live in `vault_chunks` with the schema, indexes
and tenant column this application already owns, and a store abstraction on top would
either duplicate that or quietly bypass it. Tenant scoping stays where the project puts
it -- in the repository -- so this class holds a `user_id` and passes it down rather than
assembling a filter of its own.

The query embedding comes from `AIProvider`, the same code that wrote the stored vectors.
Using LangChain's embeddings here would compare a fresh vector against a space it was not
drawn from: under Gemini the stored ones are 768 dims zero-padded to 1536, so the result
would be plausible ordering over noise rather than an error anyone would notice.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from langchain_core.callbacks import (
    AsyncCallbackManagerForRetrieverRun,
    CallbackManagerForRetrieverRun,
)
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict

from app.ai import get_ai_provider
from app.models.base import ContentType
from app.repositories.vault import VaultRepository

#: Enough for the model to answer from, short enough that eight of them stay in budget.
_MAX_EXCERPT = 600


class VaultRetriever(BaseRetriever):
    """Nearest-neighbour lookup over one user's memories."""

    # BaseRetriever is a pydantic model; the repository and UUID are not pydantic types.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    repo: VaultRepository
    user_id: uuid.UUID
    limit: int = 8
    created_after: datetime | None = None
    content_types: Sequence[ContentType] | None = None
    category: str | None = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        # Every caller is async. A sync path would need its own event loop inside a
        # Celery child that already has one, so it is refused rather than faked.
        raise NotImplementedError("VaultRetriever is async-only; use ainvoke")

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: AsyncCallbackManagerForRetrieverRun
    ) -> list[Document]:
        vector = await get_ai_provider().generate_embedding(query)
        rows = await self.repo.search_semantic(
            self.user_id,
            vector,
            limit=self.limit,
            created_after=self.created_after,
            content_types=self.content_types,
            category=self.category,
        )
        return [to_document(item, distance) for item, distance in rows]


def to_document(item: Any, distance: float | None = None) -> Document:
    """One vault item as a Document, with the metadata the answer needs to cite it."""
    body = item.summary or item.content or item.title or ""
    return Document(
        page_content=str(body)[:_MAX_EXCERPT],
        metadata={
            "item_id": str(item.id),
            "title": item.title or item.source_url or "Untitled",
            "source_url": item.source_url,
            "ai_category": item.ai_category,
            "ai_tags": list(item.ai_tags or []),
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "distance": distance,
        },
    )
