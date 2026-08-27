"""Extractor registry — selects the right strategy for a URL.

Order matters: specific extractors first, ArticleExtractor last as fallback.
"""
from __future__ import annotations

from app.extractors.article import ArticleExtractor
from app.extractors.base import AnyExtractor
from app.extractors.facebook import FacebookReelApifyExtractor, FacebookReelExtractor
from app.extractors.instagram import InstagramPostExtractor, InstagramReelExtractor
from app.extractors.youtube import YouTubeExtractor

# Future: TikTokExtractor, LinkedInExtractor — append above ArticleExtractor.
_EXTRACTORS: list[AnyExtractor] = [
    YouTubeExtractor(),
    InstagramReelExtractor(),   # paid: Apify
    InstagramPostExtractor(),   # free: saved as a link, no scrape
    FacebookReelApifyExtractor(),  # paid: Apify -- claims nothing unless FACEBOOK_USE_APIFY
    FacebookReelExtractor(),    # free: Open Graph tags off the reel page
    ArticleExtractor(),         # fallback — keep last
]


def get_extractor(url: str) -> AnyExtractor:
    """Return the first extractor that can handle the URL."""
    for extractor in _EXTRACTORS:
        if extractor.can_handle(url):
            return extractor
    raise ValueError(f"No extractor available for URL: {url}")
