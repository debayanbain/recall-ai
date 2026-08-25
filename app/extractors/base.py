"""Extractor strategy protocol and shared result type.

Platform logic lives ONLY in extractors. Never in API routes or workers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.models.base import ContentType


class PermanentExtractionError(Exception):
    """A failure that retrying cannot fix.

    ARQ retries a failed job four times. That is right for a timeout or a 5xx, and wrong
    for a deleted post, a private account or a rejected API key -- those burn paid
    third-party credits four times to reach the same answer. Raising this tells the worker
    to record the failure and stop.
    """


@dataclass(slots=True)
class ExtractedContent:
    """Normalized output of any extractor."""

    type: ContentType
    title: str | None = None
    content: str | None = None
    thumbnail_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: False when there is deliberately nothing to summarize — the item is saved as a
    #: bare link and marked `skipped` rather than failed. Used by sources we choose not
    #: to pay a scraper for; without it, "no content" is indistinguishable from a bug.
    enrich: bool = True


@runtime_checkable
class DeferredExtractor(Protocol):
    """An extractor whose fetch happens off-box and finishes later.

    Splitting `extract` into `start` + `build` is what stops a worker sitting on a
    multi-minute crawl. `start` triggers the provider and returns its run id in about a
    second; the provider announces completion by webhook (or the sweeper notices), and
    `build` maps the delivered payload — never re-fetching, so this half is pure.

    Fast sources (an article, a YouTube oEmbed) stay on the plain `Extractor` protocol:
    deferring a 300ms request would add a round trip and a state machine for nothing.
    """

    content_type: ContentType
    #: Marks this extractor as two-phase for `ProcessingService`.
    deferred: bool

    def can_handle(self, url: str) -> bool:
        ...

    async def start(self, url: str) -> str:
        """Trigger the run and return the provider's run id. Must not wait for results."""
        ...

    async def fetch_dataset(self, dataset_id: str) -> list[dict[str, Any]]:
        """Read a finished run's items. Called after the callback, never from `build`."""
        ...

    def build(self, items: list[dict[str, Any]]) -> ExtractedContent:
        """Map a finished payload. Pure — no I/O, so it is trivially testable."""
        ...


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


#: Either kind. `ProcessingService` branches on `getattr(x, "deferred", False)`.
AnyExtractor = Extractor | DeferredExtractor
