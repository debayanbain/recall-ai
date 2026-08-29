"""Blocks -> Telegram HTML: the one place that translation happens.

Telegram rejects the whole message on malformed markup, so a missed escape is not a
cosmetic bug -- the user receives nothing. Block text is model output derived from
scraped pages, which makes a caption containing `<b>` ordinary input.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.types import (
    ErrorBlock,
    ErrorKind,
    ItemListBlock,
    OutboundReply,
    TextBlock,
)
from app.services.surfaces.telegram.render import render

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item(**overrides: Any) -> VaultItem:
    values: dict[str, Any] = {
        "user_id": _USER,
        "type": ContentType.article,
        "title": "Pasta",
        "processing_status": ProcessingStatus.completed,
        "created_at": datetime(2026, 8, 25, tzinfo=UTC),
    }
    values.update(overrides)
    return VaultItem(**values)


# --- escaping ------------------------------------------------------------------------


def test_markup_in_prose_is_escaped() -> None:
    out = render(OutboundReply([TextBlock(text="<script>alert(1)</script> & more")]))
    assert out is not None
    assert "<script>" not in out
    assert "&lt;script&gt;" in out and "&amp; more" in out


def test_markup_in_a_listed_title_is_escaped() -> None:
    out = render(OutboundReply([ItemListBlock(items=[_item(title="<b>x</b> & y")], total=1)]))
    assert out is not None
    assert "<b>x</b>" not in out
    assert "&lt;b&gt;x&lt;/b&gt; &amp; y" in out


def test_a_listed_url_is_escaped_inside_the_href() -> None:
    """An href is markup too: an unescaped quote there breaks the whole message."""
    item = _item(source_url='https://example.com/a"onmouseover="x')
    out = render(OutboundReply([ItemListBlock(items=[item], total=1)]))
    assert out is not None and 'onmouseover="x' not in out


# --- block kinds ---------------------------------------------------------------------


def test_a_lone_leading_bullet_is_still_stripped_from_prose() -> None:
    out = render(OutboundReply([TextBlock(text="- Hey there")]))
    assert out == "Hey there"


def test_a_listing_renders_as_a_list() -> None:
    out = render(OutboundReply([ItemListBlock(items=[_item()], total=3)]))
    assert out is not None and "of 3" in out and "Pasta" in out


def test_each_error_kind_has_its_own_wording() -> None:
    failure = render(OutboundReply([ErrorBlock(ErrorKind.provider_failure)]))
    unavailable = render(OutboundReply([ErrorBlock(ErrorKind.chat_unavailable)]))

    assert failure is not None and "went wrong" in failure
    assert unavailable is not None and "can't chat" in unavailable
    assert failure != unavailable


# --- the message as a whole ----------------------------------------------------------


def test_nothing_to_say_renders_as_nothing_rather_than_an_empty_message() -> None:
    """Telegram rejects an empty sendMessage; the caller must be able to skip it."""
    assert render(OutboundReply()) is None
    assert render(OutboundReply([TextBlock(text="   ")])) is None


def test_several_blocks_are_separated_by_a_blank_line() -> None:
    out = render(OutboundReply([TextBlock(text="one"), TextBlock(text="two")]))
    assert out == "one\n\ntwo"
