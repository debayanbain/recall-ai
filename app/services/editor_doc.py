"""EditorJS document handling: sanitize blocks, keep a plain-text projection.

Two representations are stored and they answer different questions:

* ``item_metadata["editor_doc"]`` is the block document — headings, lists, quotes and a
  **small allowlist of inline markup** (bold, italic, underline, mark, code, links). It
  is what the reader renders and what re-seeds the editor, so formatting survives a save.
* ``VaultItem.content`` is the flat text projection of that document. Highlights index
  into it, search matches it and the embedding is drawn from it — none of which want
  tags. It is derived here, never accepted from the client.

The inline HTML is produced by **re-serializing from an allowlist**, never by stripping
patterns out of the input: the parser reads whatever the contenteditable sent, throws
away every tag and attribute it does not recognise, and writes fresh markup from what is
left. A tag that is not in ``_ALLOWED_TAGS`` cannot appear in the output because nothing
in the output is copied from the input — only text, which is escaped on the way through.
That is what makes it safe for the frontend to render these blocks as elements.

A ``code`` block is the exception and is kept verbatim: it comes from a textarea's
``.value``, which never parses markup, and sanitizing it would delete the user's own
examples of the very tags this module refuses.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

#: A document longer than this is not a hand-written note, it is a paste bomb.
MAX_BLOCKS = 500
MAX_BLOCK_CHARS = 20_000
#: Markup costs characters that the reader never sees, so the raw input gets more room.
MAX_RAW_CHARS = MAX_BLOCK_CHARS * 4
MAX_LIST_ITEMS = 300
MAX_CONTENT_CHARS = 400_000
#: Deeper nesting than this is flattened rather than refused -- the text survives either way.
MAX_LIST_DEPTH = 4
MAX_INLINE_NESTING = 8

#: Block types we know how to flatten. Anything else is dropped: a block we cannot turn
#: into text would be invisible in `content` while still occupying the stored document.
ALLOWED_BLOCK_TYPES = frozenset({"paragraph", "header", "list", "quote", "code", "delimiter"})

#: Inline tags the reader is allowed to render. Deliberately tiny: every entry here is a
#: tag some future renderer has to handle safely, and none of these carry behaviour.
_ALLOWED_TAGS = frozenset({"b", "i", "u", "mark", "code", "a", "br"})
#: The editor emits both spellings depending on the browser's execCommand.
_NORMALIZE = {"strong": "b", "em": "i", "ins": "u"}
#: Schemes a link may use. `javascript:` and `data:` are the reason this list exists.
_SAFE_SCHEME = re.compile(r"^(?:https?://|mailto:)", re.IGNORECASE)
#: Stripped before the scheme is checked, so `java\tscript:` cannot sneak past it.
_URL_NOISE = re.compile(r"[\s\x00-\x1f\x7f]+")
#: Pasted block-level markup carries a line break with it; the tag goes, the break stays.
_BLOCK_LEVEL = frozenset({"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})

_TAG = re.compile(r"<[^>]*>")
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)


class EditorDocumentError(ValueError):
    """The submitted document cannot be stored. The message is shown to the user."""


def _safe_href(value: str | None) -> str | None:
    """A link target, or None when it is not plain web navigation."""
    if not value:
        return None
    cleaned = _URL_NOISE.sub("", html.unescape(value))
    return cleaned if _SAFE_SCHEME.match(cleaned) else None


class _InlineSanitizer(HTMLParser):
    """Rewrites a contenteditable's markup as a small, known-safe subset.

    Nothing from the input is copied into the output except text, which is escaped. Tags
    are re-emitted from the allowlist, attributes are dropped wholesale (``href`` is the
    single exception and is re-validated), and any tag left open at the end is closed
    here so a truncated paste cannot leak structure into whatever renders it.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = _NORMALIZE.get(tag, tag)
        if tag == "br":
            self._out.append("<br>")
            return
        if tag not in _ALLOWED_TAGS or len(self._open) >= MAX_INLINE_NESTING:
            return  # the tag goes, its text stays
        if tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            if href is None:
                return
            self._out.append(f'<a href="{html.escape(href, quote=True)}">')
        else:
            self._out.append(f"<{tag}>")
        self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if _NORMALIZE.get(tag, tag) == "br":
            self._out.append("<br>")

    def handle_endtag(self, tag: str) -> None:
        tag = _NORMALIZE.get(tag, tag)
        if tag in _BLOCK_LEVEL:
            self._out.append("<br>")
            return
        if tag == "br" or tag not in self._open:
            return
        # Close through to the matching tag, so `<b><i></b>` cannot leave `<i>` dangling.
        while self._open:
            opened = self._open.pop()
            self._out.append(f"</{opened}>")
            if opened == tag:
                break

    def handle_data(self, data: str) -> None:
        self._out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self._open:
            self._out.append(f"</{self._open.pop()}>")
        return "".join(self._out)


def to_rich(value: Any) -> str:
    """Contenteditable markup -> the allowlisted inline subset the reader may render."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _InlineSanitizer()
    parser.feed(value[:MAX_RAW_CHARS])
    parser.close()
    # nbsp is what a contenteditable inserts for a held-down space; it is a space.
    return parser.result().replace("\xa0", " ").strip()


def to_plain(value: str) -> str:
    """Our own sanitized markup -> the flat text that goes into `content`.

    Only ever fed output from `to_rich`, so the tag set is known and the regex has
    nothing to be fooled by -- this is a projection, not a security boundary.
    """
    text = _BREAK.sub("\n", value)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    return _TRAILING_WS.sub("", text).strip()


def _both(value: Any) -> tuple[str, str]:
    """(rich, plain) for one field, capped on the text a reader actually sees."""
    rich = to_rich(value)
    plain = to_plain(rich)
    if len(plain) > MAX_BLOCK_CHARS:
        # Truncating markup mid-tag is how a renderer inherits someone else's `<a>`;
        # past the cap the formatting is dropped rather than cut.
        plain = plain[:MAX_BLOCK_CHARS]
        rich = html.escape(plain, quote=False)
    return rich, plain


def _list_items(raw: Any, depth: int = 0) -> list[tuple[int, str, str]]:
    """Flatten EditorJS list items to (depth, rich, plain) triples.

    Two shapes are in the wild and both are accepted: `@editorjs/list` v1 stores plain
    strings, v2 stores `{content, items}` objects with nesting. Guessing wrong on the
    installed version would drop every bullet.
    """
    if not isinstance(raw, list) or depth > MAX_LIST_DEPTH:
        return []
    out: list[tuple[int, str, str]] = []
    for entry in raw[:MAX_LIST_ITEMS]:
        if isinstance(entry, str):
            rich, plain = _both(entry)
            if plain:
                out.append((depth, rich, plain))
        elif isinstance(entry, dict):
            rich, plain = _both(entry.get("content") or entry.get("text"))
            if plain:
                out.append((depth, rich, plain))
            out.extend(_list_items(entry.get("items"), depth + 1))
    return out


def _clean_block(block: Any) -> tuple[dict[str, Any], str] | None:
    """One raw block -> (stored block, its plain text), or None when it carries nothing."""
    if not isinstance(block, dict):
        return None
    block_type = block.get("type")
    if not isinstance(block_type, str) or block_type not in ALLOWED_BLOCK_TYPES:
        return None
    data = block.get("data")
    data = data if isinstance(data, dict) else {}

    if block_type == "delimiter":
        return {"type": "delimiter", "data": {}}, "---"

    if block_type == "code":
        # Already plain -- see the module docstring for why this one is not sanitized.
        raw = data.get("code")
        code = raw[:MAX_BLOCK_CHARS] if isinstance(raw, str) else ""
        if not code.strip():
            return None
        return {"type": "code", "data": {"code": code}}, code

    if block_type == "header":
        rich, plain = _both(data.get("text"))
        if not plain:
            return None
        raw_level = data.get("level")
        level = raw_level if isinstance(raw_level, int) and 1 <= raw_level <= 6 else 2
        return {"type": "header", "data": {"text": rich, "level": level}}, plain

    if block_type == "quote":
        rich, plain = _both(data.get("text"))
        caption_rich, caption_plain = _both(data.get("caption"))
        if not plain:
            return None
        stored = {"type": "quote", "data": {"text": rich, "caption": caption_rich}}
        return stored, f"{plain}\n— {caption_plain}" if caption_plain else plain

    if block_type == "list":
        items = _list_items(data.get("items"))
        if not items:
            return None
        ordered = data.get("style") == "ordered"
        marker = "1." if ordered else "-"
        lines = ["  " * depth + f"{marker} {plain}" for depth, _, plain in items]
        stored = {
            "type": "list",
            "data": {
                "style": "ordered" if ordered else "unordered",
                # Flat strings: the nested shape is rebuilt by neither side, and a list
                # that renders one level deep is better than one that fails to load.
                "items": [rich for _, rich, _ in items],
            },
        }
        return stored, "\n".join(lines)

    rich, plain = _both(data.get("text"))
    if not plain:
        return None
    return {"type": "paragraph", "data": {"text": rich}}, plain


def sanitize(blocks: Any) -> tuple[dict[str, Any], str]:
    """Validate and normalize a submitted document.

    Returns the block document to store and the plain text to write to
    `VaultItem.content`. Raises `EditorDocumentError` when there is nothing to save --
    an empty save would silently wipe a memory's body, which is not something a user
    ever means by pressing Save.
    """
    if not isinstance(blocks, list):
        raise EditorDocumentError("The editor sent something we couldn't read.")
    if len(blocks) > MAX_BLOCKS:
        raise EditorDocumentError(f"That's more than {MAX_BLOCKS} blocks — split it up.")

    stored: list[dict[str, Any]] = []
    parts: list[str] = []
    for block in blocks:
        cleaned = _clean_block(block)
        if cleaned is None:
            continue
        block_doc, text = cleaned
        stored.append(block_doc)
        parts.append(text)

    content = "\n\n".join(parts).strip()
    if not content:
        raise EditorDocumentError("Write something before saving.")
    if len(content) > MAX_CONTENT_CHARS:
        raise EditorDocumentError(
            f"That's longer than {MAX_CONTENT_CHARS:,} characters — trim it down."
        )
    return {"blocks": stored}, content
