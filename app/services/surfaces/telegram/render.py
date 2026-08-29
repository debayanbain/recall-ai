"""Blocks -> Telegram HTML. The only place that translation happens.

The engine returns structure; this turns it into the one markup Telegram will accept.
Keeping the mapping in a single file is what makes a second surface cheap: it writes its
own renderer and shares every line of the engine.

**Everything interpolated is escaped, because everything interpolated is untrusted.**
Titles, categories and answer text are model output derived from scraped pages, and
Telegram's HTML mode rejects a whole message on malformed markup -- so a missed escape is
not a cosmetic bug, it is the user receiving nothing at all. The escaping itself is
`formatting.escape` rather than a second implementation here: two copies of an escape
function is one copy that gets fixed.

HTML and not MarkdownV2 for the reason the rest of this surface uses it: MarkdownV2 makes
18 characters special including `.` and `-`, so one article title returns HTTP 400.
"""
from __future__ import annotations

from typing import assert_never

from app.services.chat_engine.types import (
    Block,
    ErrorBlock,
    ErrorKind,
    ItemListBlock,
    OutboundReply,
    TextBlock,
)
from app.services.telegram import formatting


def render(reply: OutboundReply) -> str | None:
    """One message, or None when there is nothing to say.

    None rather than an empty string on purpose: the caller sends only when there is a
    reply, and an empty message is a Telegram API error rather than a quiet no-op.
    """
    parts = [rendered for block in reply.blocks if (rendered := render_block(block))]
    return "\n\n".join(parts) or None


def render_block(block: Block) -> str:
    if isinstance(block, TextBlock):
        # `chat_reply` escapes, and drops a lone leading bullet the model adds to
        # single-sentence answers.
        return formatting.chat_reply(block.text)
    if isinstance(block, ItemListBlock):
        return formatting.recent(block.items, block.total)
    if isinstance(block, ErrorBlock):
        return _render_error(block.kind)
    # Exhaustive: a new Block type fails type-checking here rather than rendering as
    # silence at runtime.
    assert_never(block)


def _render_error(kind: ErrorKind) -> str:
    if kind is ErrorKind.chat_unavailable:
        return formatting.chat_unavailable()
    return formatting.failed()
