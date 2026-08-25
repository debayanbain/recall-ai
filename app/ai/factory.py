"""AI provider factory — pick implementation from settings."""
from __future__ import annotations

from functools import lru_cache

from app.ai.base import AIProvider
from app.core.config import settings


@lru_cache
def get_ai_provider() -> AIProvider:
    if settings.AI_PROVIDER == "gemini":
        from app.ai.gemini import GeminiProvider

        return GeminiProvider()
    if settings.AI_PROVIDER == "openai":
        from app.ai.openai import OpenAIProvider

        return OpenAIProvider()
    # Future: claude
    raise ValueError(f"Unsupported AI provider: {settings.AI_PROVIDER}")
