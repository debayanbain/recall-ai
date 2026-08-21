"""Extractor strategy protocol and shared result type.

Platform logic lives ONLY in extractors. Never in API routes or workers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.models.base import ContentType


@dataclass(slots=True)
class ExtractedContent:
    """Normalized output of any extractor."""

    type: ContentType
    title: str | None = None
    content: str | None = None
    thumbnail_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Extractor(Protocol):
    """A strategy that knows how to pull content from one source family."""

    content_type: ContentType

    def can_handle(self, url: str) -> bool:
        """Return True if this extractor recognizes the URL."""
        ...

    async def extract(self, url: str) -> ExtractedContent:
        """Fetch and normalize content for the URL."""
        ...
