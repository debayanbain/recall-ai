"""URL canonicalisation — the key duplicate detection compares on."""
from __future__ import annotations

import pytest

from app.core.urls import canonical_url


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # The exact case that motivated this: Instagram's share parameter.
        (
            "https://www.instagram.com/reel/DcWSEOVKt88/?igsi=MWhubDBnbW5va2hxbw==",
            "https://www.instagram.com/reel/DcWSEOVKt88/",
        ),
        ("https://example.com/a/", "https://example.com/a"),
        ("https://EXAMPLE.com/a", "https://example.com/a"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&utm_medium=y", "https://example.com/a"),
        ("https://example.com/a?fbclid=123", "https://example.com/a"),
    ],
)
def test_share_noise_collapses_to_the_same_key(a: str, b: str) -> None:
    assert canonical_url(a) == canonical_url(b)


def test_meaningful_query_parameters_survive() -> None:
    """A YouTube link's identity lives in `v` — stripping unknown params would break it."""
    out = canonical_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=track")
    assert "v=dQw4w9WgXcQ" in out
    assert "si=" not in out


def test_different_videos_stay_different() -> None:
    assert canonical_url("https://youtube.com/watch?v=aaa") != canonical_url(
        "https://youtube.com/watch?v=bbb"
    )


def test_different_reels_stay_different() -> None:
    assert canonical_url("https://instagram.com/reel/AAA/") != canonical_url(
        "https://instagram.com/reel/BBB/"
    )


def test_the_root_path_keeps_its_slash() -> None:
    assert canonical_url("https://example.com/") == "https://example.com/"


def test_unparseable_input_is_returned_unchanged() -> None:
    assert canonical_url("not a url") == "not a url"
