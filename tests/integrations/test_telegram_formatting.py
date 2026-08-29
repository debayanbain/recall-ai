"""Reply text: escaping, and the promises the bot is allowed to make.

Titles, tags and categories are model output derived from scraped pages, so a caption
containing `<b>` or `&` is ordinary input rather than an edge case. Telegram's HTML parse
mode rejects the whole message on malformed markup, so a missed escape is not a cosmetic
bug -- the user gets nothing at all.
"""
from __future__ import annotations

import uuid
from typing import Any

from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.telegram import formatting
from app.services.telegram.capture import _filename_for, first_url

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item(**overrides: Any) -> VaultItem:
    values: dict[str, Any] = {
        "user_id": _USER,
        "type": ContentType.article,
        "title": "Pasta",
        "processing_status": ProcessingStatus.completed,
    }
    values.update(overrides)
    return VaultItem(**values)


def test_markup_in_a_title_is_escaped() -> None:
    text = formatting.result(_item(title="<script>alert(1)</script> & more"))
    assert "<script>" not in text
    assert "&lt;script&gt;" in text and "&amp; more" in text


def test_markup_in_tags_and_category_is_escaped() -> None:
    text = formatting.result(
        _item(ai_category="Tech <b>", ai_tags=["a&b", "<i>x</i>"], summary="ok")
    )
    assert "<b>\n" not in text
    assert "Tech &lt;b&gt;" in text
    assert "a&amp;b" in text and "&lt;i&gt;x&lt;/i&gt;" in text


def test_a_failed_item_says_so_rather_than_claiming_success() -> None:
    text = formatting.result(_item(processing_status=ProcessingStatus.failed))
    assert "Couldn't process" in text
    assert "✅" not in text


def test_a_stored_only_file_does_not_promise_tags() -> None:
    text = formatting.stored_without_text(_item(title="scan.png", type=ContentType.image))
    assert "can't read this file type" in text
    assert "tag" in text


def test_recent_escapes_urls_it_links() -> None:
    item = _item(title="A & B", source_url="https://example.com/?a=1&b=2")
    text = formatting.recent([item], total=1)
    assert 'href="https://example.com/?a=1&amp;b=2"' in text
    assert ">A &amp; B<" in text


def test_recent_handles_an_empty_vault() -> None:
    assert "Nothing saved yet" in formatting.recent([], total=0)


def test_url_entity_offsets_are_utf16_counted() -> None:
    """Telegram counts UTF-16 code units; an emoji shifts every later offset by one."""
    text = "🍝 https://example.com/x"
    message = {
        "text": text,
        "entities": [{"type": "url", "offset": 3, "length": 23}],
    }
    assert first_url(message) == "https://example.com/x"


def test_non_http_schemes_are_not_treated_as_links() -> None:
    assert first_url({"text": "file:///etc/passwd"}) is None
    assert first_url({"text": "javascript:alert(1)"}) is None


def test_photo_without_a_filename_gets_one_from_the_file_path() -> None:
    """Telegram photos carry no filename, and the allowlist reads the extension."""
    assert _filename_for({}, "photos/file_12.jpg") == "telegram-upload.jpg"


def test_filename_falls_back_to_the_declared_mime_type() -> None:
    assert _filename_for({"mime_type": "application/pdf"}, "documents/file_9") == (
        "telegram-upload.pdf"
    )


def test_unidentifiable_file_is_left_extensionless_to_be_refused() -> None:
    assert _filename_for({"mime_type": "application/x-msdownload"}, "docs/f") == (
        "telegram-upload"
    )


def test_a_supplied_filename_is_kept() -> None:
    assert _filename_for({"file_name": "notes.pdf"}, "documents/file_1.bin") == "notes.pdf"


def test_connect_markup_is_one_button_to_the_configured_frontend(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://app.example.com/")
    markup = formatting.connect_markup()
    assert markup == {
        "inline_keyboard": [
            [{"text": "Connect my account", "url": "https://app.example.com/capture"}]
        ]
    }


def test_connect_markup_is_omitted_for_a_plaintext_frontend(monkeypatch: Any) -> None:
    """Telegram rejects a button it cannot reach, and a rejected sendMessage is silence.

    The local default is `http://localhost:3000`, so the fallback is the common case in
    development: the user still gets the text telling them how to connect.
    """
    monkeypatch.setattr(settings, "FRONTEND_URL", "http://localhost:3000")
    assert formatting.connect_markup() is None


# --- chat_reply ----------------------------------------------------------------------


def test_a_lone_leading_dash_is_stripped_from_a_one_line_reply() -> None:
    """The reported bug: "Hii" came back as a one-item bullet list."""
    assert formatting.chat_reply("- Hey! Send me a link and I'll keep it.") == (
        "Hey! Send me a link and I'll keep it."
    )


def test_asterisk_and_hash_markers_are_stripped_too() -> None:
    assert formatting.chat_reply("* Hello there") == "Hello there"
    assert formatting.chat_reply("# Hello there") == "Hello there"
    assert formatting.chat_reply("## Hello there") == "Hello there"


def test_a_real_list_keeps_every_marker() -> None:
    """Multi-line means the first "-" has siblings; eating it would misalign the list."""
    reply = "- one\n- two"
    assert formatting.chat_reply(reply) == reply


def test_a_marker_character_that_is_part_of_the_words_survives() -> None:
    """Whitespace after the marker is required, so content is never trimmed."""
    assert formatting.chat_reply("-5 degrees tonight") == "-5 degrees tonight"
    assert formatting.chat_reply("#1 pick") == "#1 pick"
    assert formatting.chat_reply("**bold**") == "**bold**"


def test_chat_reply_still_escapes() -> None:
    """Stripping is presentation; escaping is the security boundary and stays."""
    assert formatting.chat_reply("- <b>hi</b> & bye") == "&lt;b&gt;hi&lt;/b&gt; &amp; bye"


def test_chat_reply_escapes_markup_hiding_behind_a_marker() -> None:
    assert "<script>" not in formatting.chat_reply("- <script>alert(1)</script>")
