"""YouTube extractor using the public oEmbed endpoint (no API key needed)."""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.net import assert_safe_url
from app.extractors.base import ExtractedContent, Extractor
from app.models.base import ContentType

_YT_RE = re.compile(
    r"(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)", re.IGNORECASE
)


class YouTubeExtractor(Extractor):
    content_type = ContentType.youtube

    def can_handle(self, url: str) -> bool:
        return bool(_YT_RE.search(url))

    async def extract(self, url: str) -> ExtractedContent:
        oembed = "https://www.youtube.com/oembed"
        # oembed is a fixed YouTube endpoint, but the `url` parameter is user-supplied
        # and YouTube echoes/redirects on it, so validate before handing it over.
        assert_safe_url(url)
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(oembed, params={"url": url, "format": "json"})
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

        title = data.get("title")
        author = data.get("author_name")
        content_text = f"{title or ''} by {author or ''}".strip()
        return ExtractedContent(
            type=self.content_type,
            title=title,
            content=content_text,
            thumbnail_url=data.get("thumbnail_url"),
            metadata={
                "author": author,
                "provider": data.get("provider_name"),
                "html": data.get("html"),
            },
        )
