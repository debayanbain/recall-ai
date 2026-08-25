"""Apify-backed Instagram extraction.

The mapping is tested directly rather than through HTTP: `_build` is pure, and what
matters is that a real actor payload becomes text the AI pipeline can tag.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.extractors.base import PermanentExtractionError
from app.extractors.instagram import (
    MAX_CONTENT_CHARS,
    ApifyNotConfiguredError,
    InstagramPostExtractor,
    InstagramReelExtractor,
)
from app.extractors.registry import get_extractor
from app.models.base import ContentType

REEL: dict[str, Any] = {
    "type": "Video",
    "shortCode": "C1xYzAbCdEf",
    "caption": "3 ways to take better notes\nSave this for later!",
    "hashtags": ["pkm", "notetaking"],
    "mentions": ["tiagoforte"],
    "ownerUsername": "productivity",
    "displayUrl": "https://scontent.cdninstagram.com/thumb.jpg",
    "videoUrl": "https://scontent.cdninstagram.com/reel.mp4",
    "videoDuration": 42.5,
    "likesCount": 1200,
    "commentsCount": 34,
    "videoViewCount": 90000,
    "timestamp": "2026-08-01T10:00:00.000Z",
    "musicInfo": {"song_name": "Lo-fi beat", "artist_name": "Someone"},
    "latestComments": [{"text": "so useful"}, {"text": "saved!"}],
}


def test_reel_urls_route_to_the_paid_extractor() -> None:
    for url in (
        "https://www.instagram.com/reel/C1xYzAbCdEf/",
        "https://www.instagram.com/reel/C1xYzAbCdEf/?igsi=tracking",
        "https://www.instagram.com/someuser/reel/C1xYzAbCdEf/",
        "https://www.instagram.com/tv/C1xYzAbCdEf/",
    ):
        assert isinstance(get_extractor(url), InstagramReelExtractor), url


def test_posts_never_reach_apify() -> None:
    """A post is a caption and an image — not worth an actor run."""
    extractor = get_extractor("https://instagram.com/p/C1xYzAbCdEf/")
    assert isinstance(extractor, InstagramPostExtractor)
    assert extractor.deferred is False


async def test_a_post_is_saved_as_a_link_without_enrichment() -> None:
    out = await InstagramPostExtractor().extract("https://instagram.com/p/C1xYzAbCdEf/")
    assert out.enrich is False, "no text exists, so the model must not be called"
    assert out.title == "Instagram post C1xYzAbCdEf"
    assert out.content is None
    assert out.metadata["source"] == "link-only"


def test_profile_urls_are_left_to_the_article_fallback() -> None:
    """A profile is not a memory; the actor returns a different shape for it."""
    assert not InstagramReelExtractor().can_handle("https://www.instagram.com/someuser/")


def test_caption_hashtags_and_audio_all_reach_the_ai_pipeline() -> None:
    out = InstagramReelExtractor()._build([REEL], "https://instagram.com/reel/C1xYzAbCdEf/")

    assert out.type is ContentType.instagram
    body = out.content or ""
    # Everything the model needs to produce meaningful memory tags.
    assert "3 ways to take better notes" in body
    assert "#pkm" in body and "#notetaking" in body
    assert "@tiagoforte" in body
    assert "@productivity" in body
    assert "Lo-fi beat" in body
    assert "so useful" in body


def test_title_is_the_caption_first_line_not_the_whole_blob() -> None:
    out = InstagramReelExtractor()._build([REEL], "https://instagram.com/reel/x/")
    assert out.title == "3 ways to take better notes"


def test_reel_metadata_is_kept_structured() -> None:
    meta = InstagramReelExtractor()._build([REEL], "https://instagram.com/reel/x/").metadata
    assert meta["is_video"] is True
    assert meta["video_url"].endswith(".mp4")
    assert meta["views"] == 90000
    assert meta["hashtags"] == ["pkm", "notetaking"]


def test_captionless_post_still_gets_a_usable_title() -> None:
    out = InstagramReelExtractor()._build(
        [{"type": "Image", "ownerUsername": "someone"}], "https://instagram.com/p/x/"
    )
    assert out.title == "Instagram post by @someone"


def test_private_or_deleted_post_is_a_permanent_failure() -> None:
    """Permanent, not retryable: four ARQ attempts would cost four paid actor runs."""
    with pytest.raises(PermanentExtractionError, match="private"):
        InstagramReelExtractor()._build([], "https://instagram.com/p/x/")


@pytest.mark.parametrize("code", ["not_found", "no_items", "private"])
def test_actor_unavailable_codes_are_permanent(code: str) -> None:
    with pytest.raises(PermanentExtractionError):
        InstagramReelExtractor()._build(
            [{"error": code, "errorDescription": "Post does not exist"}],
            "https://instagram.com/p/x/",
        )


def test_unknown_actor_errors_stay_retryable() -> None:
    """An unrecognised failure might be transient — let ARQ try again."""
    with pytest.raises(RuntimeError) as caught:
        InstagramReelExtractor()._build([{"error": "rate_limited"}], "https://instagram.com/p/x/")
    assert not isinstance(caught.value, PermanentExtractionError)


def test_huge_comment_threads_are_capped_before_reaching_the_llm() -> None:
    """A viral post's thread would otherwise blow the prompt budget for no extra signal."""
    fat = dict(REEL, caption="x" * 40_000)
    out = InstagramReelExtractor()._build([fat], "https://instagram.com/p/x/")
    assert out.content is not None
    assert len(out.content) <= MAX_CONTENT_CHARS + 1


async def test_missing_token_names_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Better than silently falling through to a login-wall page with no content."""
    monkeypatch.setattr(settings, "APIFY_TOKEN", "")
    with pytest.raises(ApifyNotConfiguredError, match="APIFY_TOKEN"):
        await InstagramReelExtractor().start("https://instagram.com/reel/x/")


def test_instagram_is_marked_deferred() -> None:
    """ProcessingService branches on this: a crawl must not block a worker."""
    assert InstagramReelExtractor().deferred is True


def test_fast_extractors_are_not_deferred() -> None:
    """A 300ms oEmbed does not need a run table and a webhook round trip."""
    from app.extractors.article import ArticleExtractor
    from app.extractors.youtube import YouTubeExtractor

    assert getattr(ArticleExtractor(), "deferred", False) is False
    assert getattr(YouTubeExtractor(), "deferred", False) is False


def test_webhook_config_is_omitted_without_a_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registering a callback to localhost would strand every run silently."""
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(settings, "APIFY_WEBHOOK_SECRET", "s3cret")
    assert InstagramReelExtractor._webhook_config() is None


def test_webhook_config_encodes_the_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64
    import json

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://tunnel.example")
    monkeypatch.setattr(settings, "APIFY_WEBHOOK_SECRET", "s3cret")

    encoded = InstagramReelExtractor._webhook_config()
    assert encoded is not None
    hooks = json.loads(base64.b64decode(encoded))
    url = hooks[0]["requestUrl"]
    assert url == "https://tunnel.example/api/v1/webhooks/apify/s3cret"
    # Failure events matter as much as success: without them a failed run never calls
    # back and only the sweeper would ever notice.
    assert "ACTOR.RUN.FAILED" in hooks[0]["eventTypes"]
