"""Extractor registry — selects the right strategy for a URL.

Order matters: specific extractors first, ArticleExtractor last as fallback.
"""
from __future__ import annotations

from app.extractors.article import ArticleExtractor
from app.extractors.base import Extractor
from app.extractors.youtube import YouTubeExtractor

# Future: InstagramExtractor, TikTokExtractor, LinkedInExtractor — append here.
_EXTRACTORS: list[Extractor] = [
    YouTubeExtractor(),
    ArticleExtractor(),  # fallback — keep last
]


def get_extractor(url: str) -> Extractor:
    """Return the first extractor that can handle the URL."""
    for extractor in _EXTRACTORS:
        if extractor.can_handle(url):
            return extractor
    raise ValueError(f"No extractor available for URL: {url}")
