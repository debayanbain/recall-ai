"""Giving a video reading back the shape the flattening took away.

What is pinned here is mostly restraint. This module makes no judgement about importance
-- two models already did that, and a third layer guessing would be one that can be
confidently wrong with nothing to check it against. So the tests are about structure,
about text from a video frame never being trusted as markup, and about the machine's
document staying out of the slot that means "a person wrote this".
"""
from __future__ import annotations

from typing import Any

from app.services import video_doc

BODY = (
    "Facebook reel by someone\n\n"
    "5 games every engineer should play\n\n"
    "Spoken in the video:\n"
    "Start on sad servers. Then Game of Pods.\n\n"
    "Seen in the video:\n"
    "On-screen text:\n"
    "5 GAMES TO MASTER CLOUD\n"
    "1 : Sad Servers\n"
    "lab\n"
    "2 : Game of Pods\n"
    "1 : Sad Servers\n"
    "\n"
    "Links:\n"
    "none\n\n"
    "What happens:\n"
    "A person introduces five games."
)


def _blocks(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    assert doc is not None
    return doc["blocks"]


def _of_type(doc: dict[str, Any] | None, kind: str) -> list[dict[str, Any]]:
    return [b for b in _blocks(doc) if b["type"] == kind]


# --- structure -----------------------------------------------------------------------


def test_the_reading_becomes_headed_sections() -> None:
    """The point of the exercise: five title cards arrive as five items rather than as
    one paragraph the reader has to pick apart by eye."""
    headers = [b["data"]["text"] for b in _of_type(video_doc.build(BODY), "header")]
    assert headers == ["What was said", "What was on screen", "What happens"]


def test_on_screen_text_becomes_a_list() -> None:
    lists = _of_type(video_doc.build(BODY), "list")
    assert len(lists) == 1
    assert "1 : Sad Servers" in lists[0]["data"]["items"]


def test_the_caption_leads_and_is_not_headed() -> None:
    """It is the memory as it was captured. Everything below it is a machine's account of
    the video, and the order is what says so."""
    first = _blocks(video_doc.build(BODY))[0]
    assert first["type"] == "paragraph"
    assert "Facebook reel by someone" in first["data"]["text"]


def test_fragments_and_repeats_are_dropped_from_the_list() -> None:
    """The vision prompt asks for *every* piece of text visible, which on a fast-cut reel
    catches words mid-transition; and a title card spanning two sampled frames is read
    twice. Both render as noise carrying the same weight as the answer."""
    items = _of_type(video_doc.build(BODY), "list")[0]["data"]["items"]
    assert "lab" not in items
    assert items.count("1 : Sad Servers") == 1


def test_a_none_links_section_produces_no_list() -> None:
    """"none" is the model saying there were none, not a line to render."""
    items = _of_type(video_doc.build(BODY), "list")[0]["data"]["items"]
    assert not any("none" == str(i).casefold() for i in items)


def test_an_ordinary_caption_gets_no_document() -> None:
    """Wrapping one paragraph in a document buys nothing, and None is what tells the
    caller to store nothing and let the reader use the flat text."""
    assert video_doc.build("Just a caption with no video sections.") is None
    assert video_doc.build("") is None


# --- links ---------------------------------------------------------------------------


def test_links_become_anchors_labelled_by_host() -> None:
    """The words around a URL in a video frame are written by whoever made the video, so
    the only honest label for a destination is the destination."""
    doc = video_doc.build(
        BODY, [{"url": "https://sadservers.com/x", "host": "sadservers.com", "source": "video"}]
    )
    rendered = _of_type(doc, "list")[-1]["data"]["items"][0]
    assert '<a href="https://sadservers.com/x">' in rendered
    assert "sadservers.com" in rendered


def test_the_models_own_links_section_is_ignored() -> None:
    """`core.links` already found, validated and attributed the links. Rendering a second
    unvalidated copy would put a string nobody checked beside one that was."""
    body = BODY.replace("Links:\nnone", "Links:\nevil.example/steal")
    doc = video_doc.build(body)
    assert "evil.example" not in str(doc)


# --- what a frame is never allowed to become -----------------------------------------


def test_text_read_off_a_frame_is_escaped_not_rendered() -> None:
    """A caption containing markup is a thing a person can type on purpose, and this text
    is transcribed from a video someone else made."""
    body = BODY.replace("5 GAMES TO MASTER CLOUD", "<script>alert(1)</script>")
    doc = video_doc.build(body)
    assert "<script>" not in str(doc)


def test_a_hostile_href_never_survives() -> None:
    doc = video_doc.build(
        BODY, [{"url": "javascript:alert(1)", "host": "x", "source": "video"}]
    )
    assert "javascript:" not in str(doc)


def test_the_document_passes_the_same_gate_a_typed_one_does() -> None:
    """Generated or typed, a stored document obeys one allowlist. Anything outside it
    cannot appear, because nothing in the output is copied from the input."""
    doc = video_doc.build(BODY)
    for block in _blocks(doc):
        assert block["type"] in {"paragraph", "header", "list"}
