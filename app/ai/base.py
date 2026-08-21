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

    async def generate_category(self, text: str) -> str:
        ...

    async def generate_embedding(self, text: str) -> list[float]:
        ...
