"""The one place that reads Telegram's payload shape.

If this drops a field the engine never learns it existed, so the cases worth pinning are
the ones that decide a lane: whether there is text, whether there is a file, and whether
the conversation is private.
"""
from __future__ import annotations

from typing import Any

from app.services.surfaces.telegram.parse import attachments, message_text, parse_message


def _message(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "chat": {"id": 555000, "type": "private"},
        "from": {"id": 9001, "is_bot": False},
        "text": "hello",
    }
    base.update(overrides)
    return base


def test_a_plain_message_becomes_an_inbound_message() -> None:
    inbound = parse_message(_message())

    assert inbound is not None
    assert inbound.surface == "telegram"
    assert inbound.external_user_id == "9001"
    assert inbound.external_chat_id == "555000"
    assert inbound.text == "hello"
    assert inbound.attachments == [] and inbound.is_private


def test_ids_are_strings_so_no_surface_leaks_its_numbering() -> None:
    inbound = parse_message(_message())
    assert inbound is not None
    assert isinstance(inbound.external_user_id, str)
    assert isinstance(inbound.external_chat_id, str)


def test_a_group_chat_is_marked_not_private() -> None:
    inbound = parse_message(_message(chat={"id": -100, "type": "supergroup"}))
    assert inbound is not None and inbound.is_private is False


def test_a_message_with_no_sender_is_not_addressable() -> None:
    """An edited stub or a channel post: acknowledged, acted on by nobody."""
    message = _message()
    del message["from"]
    assert parse_message(message) is None


def test_a_message_with_no_chat_is_not_addressable() -> None:
    message = _message()
    del message["chat"]
    assert parse_message(message) is None


# --- text ----------------------------------------------------------------------------


def test_a_caption_counts_as_text() -> None:
    message = _message(caption="  words under a photo  ")
    del message["text"]
    assert message_text(message) == "words under a photo"


def test_no_words_at_all_is_an_empty_string() -> None:
    message = _message()
    del message["text"]
    assert message_text(message) == ""


# --- attachments ---------------------------------------------------------------------


def test_a_document_is_described_not_fetched() -> None:
    found = attachments(
        _message(
            document={
                "file_id": "BQACAgQ",
                "file_name": "notes.pdf",
                "mime_type": "application/pdf",
                "file_size": 1234,
            }
        )
    )

    assert len(found) == 1
    assert found[0].kind == "document"
    assert found[0].file_id == "BQACAgQ"
    assert found[0].file_name == "notes.pdf"
    assert found[0].size_bytes == 1234
    # No bytes anywhere: downloading is the capture pipeline's job.
    assert not hasattr(found[0], "data")


def test_a_photo_is_taken_at_its_largest_size() -> None:
    """Telegram sends a ladder of thumbnails; only the last one is worth naming."""
    found = attachments(
        _message(
            photo=[
                {"file_id": "small", "file_size": 100},
                {"file_id": "large", "file_size": 9000},
            ]
        )
    )

    assert [a.file_id for a in found] == ["large"]


def test_a_photo_carries_no_filename_which_is_normal() -> None:
    found = attachments(_message(photo=[{"file_id": "x"}]))
    assert found[0].file_name is None


def test_a_voice_note_is_still_described() -> None:
    """Refused later, out loud -- but the router has to see that a file was sent."""
    found = attachments(_message(voice={"file_id": "v", "mime_type": "audio/ogg"}))
    assert [a.kind for a in found] == ["voice"]


def test_a_malformed_attachment_does_not_crash() -> None:
    assert attachments(_message(document="not-a-dict"))[0].file_id is None


def test_no_attachment_is_an_empty_list() -> None:
    assert attachments(_message()) == []


# --- the link, resolved the way only this surface can --------------------------------


def test_a_plain_link_is_carried_on_the_message() -> None:
    url = "https://youtube.com/watch?v=xyz"
    inbound = parse_message(
        _message(text=url, entities=[{"type": "url", "offset": 0, "length": len(url)}])
    )
    assert inbound is not None and inbound.url == url


def test_a_hyperlinked_label_yields_its_target_not_its_words() -> None:
    """The whole reason the surface parses the link rather than the engine scanning it."""
    label = "click here for the recipe"
    inbound = parse_message(
        _message(
            text=label,
            entities=[
                {
                    "type": "text_link",
                    "offset": 0,
                    "length": len(label),
                    "url": "https://insta.com/reel/9",
                }
            ],
        )
    )

    assert inbound is not None
    assert inbound.url == "https://insta.com/reel/9"
    assert inbound.text == label


def test_a_message_with_no_link_carries_none() -> None:
    inbound = parse_message(_message())
    assert inbound is not None and inbound.url is None
