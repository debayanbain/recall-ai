"""Gemini implementation of AIProvider using google-genai SDK."""
from __future__ import annotations

import json
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from app.ai import parsing, prompts
from app.ai.base import AIProvider
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("ai.gemini")

_CATEGORIES = [
    "Technology", "Business", "Science", "Health", "Education",
    "Entertainment", "News", "Productivity", "Finance", "Lifestyle", "Other",
]


class GeminiProvider(AIProvider):
    """Calls Gemini Flash for text tasks and text-embedding-004 for vectors.

    The client is created lazily so importing this module never requires a key.
    """

    def __init__(self) -> None:
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai  # lazy import

            if not settings.GEMINI_API_KEY:
                raise RuntimeError("GEMINI_API_KEY is not configured")
            self._client = genai.Client(api_key=settings.GEMINI_API_KEY)
        return self._client

    # reraise=True: without it tenacity swallows the provider's error and raises its own
    # RetryError, so the failure recorded on the item reads
    # "RetryError[<Future ... raised ClientError>]" instead of the actual cause
    # ("API key not valid"), which is the one thing that makes it fixable.
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    async def _generate(self, prompt: str) -> str:
        client = self._get_client()
        resp = await client.aio.models.generate_content(
            model=settings.GEMINI_TEXT_MODEL, contents=prompt
        )
        return (resp.text or "").strip()

    async def generate_summary(self, text: str) -> str:
        prompt = (
            "Summarize the following content in 2-3 concise sentences. "
            "Be factual and neutral.\n\nCONTENT:\n" + text[:12000]
        )
        return await self._generate(prompt)

    async def generate_tags(self, text: str) -> list[str]:
        prompt = (
            "Extract 3-7 short topical tags from this content. "
            'Respond ONLY with a JSON array of lowercase strings, e.g. ["ai","startups"].'
            "\n\nCONTENT:\n" + text[:12000]
        )
        raw = await self._generate(prompt)
        return self._parse_tags(raw)

    async def generate_label(self, text: str) -> str:
        return parsing.clean_label(await self._generate(prompts.label_prompt(text)))

    async def generate_highlights(self, text: str) -> list[str]:
        return parsing.parse_string_list(
            await self._generate(prompts.highlights_prompt(text))
        )

    async def generate_category(self, text: str) -> str:
        prompt = (
            "Classify this content into exactly one category from: "
            + ", ".join(_CATEGORIES)
            + ". Respond with ONLY the category word.\n\nCONTENT:\n"
            + text[:8000]
        )
        raw = (await self._generate(prompt)).strip().strip(".")
        return raw if raw in _CATEGORIES else "Other"

    # reraise=True: without it tenacity swallows the provider's error and raises its own
    # RetryError, so the failure recorded on the item reads
    # "RetryError[<Future ... raised ClientError>]" instead of the actual cause
    # ("API key not valid"), which is the one thing that makes it fixable.
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    async def generate_embedding(self, text: str) -> list[float]:
        client = self._get_client()
        resp = await client.aio.models.embed_content(
            model=settings.GEMINI_EMBED_MODEL, contents=text[:12000]
        )
        vec = list(resp.embeddings[0].values)
        return self._fit_dim(vec)

    @staticmethod
    def _parse_tags(raw: str) -> list[str]:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return [str(t).lower().strip() for t in data if str(t).strip()][:7]
        except json.JSONDecodeError:
            log.warning("tag_parse_failed", raw=raw[:200])
        return []

    @staticmethod
    def _fit_dim(vec: list[float]) -> list[float]:
        """Pad/truncate embedding to the configured column dimension."""
        target = settings.EMBEDDING_DIM
        if len(vec) == target:
            return vec
        if len(vec) > target:
            return vec[:target]
        return vec + [0.0] * (target - len(vec))
