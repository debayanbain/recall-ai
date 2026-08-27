"""AI provider abstraction. Business logic must never depend on a concrete provider."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class AIProvider(Protocol):
    """Contract every AI backend (Gemini, OpenAI, Claude) must satisfy."""

    async def generate_summary(self, text: str) -> str:
        ...

    async def generate_tags(self, text: str) -> list[str]:
        ...

    async def generate_label(self, text: str) -> str:
        """One short, distinctive name for *this* memory.

        Tags are topical and collide by design ("jobs" belongs to hundreds of items);
        this is the line that tells two saved memories apart in a list, so it names the
        specific thing rather than the subject area.
        """
        ...

    async def generate_highlights(self, text: str) -> list[str]:
        """Verbatim key sentences, quoted exactly so the UI can mark them in place.

        The caller drops anything that is not actually present in the source (see
        `app.ai.spans.keep_verbatim`) — a paraphrase cannot be highlighted, and silently
        showing one as if it were the author's words would be a fabrication.
        """
        ...

    async def generate_category(self, text: str) -> str:
        ...

    async def generate_embedding(self, text: str) -> list[float]:
        ...
