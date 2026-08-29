"""One vault row as a LangChain `Document`.

Deliberately not a LangChain vector store and no longer a retriever either: the lookup
itself is `services/chat_engine/retrieval.py`, which returns rows and knows nothing about
LangChain. What is left here is the adapter between the two -- the vectors live in
`vault_chunks` with the schema, indexes and tenant column this application already owns,
and a store abstraction on top would either duplicate that or quietly bypass it.
"""
from __future__ import annotations

from typing import Any

from langchain_core.documents import Document

from app.services.chat_engine.cards import short_id

#: Enough for the model to answer from, short enough that eight of them stay in budget.
_MAX_EXCERPT = 600


def to_document(
    item: Any, distance: float | None = None, body: str | None = None
) -> Document:
    """One vault item as a Document, with the metadata the answer needs to cite it.

    `body` is an already-rendered block the prompt layer should use verbatim instead of
    building a card -- how a detail question gets the item's own text. Left unset it is
    the ordinary path, and `page_content` is only the fallback excerpt for a Document
    that reaches the prompt from somewhere other than here.
    """
    excerpt = item.summary or item.content or item.title or ""
    return Document(
        page_content=str(excerpt)[:_MAX_EXCERPT],
        metadata={
            "body": body,
            # The row itself, so `chain.format_context` can render a card from every
            # field rather than re-deriving one from this flattened metadata (which
            # carries no ai_label, no highlights and no summary). Nothing serializes a
            # Document, so an ORM object in here never has to survive a round trip.
            "item": item,
            "item_id": str(item.id),
            # The handle the prompt labels this block with, and the only thing the
            # answer validator will accept as a citation. Derived from the same
            # function the card uses, so the two cannot drift apart.
            "short_id": short_id(item),
            "title": item.title or item.source_url or "Untitled",
            "source_url": item.source_url,
            "ai_category": item.ai_category,
            "ai_tags": list(item.ai_tags or []),
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "distance": distance,
        },
    )
