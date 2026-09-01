"""What a YouTube save actually reads, and what happens when each tier is unavailable.

Offline -- every network call is monkeypatched. What is pinned is the tiering: oEmbed
alone still produces a card, the Data API adds the description a creator put their links
in, and the caption track adds what was said. Each upper tier must degrade into the one
below rather than failing the save, because a missing key is a deployment fact and a
blocked caption fetch is somebody else's rate limiter -- neither is our user's mistake.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.extractors.youtube import YouTubeExtractor
from app.models.base import ContentType

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

OEMBED = {
    "title": "Building a CLI in Rust",
    "author_name": "Some Channel",
    "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hq.jpg",
    "provider_name": "YouTube",
    "html": "<iframe src='...'></iframe>",
}

SNIPPET = {
    "title": "Building a CLI in Rust",
    "channelTitle": "Some Channel",
    "publishedAt": "2026-01-04T10:00:00Z",
    "tags": ["rust", "cli"],
    "description": (
        "Full written guide: https://example.com/guide\n"
        "Sponsor: https://sponsor.example/deal\n"
        "Follow me at twitter.com/someone"
    ),
}


def _extractor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    oembed: dict[str, Any] | None = None,
    snippet: dict[str, Any] | None = None,
    transcript: str | None = None,
) -> YouTubeExtractor:
    extractor = YouTubeExtractor()

    async def _oembed(_self: Any, _url: str) -> dict[str, Any]:
        return OEMBED if oembed is None else oembed

    async def _snippet(_self: Any, _video_id: str) -> dict[str, Any]:
        return snippet or {}

    async def _transcript(_self: Any, _video_id: str) -> str | None:
        return transcript

    monkeypatch.setattr(YouTubeExtractor, "_fetch_oembed", _oembed)
    monkeypatch.setattr(YouTubeExtractor, "_fetch_snippet", _snippet)
    monkeypatch.setattr(YouTubeExtractor, "_fetch_transcript", _transcript)
    # The URL is validated for real everywhere else; here it would resolve a hostname on
    # a machine that may have no network.
    monkeypatch.setattr("app.extractors.youtube.assert_safe_url", lambda _url: None)
    return extractor


# --- the video id --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?t=30&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=xyz", "dQw4w9WgXcQ"),
        ("https://youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=tooshort", None),
    ],
)
def test_the_video_id_is_pinned_to_its_real_shape(url: str, expected: str | None) -> None:
    """The id is interpolated into a Google API request and is the only user-controlled
    part of it, so constraining its shape is what keeps that request ours."""
    assert YouTubeExtractor._video_id(url) == expected


# --- the tiers -----------------------------------------------------------------------


async def test_oembed_alone_still_produces_a_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no API key there is no description, and that is a thinner memory -- but it is
    still a memory, and the save must not fail for it."""
    extracted = await _extractor(monkeypatch).extract(URL)

    assert extracted.type is ContentType.youtube
    assert extracted.title == "Building a CLI in Rust"
    assert extracted.thumbnail_url == OEMBED["thumbnail_url"]
    assert extracted.content is not None and "Some Channel" in extracted.content
    assert extracted.metadata["links"] == []
    assert extracted.metadata["has_transcript"] is False


async def test_the_description_is_what_carries_the_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is the whole reason the Data API tier exists: oEmbed returns four fields and
    none of them is the description, which is where a creator puts every link."""
    extracted = await _extractor(monkeypatch, snippet=SNIPPET).extract(URL)

    assert extracted.content is not None
    assert "Full written guide" in extracted.content
    assert extracted.metadata["creator_tags"] == ["rust", "cli"]
    assert extracted.metadata["published_at"] == "2026-01-04T10:00:00Z"

    urls = [link["url"] for link in extracted.metadata["links"]]
    assert "https://example.com/guide" in urls
    assert "https://sponsor.example/deal" in urls
    # A bare domain in a description is still a link a viewer is meant to follow.
    assert "https://twitter.com/someone" in urls
    assert {link["source"] for link in extracted.metadata["links"]} == {"description"}


async def test_the_transcript_adds_what_was_only_said(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL spoken aloud and never typed reaches no description. It is kept, and tagged
    as heard rather than written, because transcribing a spoken domain is a guess."""
    extracted = await _extractor(
        monkeypatch,
        snippet=SNIPPET,
        transcript="grab the template over at template.example slash rust",
    ).extract(URL)

    assert extracted.content is not None and "Transcript:" in extracted.content
    assert extracted.metadata["has_transcript"] is True

    by_source = {link["url"]: link["source"] for link in extracted.metadata["links"]}
    assert by_source["https://example.com/guide"] == "description"
    assert by_source["https://template.example"] == "transcript"


async def test_a_link_in_both_places_is_credited_to_the_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trust order, not document order: the typed one is a fact, the heard one is a guess
    that happens to agree."""
    extracted = await _extractor(
        monkeypatch,
        snippet=SNIPPET,
        transcript="everything is at https://example.com/guide",
    ).extract(URL)

    by_source = {link["url"]: link["source"] for link in extracted.metadata["links"]}
    assert by_source["https://example.com/guide"] == "description"


# --- degrading -----------------------------------------------------------------------


async def test_a_hostile_link_in_a_description_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A description is authored by whoever uploaded the video, not by our user."""
    snippet = {
        **SNIPPET,
        "description": (
            "javascript:alert(1)\n"
            "https://youtube.com@evil.example/\n"
            "real one: https://example.com/ok"
        ),
    }
    extracted = await _extractor(monkeypatch, snippet=snippet).extract(URL)

    urls = [link["url"] for link in extracted.metadata["links"]]
    assert urls == ["https://example.com/ok"]


async def test_a_data_api_fault_never_costs_the_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exhausted quota or a rejected key degrades the memory. Raising here would turn
    a thinner memory into a failed one."""
    extractor = YouTubeExtractor()

    async def _oembed(_self: Any, _url: str) -> dict[str, Any]:
        return OEMBED

    async def _boom(_self: Any, _video_id: str) -> str | None:
        raise RuntimeError("captions are blocked from this IP")

    monkeypatch.setattr(YouTubeExtractor, "_fetch_oembed", _oembed)
    monkeypatch.setattr("app.extractors.youtube.assert_safe_url", lambda _url: None)
    monkeypatch.setattr("app.extractors.youtube.settings.YOUTUBE_API_KEY", "")
    monkeypatch.setattr(YouTubeExtractor, "_transcript_blocking", staticmethod(_boom))

    extracted = await extractor.extract(URL)
    assert extracted.title == "Building a CLI in Rust"
    assert extracted.metadata["has_transcript"] is False


async def test_the_api_key_never_reaches_the_stored_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`item_metadata` is serialized to the browser. A key that reaches it has leaked."""
    monkeypatch.setattr("app.extractors.youtube.settings.YOUTUBE_API_KEY", "AIza-secret")
    extracted = await _extractor(monkeypatch, snippet=SNIPPET).extract(URL)
    assert "AIza-secret" not in repr(extracted.metadata)
    assert "AIza-secret" not in (extracted.content or "")
