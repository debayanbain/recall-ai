"""Chat-model selection, mirroring `app/ai/factory.py`.

Same setting (`AI_PROVIDER`), same lru_cache, same "unsupported provider is an error, not
a fallback" rule -- a silent fallback would answer a user's question using a model they
did not configure and a key they may not have paid for.
"""
from __future__ import annotations

from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.core.config import settings

# Low but not zero: the answer is grounded in retrieved text, so creativity is a liability,
# while a completely deterministic model phrases every "nothing found" identically.
_TEMPERATURE = 0.2
_TIMEOUT = 30.0
_MAX_RETRIES = 2


def chat_available() -> bool:
    """True when the configured provider has a key. Checked before any chat feature."""
    if settings.AI_PROVIDER == "openai":
        return bool(settings.OPENAI_API_KEY)
    if settings.AI_PROVIDER == "gemini":
        return bool(settings.GEMINI_API_KEY)
    return False


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    if settings.AI_PROVIDER == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.OPENAI_TEXT_MODEL,
            api_key=settings.OPENAI_API_KEY,
            temperature=_TEMPERATURE,
            timeout=_TIMEOUT,
            max_retries=_MAX_RETRIES,
        )
    if settings.AI_PROVIDER == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_TEXT_MODEL,
            google_api_key=settings.GEMINI_API_KEY,
            temperature=_TEMPERATURE,
            timeout=_TIMEOUT,
            max_retries=_MAX_RETRIES,
        )
    raise ValueError(f"No chat model for AI_PROVIDER={settings.AI_PROVIDER!r}")
