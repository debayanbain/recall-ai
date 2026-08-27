"""Facebook reels: routing, Open Graph parsing, and Apify payload mapping."""
from __future__ import annotations

from typing import Any

import pytest

from app.extractors.base import PermanentExtractionError
from app.extractors.facebook import (
    FacebookReelApifyExtractor,
    FacebookReelExtractor,
)
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

# Trimmed from a live fetch of https://www.facebook.com/share/r/1DDjhJGVif/ — Facebook
# puts the *whole* caption in og:title behind an engagement prefix, and truncates
# og:description at ~200 chars, which is why the parser prefers the former.
PAGE = """<html><head>
<meta property="og:type" content="video.other" />
<meta property="og:title" content="3.7K views &#183; 1.4K reactions | GLOBAL JOB SERIES: SWEDEN
Looking to build your career in Sweden? | Deepak On Board" />
<meta property="og:description" content="GLOBAL JOB SERIES: SWEDEN Looking to build..." />
<meta property="og:url"
 content="https://www.facebook.com/deepakonboard/videos/global-job/1061288983287554/" />
<meta property="og:image" content="https://scontent.fb/thumb.jpg" />
</head><body>irrelevant</body></html>"""

LOGIN_WALL = "<html><head><title>Facebook</title></head><body>Log in</body></html>"


def _og(monkeypatch: pytest.MonkeyPatch, html: str) -> FacebookReelExtractor:
    """An extractor whose network call is replaced by a canned page."""

    async def fake_fetch(url: str) -> tuple[str, str]:
        return html, url

    extractor = FacebookReelExtractor()
    monkeypatch.setattr(extractor, "_fetch", fake_fetch)
    return extractor


@pytest.mark.parametrize(
    "url",
    [
        "https://www.facebook.com/reel/1234567890",
        "https://fb.watch/aBcDeF/",
        "https://www.facebook.com/share/r/abc123/",
        "https://www.facebook.com/techdaily/videos/998877/",
    ],
)
def test_reel_urls_route_to_the_free_extractor(url: str) -> None:
    """FACEBOOK_USE_APIFY is off by default, so the Open Graph path owns these."""
    assert isinstance(get_extractor(url), FacebookReelExtractor), url


def test_a_plain_facebook_page_is_not_claimed() -> None:
    """A profile or page is not a memory; it falls to the article fallback."""
    assert not FacebookReelExtractor().can_handle("https://www.facebook.com/techdaily/")


def test_apify_extractor_claims_nothing_while_the_flag_is_off() -> None:
    assert not FacebookReelApifyExtractor().can_handle(
        "https://www.facebook.com/reel/1234567890"
    )


# --- Open Graph path ---------------------------------------------------------------


async def test_caption_and_owner_reach_the_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    out = await _og(monkeypatch, PAGE).extract("https://www.facebook.com/share/r/x/")
    assert out.type is ContentType.facebook
    body = out.content or ""
    assert "GLOBAL JOB SERIES: SWEDEN" in body
    assert "Looking to build your career in Sweden?" in body
    assert "Deepak On Board" in body
    assert out.thumbnail_url == "https://scontent.fb/thumb.jpg"


async def test_engagement_prefix_is_stripped_and_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counts belong in metadata, not glued to the front of the title."""
    out = await _og(monkeypatch, PAGE).extract("https://www.facebook.com/share/r/x/")
    assert out.title == "GLOBAL JOB SERIES: SWEDEN"
    assert out.metadata["views"] == 3700
    assert out.metadata["reactions"] == 1400
    assert out.metadata["owner"] == "Deepak On Board"
    assert out.metadata["source"] == "opengraph"


async def test_page_name_suffix_is_only_trimmed_when_it_matches_the_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caption may itself contain " | " — dropping that text would lose content."""
    page = PAGE.replace("| Deepak On Board", "| part two of three")
    out = await _og(monkeypatch, page).extract("https://www.facebook.com/share/r/x/")
    assert "part two of three" in (out.content or "")
    assert out.metadata["owner"] == "deepakonboard"  # falls back to the URL slug


async def test_a_login_wall_is_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No og tags means no preview; retrying spends the same nothing three more times."""
    with pytest.raises(PermanentExtractionError, match="private"):
        await _og(monkeypatch, LOGIN_WALL).extract("https://www.facebook.com/reel/1/")


# --- Apify path --------------------------------------------------------------------


def test_caption_owner_and_comments_reach_the_ai() -> None:
    out = FacebookReelApifyExtractor().build([REEL])
    assert out.type is ContentType.facebook
    body = out.content or ""
    assert "Watch this before you buy a laptop" in body
    assert "TechDaily" in body
    assert "so helpful" in body


def test_title_is_the_caption_first_line() -> None:
    assert FacebookReelApifyExtractor().build([REEL]).title == (
        "Watch this before you buy a laptop"
    )


def test_metadata_is_kept_structured() -> None:
    meta = FacebookReelApifyExtractor().build([REEL]).metadata
    assert meta["is_video"] is True
    assert meta["views"] == 120000
    assert meta["video_url"].endswith(".mp4")


def test_field_names_are_probed_leniently() -> None:
    """Actor payloads differ between store actors, so each field has fallbacks."""
    out = FacebookReelApifyExtractor().build(
        [{"caption": "alt field name", "authorName": "Someone", "views": 5}]
    )
    assert "alt field name" in (out.content or "")
    assert out.metadata["views"] == 5


def test_empty_result_is_permanent() -> None:
    with pytest.raises(PermanentExtractionError, match="private"):
        FacebookReelApifyExtractor().build([])
