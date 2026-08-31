"""OpenAI implementation of AIProvider.

Mirrors `GeminiProvider` deliberately: same prompts, same defensive parsing, same retry
policy. The only structural difference is embeddings — `text-embedding-3-small` emits
1536 dimensions natively, matching the `Vector(1536)` column exactly, so nothing is
zero-padded the way Gemini's 768-dim output has to be.
"""
from __future__ import annotations

import json
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from app.ai import parsing, prompts
from app.ai.base import AIProvider
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("ai.openai")

_CATEGORIES = [
    "Technology", "Business", "Science", "Health", "Education",
    "Entertainment", "News", "Productivity", "Finance", "Lifestyle", "Other",
]


class OpenAIProvider(AIProvider):
    """Chat Completions for text tasks, embeddings for vectors.

    The client is created lazily so importing this module never requires a key.
    """

    def __init__(self) -> None:
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import AsyncOpenAI  # lazy import

            if not settings.OPENAI_API_KEY:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            self._client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        return self._client

    # reraise=True so the recorded failure is the provider's own message (e.g. an invalid
    # key or a rate limit) rather than tenacity's opaque RetryError wrapper.
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    async def _generate(self, prompt: str, max_tokens: int = 400) -> str:
        client = self._get_client()
        resp = await client.chat.completions.create(
            model=settings.OPENAI_TEXT_MODEL,
            messages=[{"role": "user", "content": prompt}],
            # Low but non-zero: tag extraction benefits from a little variety, and
            # summaries should not drift between runs of the same content.
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    async def generate_summary(self, text: str) -> str:
        prompt = (
            "Summarize the following content in 2-3 concise sentences. "
            "Be factual and neutral. "
            # Without this the summary comes back in English whatever the note was
            # written in, so a Bengali voice note gets an English card and the person
            # who recorded it reads their own memory in translation.
            "Write the summary in the SAME LANGUAGE as the content."
            "\n\nCONTENT:\n" + text[:12000]
        )
        return await self._generate(prompt)

    async def generate_tags(self, text: str) -> list[str]:
        prompt = (
            "Extract 3-7 short topical tags from this content. "
            # Tags are shown on the person's own card and typed into their own search
            # box, so they belong in the language they wrote in. The cost is a split tag
            # space -- "jobs" and "চাকরি" never match -- which is real but is the same
            # split their notes already have.
            "Use the SAME LANGUAGE as the content. "
            'Respond ONLY with a JSON array of lowercase strings, e.g. ["ai","startups"].'
            "\n\nCONTENT:\n" + text[:12000]
        )
        raw = await self._generate(prompt, max_tokens=120)
        return self._parse_tags(raw)

    async def generate_label(self, text: str) -> str:
        raw = await self._generate(prompts.label_prompt(text), max_tokens=32)
        return parsing.clean_label(raw)

    async def generate_highlights(self, text: str) -> list[str]:
        raw = await self._generate(prompts.highlights_prompt(text), max_tokens=500)
        return parsing.parse_string_list(raw)

    async def generate_category(self, text: str) -> str:
        prompt = (
            "Classify this content into exactly one category from: "
            + ", ".join(_CATEGORIES)
            # The answer is checked against that exact list and anything else becomes
            # "Other", so a model that helpfully translates the category for Bengali
            # content silently drops the item into the catch-all. This is the one field
            # that must stay English -- it is an enum, not prose.
            + ". Answer with the English word from that list exactly as written, "
            "whatever language the content is in. Respond with ONLY the category word."
            "\n\nCONTENT:\n"
            + text[:8000]
        )
        raw = (await self._generate(prompt, max_tokens=10)).strip().strip(".")
        return raw if raw in _CATEGORIES else "Other"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10), reraise=True)
    async def generate_embedding(self, text: str) -> list[float]:
        client = self._get_client()
        resp = await client.embeddings.create(
            model=settings.OPENAI_EMBED_MODEL,
            input=text[:12000],
        )
        return self._fit_dim(list(resp.data[0].embedding))

    @staticmethod
    def _parse_tags(raw: str) -> list[str]:
        """Models return prose and code fences even when told not to. Strip both."""
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return [str(t).lower().strip() for t in data if str(t).strip()][:7]
        except json.JSONDecodeError:
            log.warning("tag_parse_failed", raw=raw[:200])
        # Fall back to comma-splitting rather than losing the tags entirely.
        parts = [p.strip().strip('"[]').lower() for p in cleaned.split(",")]
        return [p for p in parts if p and len(p) < 40][:7]

    @staticmethod
    def _fit_dim(vec: list[float]) -> list[float]:
        """Pad/truncate to the column width.

        A no-op for text-embedding-3-small (1536 = EMBEDDING_DIM), but kept so swapping
        OPENAI_EMBED_MODEL cannot silently write vectors the column will reject.
        """
        target = settings.EMBEDDING_DIM
        if len(vec) == target:
            return vec
        if len(vec) > target:
            return vec[:target]
        return vec + [0.0] * (target - len(vec))
