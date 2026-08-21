"""Generic article/webpage extractor (fallback for any http(s) URL).

Lightweight HTML parsing with stdlib only to avoid heavy deps for MVP.
Swap in trafilatura/readability later for better extraction quality.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from app.extractors.base import ExtractedContent, Extractor
from app.models.base import ContentType

_TAG_STRIP = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WS = re.compile(r"\s+")


class _TextHarvester(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.og_image: str | None = None
        self.meta_desc: str | None = None
        self._chunks: list[str] = []
        self._in_title = False
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag in {"script", "style", "noscript"}:
            self._skip = True
        if tag == "meta":
            a = dict(attrs)
            if a.get("property") == "og:image" and not self.og_image:
                self.og_image = a.get("content")
            if a.get("name") == "description" and not self.meta_desc:
                self.meta_desc = a.get("content")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in {"script", "style", "noscript"}:
            self._skip = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()
        elif not self._skip:
            text = data.strip()
            if text:
                self._chunks.append(text)

    @property
    def text(self) -> str:
        return _WS.sub(" ", " ".join(self._chunks)).strip()


class ArticleExtractor(Extractor):
    content_type = ContentType.article

    def can_handle(self, url: str) -> bool:
        return url.startswith("http://") or url.startswith("https://")

    async def extract(self, url: str) -> ExtractedContent:
        headers = {"User-Agent": "RecallAIBot/1.0 (+https://recall.ai)"}
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as c:
            resp = await c.get(url)
            resp.raise_for_status()
            html = resp.text

        html = _TAG_STRIP.sub(" ", html)
        parser = _TextHarvester()
        parser.feed(html)
        body = parser.text[:20000]  # cap before AI; full content not needed for summary

        return ExtractedContent(
            type=self.content_type,
            title=parser.title,
            content=parser.meta_desc + "\n\n" + body if parser.meta_desc else body,
            thumbnail_url=parser.og_image,
            metadata={"description": parser.meta_desc},
        )
