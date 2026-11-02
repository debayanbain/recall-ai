"""Blocks -> Telegram HTML: the one place that translation happens.

Telegram rejects the whole message on malformed markup, so a missed escape is not a
cosmetic bug -- the user receives nothing. Block text is model output derived from
scraped pages, which makes a caption containing `<b>` ordinary input.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.types import (
    ConnectedMemory,
    ConnectionsBlock,
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


# --- connected memories --------------------------------------------------------------


def _connected(item: VaultItem, about: str = "the memory") -> OutboundReply:
    memory = ConnectedMemory(item=item, phrase="expands", about=about)
    return OutboundReply([ConnectionsBlock(memories=(memory,))])


def test_a_suggestion_says_what_it_is_connected_to_and_how(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recall.example")
    item = _item(id=uuid.uuid4(), title="Moving to Germany")

    out = render(_connected(item, about="Job sources"))

    assert out == (
        "🔗 <b>Connected in your vault</b>\n"
        f'• <a href="https://recall.example/memory/{item.id}">Moving to Germany</a>'
        " — expands <i>Job sources</i>"
    )


def test_a_suggestion_links_to_the_vault_and_never_to_where_the_page_came_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This list is an invitation to tap, sitting under an answer the person already
    trusts, and a saved page's address was chosen by whoever wrote that page."""
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recall.example")
    item = _item(id=uuid.uuid4(), source_url="https://attacker.example/phish")

    out = render(_connected(item))

    assert out is not None
    assert "attacker.example" not in out
    assert f"https://recall.example/memory/{item.id}" in out


def test_a_title_cannot_open_markup_in_a_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both titles are scraped text. One unescaped `<` loses the person the whole message,
    including the answer above it."""
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recall.example")
    item = _item(id=uuid.uuid4(), title='<script>x</script> & "y"')

    out = render(_connected(item, about="<b>about</b> & more"))

    assert out is not None
    assert "<script>" not in out and "<b>about</b>" not in out
    assert "&lt;script&gt;" in out and "&lt;b&gt;about&lt;/b&gt; &amp; more" in out


def test_an_address_telegram_cannot_open_is_not_linked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local default. `connect_markup` makes the same call for the same reason."""
    monkeypatch.setattr(settings, "FRONTEND_URL", "http://localhost:3000")

    out = render(_connected(_item(id=uuid.uuid4(), title="Pasta")))

    assert out is not None
    assert "<a " not in out and "<b>Pasta</b>" in out


def test_a_long_title_is_clipped_so_one_line_stays_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://recall.example")

    out = render(_connected(_item(id=uuid.uuid4(), title="x" * 500), about="y" * 500))

    assert out is not None
    assert "x" * 81 not in out and "y" * 51 not in out


def test_the_suggestions_sit_under_the_answer() -> None:
    memory = ConnectedMemory(item=_item(id=uuid.uuid4()), phrase="expands", about="a")

    out = render(
        OutboundReply([TextBlock(text="The answer."), ConnectionsBlock(memories=(memory,))])
    )

    assert out is not None
    assert out.startswith("The answer.\n\n🔗 <b>Connected in your vault</b>")


# --- the message as a whole ----------------------------------------------------------


def test_nothing_to_say_renders_as_nothing_rather_than_an_empty_message() -> None:
    """Telegram rejects an empty sendMessage; the caller must be able to skip it."""
    assert render(OutboundReply()) is None
    assert render(OutboundReply([TextBlock(text="   ")])) is None


def test_several_blocks_are_separated_by_a_blank_line() -> None:
    out = render(OutboundReply([TextBlock(text="one"), TextBlock(text="two")]))
    assert out == "one\n\ntwo"
