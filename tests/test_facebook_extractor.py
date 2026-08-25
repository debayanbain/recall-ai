"""Facebook reels: routing and payload mapping."""
from __future__ import annotations

from typing import Any

import pytest

from app.extractors.base import PermanentExtractionError
from app.extractors.facebook import FacebookReelExtractor
from app.extractors.registry import get_extractor
from app.models.base import ContentType

REEL: dict[str, Any] = {
    "text": "Watch this before you buy a laptop\nFull guide in comments",
    "pageName": "TechDaily",
    "videoUrl": "https://video.fb/reel.mp4",
    "thumbnailUrl": "https://scontent.fb/thumb.jpg",
    "viewsCount": 120000,
    "likesCount": 4300,
    "comments": [{"text": "so helpful"}, {"text": "saved"}],
}


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/reel/1234567890",
        "https://fb.watch/aBcDeF/",
        "https://www.facebook.com/share/r/abc123/",
        "https://www.facebook.com/techdaily/videos/998877/",
    ],
)
def test_reel_urls_route_here(url: str) -> None:
    assert isinstance(get_extractor(url), FacebookReelExtractor), url


def test_a_plain_facebook_page_is_not_claimed() -> None:
    """A profile or page is not a memory; it falls to the article fallback."""
    assert not FacebookReelExtractor().can_handle("https://www.facebook.com/techdaily/")


def test_caption_owner_and_comments_reach_the_ai() -> None:
    out = FacebookReelExtractor().build([REEL])
    assert out.type is ContentType.facebook
    body = out.content or ""
    assert "Watch this before you buy a laptop" in body
    assert "TechDaily" in body
    assert "so helpful" in body


def test_title_is_the_caption_first_line() -> None:
    assert FacebookReelExtractor().build([REEL]).title == (
        "Watch this before you buy a laptop"
    )


def test_metadata_is_kept_structured() -> None:
    meta = FacebookReelExtractor().build([REEL]).metadata
    assert meta["is_video"] is True
    assert meta["views"] == 120000
    assert meta["video_url"].endswith(".mp4")


def test_field_names_are_probed_leniently() -> None:
    """Actor payloads differ between store actors, so each field has fallbacks."""
    out = FacebookReelExtractor().build(
        [{"caption": "alt field name", "authorName": "Someone", "views": 5}]
    )
    assert "alt field name" in (out.content or "")
    assert out.metadata["views"] == 5


def test_empty_result_is_permanent() -> None:
    with pytest.raises(PermanentExtractionError, match="private"):
        FacebookReelExtractor().build([])
