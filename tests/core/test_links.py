"""What counts as a link we are willing to store and show someone.

Every string fed to `core.links` was written by someone who is not our user -- a caption,
a video description, a model's reading of text an attacker chose to burn into a frame --
and the output ends up as something a person is invited to tap. So these are not parsing
tests with a security case bolted on; the refusals *are* the feature, and each one below
is a way a link can claim to go somewhere it does not.
"""
from __future__ import annotations

import pytest

from app.core.links import (
    LinkRejected,
    collect_links,
    extract_links,
    normalise_link,
    origin_label,
)

# --- what must never survive ---------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "jAvAsCrIpT:alert(document.domain)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "ftp://files.example.com/x",
    ],
)
def test_only_http_and_https_are_renderable(hostile: str) -> None:
    """An allowlist, not a blocklist: the set of schemes a browser or an OS will act on
    is not knowable, so anything unrecognised is dropped without an opinion about it."""
    with pytest.raises(LinkRejected):
        normalise_link(hostile)
    assert extract_links(hostile) == []


@pytest.mark.parametrize(
    "deceptive",
    [
        "https://youtube.com@evil.example/",
        "https://paypal.com@attacker.example/login",
        "http://good.example@evil.example",
    ],
)
def test_userinfo_links_are_refused_not_trimmed(deceptive: str) -> None:
    """`https://good.com@evil.com` renders as good.com wherever a UI truncates and
    navigates to evil.com.

    The regression this pins is subtler than the refusal: an earlier version matched only
    the half *before* the `@`, so this string quietly produced a clean link to
    youtube.com -- a destination the text never pointed at. Dropping the whole token is
    the only honest answer.
    """
    with pytest.raises(LinkRejected):
        normalise_link(deceptive)
    assert extract_links(f"click here {deceptive} now") == []


def test_homoglyph_domains_are_refused_in_both_spellings() -> None:
    """A Greek omicron in `google.com` reads as the real thing at any length.

    Both spellings go, because rejecting only the non-ASCII form is a check that catches
    nobody who pressed submit twice -- `xn--` is the same domain already encoded.
    """
    with pytest.raises(LinkRejected):
        normalise_link("https://gοogle.com/")
    with pytest.raises(LinkRejected):
        normalise_link("https://xn--ggle-0nda.com/")
    assert extract_links("go to xn--ggle-0nda.com now") == []


def test_invisible_characters_cannot_hide_a_host() -> None:
    """A zero-width space between homoglyphs makes two different strings look identical.

    They are stripped before parsing, so what is validated is what a browser would
    resolve -- not a decorated spelling of it.
    """
    assert extract_links("visit exam​ple.com/x") == ["https://example.com/x"]
    assert extract_links("go\tto\nhttps://example.com/a") == ["https://example.com/a"]


@pytest.mark.parametrize(
    "not_a_link",
    [
        "see figure2.png for details",
        "the file report.pdf is attached",
        "open invoice.zip",
        "mail me at someone@example.com",
        "version 1.2.3 shipped",
    ],
)
def test_filenames_and_addresses_are_not_links(not_a_link: str) -> None:
    """A false positive here puts a clickable host where the author wrote a filename."""
    assert extract_links(not_a_link) == []


def test_numeric_and_internal_hosts_are_not_offered() -> None:
    """A numeric host in a caption is never a link a creator meant a viewer to type, and
    an internal name is one nobody should be invited to open."""
    assert extract_links("http://169.254.169.254/latest/meta-data/") == []
    assert extract_links("http://127.0.0.1:8000/admin") == []
    assert extract_links("http://[::1]/") == []
    with pytest.raises(LinkRejected):
        normalise_link("http://localhost/admin")


def test_an_over_long_url_is_refused() -> None:
    with pytest.raises(LinkRejected):
        normalise_link("https://example.com/" + "a" * 600)


# --- what must survive ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com/pricing", "https://example.com/pricing"),
        ("www.example.co.uk/x", "https://www.example.co.uk/x"),
        ("linktr.ee/creator", "https://linktr.ee/creator"),
        ("//example.com/x", "https://example.com/x"),
        ("HTTPS://Example.COM/Path", "https://example.com/Path"),
        ("http://example.com:80/x", "http://example.com/x"),
        ("https://example.com:8080/a?b=1#c", "https://example.com:8080/a?b=1#c"),
    ],
)
def test_real_links_are_kept_and_normalised(raw: str, expected: str) -> None:
    """Default ports are dropped and the host is lowercased so two spellings of one link
    dedupe against each other; the path's case is left alone, because it is significant."""
    assert normalise_link(raw) == expected


def test_sentence_punctuation_is_stripped_but_a_closing_paren_is_not() -> None:
    """A link at the end of a caption collects punctuation that is not part of it.

    The paren case is the one that bites: `.../Foo_(bar).` loses both characters in a
    single rstrip, so testing the last character finds a '.' and restores nothing.
    """
    assert extract_links("read https://example.com/a.") == ["https://example.com/a"]
    assert extract_links("see (https://example.com/a) here") == ["https://example.com/a"]
    assert extract_links("https://en.wikipedia.org/wiki/Foo_(bar).") == [
        "https://en.wikipedia.org/wiki/Foo_(bar)"
    ]


def test_links_are_deduped_and_capped_in_first_seen_order() -> None:
    """First-seen rather than sorted: a creator's own link is usually the first one shown,
    and an alphabetical list buries it under whatever sponsor URL starts with an 'a'."""
    text = "https://b.example/1 then https://a.example/2 then https://B.EXAMPLE/1"
    assert extract_links(text) == ["https://b.example/1", "https://a.example/2"]
    assert len(extract_links(" ".join(f"https://s{i}.example/x" for i in range(40)))) == 25
    assert len(extract_links("https://a.example/1 https://b.example/2", limit=1)) == 1


# --- provenance ----------------------------------------------------------------------


def test_the_most_trusted_source_of_a_link_is_the_one_recorded() -> None:
    """A URL typed by a creator is a fact; the same URL read off a blurry frame is a guess
    that happens to be right. `collect_links` is ordered by trust and the first source to
    yield a link keeps it, so the reader can say which it is showing."""
    found = collect_links(
        [
            ("caption", "full guide at https://example.com/guide"),
            ("video", "https://example.com/guide and https://shop.example/x"),
            ("speech", "https://spoken.example/y"),
        ],
        limit=10,
    )
    assert found == [
        {"url": "https://example.com/guide", "source": "caption"},
        {"url": "https://shop.example/x", "source": "video"},
        {"url": "https://spoken.example/y", "source": "speech"},
    ]


def test_collect_links_drops_hostile_entries_without_dropping_the_rest() -> None:
    found = collect_links(
        [("video", "javascript:alert(1) then https://safe.example/a")], limit=10
    )
    assert found == [{"url": "https://safe.example/a", "source": "video"}]


def test_collect_links_respects_the_cap_across_all_sources() -> None:
    found = collect_links(
        [("caption", "https://a.example/1 https://b.example/2"), ("video", "https://c.example/3")],
        limit=2,
    )
    assert len(found) == 2


# --- what the UI is given ------------------------------------------------------------


def test_origin_label_names_the_real_destination() -> None:
    """The UI shows this beside the link, so the decision to tap is made against where it
    goes rather than against the words that surrounded it in a frame."""
    assert origin_label("https://sub.example.com/a/b?c=1") == "sub.example.com"


def test_origin_label_never_raises() -> None:
    """A display helper must not be able to break a page."""
    assert origin_label("") == ""
    assert origin_label("not a url") == ""
