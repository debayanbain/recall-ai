"""Turning a video reading into a structured document instead of a wall of text.

`_read_video` produces a body with real internal structure -- a caption, a transcript, an
itemised list of what was on screen, a description -- and then flattens all of it into one
string. The page renders that string as a single block of prose, so the five product names
a creator put on five separate title cards arrive as one paragraph, and the reader has to
find them by eye. The information survived; the shape did not.

This rebuilds the shape as an EditorJS block document, which the frontend already knows
how to render (`components/rich-content.tsx`).

**Where the "AI identification" actually happens is worth being precise about, because it
is not here.** Two models have already done it by the time this runs: the vision model
itemised the on-screen text line by line, and `generate_highlights` picked the key
sentences, which `keep_verbatim` then checked really occur in the text. This module adds
no third model call and makes no judgement of its own -- it is a deterministic reshaping
of what those two produced. That matters, because a layer that *guessed* at importance
would be a layer that can be confidently wrong about which line mattered, with nothing to
check it against.

So each kind of thing gets the emphasis that suits it:

* an **important line** stays a `<mark>`, applied by the existing highlight spans, because
  it is a sentence inside a paragraph;
* a **site or product name** becomes its own list item, because it is an item in a list
  and marking an 11-character name inside prose just speckles the text -- which is exactly
  what `MIN_SPAN_CHARS` exists to prevent;
* a **link** becomes a real anchor, so it is reachable from the body and not only from the
  links panel beside it.

**Stored under its own key, never as `editor_doc`.** That field means "a person edited
this": the Edit button seeds from it, and `PATCH /vault/{id}/content` writes it. A
machine-written document landing there would be indistinguishable from the user's own
work, and the next video re-read would silently overwrite whatever they had typed. The
frontend prefers `editor_doc` when it exists and falls back to this.
"""
from __future__ import annotations

from html import escape
from typing import Any

from app.core.logging import get_logger
from app.services import editor_doc

log = get_logger("processing.video_doc")

#: Section markers written by `VideoReading.text` and by the vision prompt. Matched rather
#: than parsed loosely: they are strings this codebase itself produces, so a miss means
#: the prompt changed and the flat body is the honest fallback.
_SPOKEN = "Spoken in the video:"
_SEEN = "Seen in the video:"
_ON_SCREEN = "On-screen text:"
_LINKS = "Links:"
_WHAT = "What happens:"

#: Headings the reader sees. Deliberately shorter than the machine markers above -- the
#: page already says the whole body is a reading of a video, so each heading only has to
#: say which part of it this is.
_HEADINGS = {
    _SPOKEN: "What was said",
    _ON_SCREEN: "What was on screen",
    _WHAT: "What happens",
}

#: A line the vision model emits to mean "there was none". Never rendered as content.
_NONE_VALUES = frozenset({"none", "none.", "n/a", "-", "—"})

#: An on-screen line longer than this is a sentence the model wrote, not a caption it
#: read, so it stays a paragraph rather than becoming a list item.
_MAX_ITEM_CHARS = 120

#: Shorter than this and it is a fragment, not a label. The vision prompt asks for *every*
#: piece of text visible, which on a fast-cut reel means catching words mid-transition --
#: a real reading produced "lab", "while", "broken" and "game of" alongside the five
#: product names it was actually there for. Rendering those as list items presents noise
#: with the same weight as the answer.
#:
#: The cost is real and worth naming: a genuine two-word call to action ("COMMENT CLOUD")
#: is dropped too. That is acceptable *here specifically* because nothing is lost from the
#: memory -- the flat body keeps every line, and a call to action that matters is said out
#: loud and lands in the transcript. This filter shapes one list; it never decides what is
#: stored.
_MIN_ITEM_CHARS = 8

#: Bounded for the same reason every other list here is: this is model output derived
#: from frames an attacker chose, and a JSONB column should not be growable for free.
_MAX_ITEMS = 40


def build(content: str, links: list[dict[str, str]] | None = None) -> dict[str, Any] | None:
    """Build the block document for a video memory, or None when there is no shape to find.

    None is the honest answer for a body with no video sections in it -- an ordinary
    caption is already one paragraph and wrapping it in a document buys nothing. The
    caller stores nothing in that case and the reader falls back to the flat text.
    """
    if not content or _SPOKEN not in content and _SEEN not in content:
        return None

    caption, spoken, seen = _split(content)
    blocks: list[dict[str, Any]] = []

    # The author's own words come first and unheaded: they are the memory as it was
    # captured, and everything below is a machine's account of the video.
    blocks.extend(_paragraphs(caption))

    if spoken:
        blocks.append(_header(_HEADINGS[_SPOKEN]))
        blocks.extend(_paragraphs(spoken))

    if seen:
        blocks.extend(_seen_blocks(seen))

    safe_links = links or []
    if safe_links:
        blocks.append(_header("Links"))
        blocks.append(
            {
                "type": "list",
                "data": {
                    "style": "unordered",
                    "items": [_anchor(link) for link in safe_links[:_MAX_ITEMS]],
                },
            }
        )

    if not blocks:
        return None

    try:
        # Through the same gate a browser-posted document goes through, so a generated
        # document cannot carry anything a typed one could not. Belt and braces: every
        # string here is escaped on the way in, and the sanitizer re-serializes from its
        # own allowlist rather than trusting what it is handed.
        document, _plain = editor_doc.sanitize(blocks)
    except editor_doc.EditorDocumentError as exc:
        # Never fatal. The flat body is still there and still correct; losing the shape is
        # a worse-looking page, not a lost memory.
        log.info("video_doc_rejected", error=str(exc)[:120])
        return None

    return document


def _split(content: str) -> tuple[str, str, str]:
    """Peel the body into caption / spoken / seen, each possibly empty."""
    caption, spoken, seen = content, "", ""

    if _SEEN in caption:
        caption, _, seen = caption.partition(_SEEN)
    if _SPOKEN in caption:
        caption, _, spoken = caption.partition(_SPOKEN)

    return caption.strip(), spoken.strip(), seen.strip()


def _seen_blocks(seen: str) -> list[dict[str, Any]]:
    """The on-screen section: a list of what was read off the frames, then the description.

    The model's own `Links:` section is deliberately ignored. `core.links` already found
    the links, validated them and recorded where each came from, and rendering a second
    unvalidated copy of that list would put a string nobody checked next to one that was.
    """
    blocks: list[dict[str, Any]] = []

    on_screen, description = "", ""
    rest = seen
    if _ON_SCREEN in rest:
        _, _, rest = rest.partition(_ON_SCREEN)
    if _WHAT in rest:
        rest, _, description = rest.partition(_WHAT)
    if _LINKS in rest:
        rest, _, _ = rest.partition(_LINKS)
    on_screen = rest.strip()

    items: list[str] = []
    seen_items: set[str] = set()
    for line in on_screen.splitlines():
        candidate = line.strip()
        key = candidate.casefold()
        if (
            not candidate
            or key in _NONE_VALUES
            or key in seen_items
            or not _MIN_ITEM_CHARS <= len(candidate) <= _MAX_ITEM_CHARS
        ):
            continue
        # Deduplicated because a title card that stays on screen across two sampled
        # frames is read twice, and the same label twice reads as two things.
        seen_items.add(key)
        items.append(candidate)
        if len(items) >= _MAX_ITEMS:
            break

    if items:
        blocks.append(_header(_HEADINGS[_ON_SCREEN]))
        # A list, not a paragraph. These are separate title cards that happened to be
        # concatenated by the flattening -- five product names on five frames read as one
        # sentence, which is the specific thing this fixes.
        blocks.append(
            {
                "type": "list",
                "data": {"style": "unordered", "items": [escape(i) for i in items]},
            }
        )

    if description.strip():
        blocks.append(_header(_HEADINGS[_WHAT]))
        blocks.extend(_paragraphs(description.strip()))

    return blocks


def _header(text: str) -> dict[str, Any]:
    # Level 3: the page's own <h2> already names the section ("Full content"), so these
    # sit under it rather than competing with it.
    return {"type": "header", "data": {"text": escape(text), "level": 3}}


def _paragraphs(text: str) -> list[dict[str, Any]]:
    """Blank-line-separated paragraphs, escaped.

    Escaped rather than passed through: this text is a transcription of speech and of
    words on a frame, both of which are written by someone who is not our user, and a
    caption containing `<script>` is a thing a person can type on purpose.
    """
    out: list[dict[str, Any]] = []
    for part in text.split("\n\n"):
        cleaned = part.strip()
        if not cleaned or cleaned.lower() in _NONE_VALUES:
            continue
        # Single newlines inside one paragraph are line wrapping, not structure; <br> is
        # in the inline allowlist and keeps them without inventing paragraph breaks.
        out.append(
            {"type": "paragraph", "data": {"text": escape(cleaned).replace("\n", "<br>")}}
        )
    return out


def _anchor(link: dict[str, str]) -> str:
    """One link as an anchor whose visible text is its host.

    The host is the link text for the same reason the links panel leads with it: the words
    around a URL in a video frame are written by whoever made the video, and the only
    honest label for a destination is the destination.
    """
    url = link.get("url", "")
    host = link.get("host") or _host_of(url)
    path = url.split(host, 1)[-1] if host and host in url else ""
    tail = f" {escape(path)}" if path and path not in ("", "/") else ""
    return f'<a href="{escape(url)}">{escape(host or url)}</a>{tail}'


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
