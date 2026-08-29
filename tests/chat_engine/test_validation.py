"""The check on the way out: an answer against the evidence it was generated from.

A prompt is a request. "Never invent a URL" is a sentence the model usually honours, and
the answer that breaks the rule is exactly the one nobody notices, because it reads like
all the others. These are the properties that can be checked without a second model.
"""
from __future__ import annotations

from app.services.chat_engine.validation import validate_answer

_IDS = ("a3f1c920", "b92c1d04")
_URLS = ("https://example.com/redis-guide",)


# --- empty ----------------------------------------------------------------------------


def test_an_empty_answer_is_rejected_rather_than_sent() -> None:
    """A blank message reads to the user as the bot ignoring them."""
    checked = validate_answer("   ")

    assert checked.rejected and checked.text == ""


def test_a_reply_that_was_only_a_fabrication_is_rejected() -> None:
    checked = validate_answer("[deadbeef]", allowed_ids=_IDS)

    assert checked.rejected


# --- citations ------------------------------------------------------------------------


def test_an_id_that_was_never_supplied_is_removed_and_reported() -> None:
    """The clearest fabrication signal the system has: a reference to a memory that was
    not in front of the model."""
    checked = validate_answer(
        "You saved two things [deadbeef] about Redis.", allowed_ids=_IDS
    )

    assert "deadbeef" not in checked.text
    assert checked.text == "You saved two things about Redis."
    assert checked.removed == ("unknown-id:deadbeef",)


def test_a_real_id_is_also_removed_but_not_flagged_as_unknown() -> None:
    """It is real, so it is not a fabrication -- but "[a3f1c920]" means nothing to a
    person reading the reply, and the prompt asks for titles."""
    checked = validate_answer(f"See [{_IDS[0]}] for that.", allowed_ids=_IDS)

    assert checked.text == "See for that."
    assert checked.removed == (f"id:{_IDS[0]}",)


def test_ordinary_brackets_are_left_alone() -> None:
    """The id form is matched narrowly so prose is not edited."""
    checked = validate_answer("You saved it [last week] apparently.", allowed_ids=_IDS)

    assert checked.text == "You saved it [last week] apparently."
    assert checked.clean


# --- links ----------------------------------------------------------------------------


def test_a_url_that_appears_in_no_memory_is_removed() -> None:
    """Both a false claim about the vault and a link a person is invited to tap."""
    checked = validate_answer(
        "It is at https://redis.example.invalid/guide.", allowed_urls=_URLS
    )

    assert "redis.example.invalid" not in checked.text
    assert checked.text == "It is at [link omitted]."
    assert checked.removed and checked.removed[0].startswith("unknown-url:")


def test_a_url_that_came_from_a_memory_survives() -> None:
    checked = validate_answer(
        f"You saved {_URLS[0]} in August.", allowed_urls=_URLS
    )

    assert _URLS[0] in checked.text and checked.clean


def test_a_trailing_full_stop_is_not_part_of_the_url() -> None:
    """Otherwise every link at the end of a sentence reads as invented."""
    checked = validate_answer(f"See {_URLS[0]}.", allowed_urls=_URLS)

    assert checked.clean and checked.text.endswith("guide.")


def test_a_trailing_slash_is_not_a_fabrication() -> None:
    checked = validate_answer(f"See {_URLS[0]}/", allowed_urls=_URLS)

    assert checked.clean


# --- length ---------------------------------------------------------------------------


def test_a_long_answer_is_clipped_at_a_sentence_boundary() -> None:
    """An answer that runs away from its evidence is the shape a fabrication takes."""
    body = "This is one sentence about the memory. " * 20
    checked = validate_answer(body, max_chars=200)

    assert len(checked.text) <= 200
    assert checked.text.endswith(".")
    assert "length" in checked.removed


def test_an_answer_within_the_cap_is_untouched() -> None:
    checked = validate_answer("Short and grounded.", max_chars=200)

    assert checked.text == "Short and grounded." and checked.clean


# --- shape ----------------------------------------------------------------------------


def test_paragraphs_survive_a_removal() -> None:
    """The reply is rendered in a chat window; its line breaks are load-bearing."""
    checked = validate_answer(
        "First line [deadbeef] here.\n\nSecond line.", allowed_ids=_IDS
    )

    assert checked.text == "First line here.\n\nSecond line."
