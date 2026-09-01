"""Streaming without giving up the output checks.

Streaming and validation pull against each other: "this URL came from a memory" is a
question about finished text, and a token stream is unfinished by definition. Correcting
after the fact is not available here -- the whole reason for the URL rule is that a
fabricated link is one a person is invited to *tap*, and a correction that arrives after
they have tapped it has protected nobody.

What makes it work is that the two checkable things, `[a3f1c920]` and a URL, contain no
whitespace. So the validator releases only up to the last whitespace it has seen. These
tests pin that the held-back boundary is honoured even when the dangerous token is split
across deltas, which is the case a provider produces constantly and a test author does
not think of.
"""
from __future__ import annotations

import pytest

from app.services.chat_engine.validation import StreamValidator, validate_answer


def _drain(chunks: list[str], **kwargs: object) -> tuple[str, StreamValidator]:
    checker = StreamValidator(**kwargs)  # type: ignore[arg-type]
    out = "".join(checker.feed(chunk) for chunk in chunks)
    return out + checker.finish(), checker


# --- nothing dangerous is ever displayed --------------------------------------------------


def test_an_unknown_url_never_reaches_the_reader() -> None:
    text, checker = _drain(
        ["Read it at ", "https://evil.test/phish", " today."],
        allowed_urls=["https://example.com/real"],
    )
    assert "evil.test" not in text
    assert "[link omitted]" in text
    assert any(r.startswith("unknown-url") for r in checker.removed)


def test_a_url_split_across_deltas_is_still_caught() -> None:
    """The case that decides whether this design works. A provider emits "https", "://",
    "evil", ".test" as separate tokens; a validator that checked each delta on its own
    would pass every one of them and print the URL."""
    text, _ = _drain(
        ["Go to ", "https", "://", "evil", ".test", "/x", " now."],
        allowed_urls=["https://example.com/real"],
    )
    assert "evil.test" not in text


def test_a_url_that_came_from_a_memory_survives() -> None:
    text, checker = _drain(
        ["See ", "https://example.com/real", " for it."],
        allowed_urls=["https://example.com/real"],
    )
    assert "https://example.com/real" in text
    assert checker.removed == []


def test_an_id_split_across_deltas_is_still_removed() -> None:
    text, _ = _drain(["See ", "[dead", "beef]", " for it."], allowed_ids=["aa11bb22"])
    assert "deadbeef" not in text


def test_a_known_id_is_removed_too() -> None:
    """A real id, but "[a3f1c920]" means nothing to a reader. The prompt asks for titles;
    this is what enforces it."""
    text, checker = _drain(["See ", "[aa11bb22]", " ok."], allowed_ids=["aa11bb22"])
    assert "aa11bb22" not in text
    assert checker.removed == ["id:aa11bb22"]


# --- the boundary itself --------------------------------------------------------------------


def test_the_trailing_word_is_held_until_it_is_complete() -> None:
    checker = StreamValidator()
    assert checker.feed("hello") == ""  # could still become part of a URL
    assert checker.feed(" world") == "hello "
    assert checker.finish() == "world"


def test_an_unbroken_run_is_eventually_released() -> None:
    """A model emitting a very long run with no whitespace would otherwise be buffered to
    the end of the answer, which is a stall the reader reads as a hang."""
    checker = StreamValidator()
    released = "".join(checker.feed("x" * 100) for _ in range(6))
    assert released


# --- the same rules as the finished path ------------------------------------------------------


@pytest.mark.parametrize(
    "chunks",
    [
        ["Read ", "https://evil.test/x", " now."],
        ["See ", "[deadbeef]", " there."],
        ["A plain ", "sentence ", "with nothing ", "wrong."],
    ],
)
def test_streaming_and_finished_validation_agree(chunks: list[str]) -> None:
    """Two implementations of "which links are allowed" is one that stops matching the
    other, and the one that drifts is whichever is used less."""
    streamed, _ = _drain(chunks, allowed_ids=["aa11bb22"], allowed_urls=["https://example.com/a"])
    finished = validate_answer(
        "".join(chunks), allowed_ids=["aa11bb22"], allowed_urls=["https://example.com/a"]
    )
    assert streamed.strip() == finished.text.strip()


def test_the_length_cap_stops_the_stream() -> None:
    """Applied as it goes, not at the end: the cap exists so a reply that has run away
    from its evidence stops, and stopping late is not stopping."""
    checker = StreamValidator(max_chars=20)
    out = "".join(checker.feed("word ") for _ in range(50))
    out += checker.finish()
    assert len(out) <= 22  # the clip adds an ellipsis
    assert "length" in checker.removed


def test_a_stream_that_produced_nothing_is_a_rejection() -> None:
    """A provider returning whitespace must reach the caller as a failure, not as a
    message the user reads as the bot ignoring them."""
    checker = StreamValidator()
    checker.feed("   ")
    checker.finish()
    assert checker.rejected
