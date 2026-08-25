"""Extractor registry selection tests (no network)."""
from __future__ import annotations

import pytest

from app.extractors import get_extractor
from app.extractors.article import ArticleExtractor
from app.extractors.youtube import YouTubeExtractor
from app.models.base import ContentType


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://youtube.com/shorts/abc123",
    ],
)
def test_youtube_urls_route_to_youtube(url: str) -> None:
    assert isinstance(get_extractor(url), YouTubeExtractor)


def test_generic_url_falls_back_to_article() -> None:
    extractor = get_extractor("https://example.com/some-post")
    assert isinstance(extractor, ArticleExtractor)
    assert extractor.content_type is ContentType.article


def test_unsupported_scheme_raises() -> None:
    with pytest.raises(ValueError):
        get_extractor("ftp://example.com/file")
