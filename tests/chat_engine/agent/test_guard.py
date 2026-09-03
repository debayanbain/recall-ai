"""The answer, checked against what the model was actually shown.

`validate_answer` has its own tests; what is pinned here is the layer that chooses the
inputs -- which evidence a reply is judged against, and which cap. Both are decisions the
model must not be able to influence, because a model that has invented a citation is the
same model that would happily list it as evidence.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.guard import LONG_REPLY_NO_TOOLS, guard
from app.services.chat_engine.toolbox import SurfacedSet

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item(url: str | None = "https://example.com/redis") -> VaultItem:
    return VaultItem(
        id=uuid.uuid4(),
        user_id=_USER,
        type=ContentType.article,
        title="Redis persistence",
        source_url=url,
        processing_status=ProcessingStatus.completed,
        created_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def _surfaced(*items: VaultItem) -> SurfacedSet:
    surfaced = SurfacedSet()
    for item in items:
        surfaced.add(item)
    return surfaced


# --- evidence -------------------------------------------------------------------------


def test_a_citation_of_something_never_shown_is_removed() -> None:
    result = guard("You saved it [ffffffff].", _surfaced(_item()), used_tools=True)

    assert "ffffffff" not in result.text
    assert result.ids_removed == 1


def test_a_url_in_no_block_is_replaced_rather_than_shown() -> None:
    """A fabricated link is not just a false claim -- it is one a person is invited to tap."""
    result = guard(
        "Read it at https://evil.example/phish", _surfaced(_item()), used_tools=True
    )

    assert "evil.example" not in result.text
    assert result.urls_removed == 1


def test_a_url_that_was_shown_survives() -> None:
    item = _item()
    result = guard(f"It is at {item.source_url}", _surfaced(item), used_tools=True)

    assert item.source_url is not None
    assert item.source_url in result.text
    assert result.urls_removed == 0


def test_an_empty_reply_is_a_failure_not_a_blank_message() -> None:
    assert guard("   ", _surfaced(_item()), used_tools=True).rejected


# --- the cap that replaced the scope gate ---------------------------------------------


def test_a_long_reply_with_no_tool_call_is_trimmed_and_flagged() -> None:
    """The measurement that replaced a closed regex gate.

    With no tool call there is no retrieved evidence behind a word of it, so length is
    the signal: this is the model answering in its own voice about something else. It is
    trimmed rather than refused, and the flag is what makes the rate readable.
    """
    result = guard("word " * 400, _surfaced(), used_tools=False)

    assert result.flag == LONG_REPLY_NO_TOOLS
    assert result.trimmed
    assert len(result.text) <= 600


def test_the_same_reply_is_allowed_when_tools_actually_ran() -> None:
    """Length is only suspicious without evidence. A grounded answer gets the wider cap."""
    result = guard("word " * 400, _surfaced(_item()), used_tools=True)

    assert result.flag is None
    assert len(result.text) > 600


def test_a_short_reply_with_no_tools_is_left_alone() -> None:
    """A greeting and a decline are both correct answers with no evidence behind them."""
    result = guard(
        "I can't help with that here, but I can search what you've saved.",
        _surfaced(),
        used_tools=False,
    )

    assert result.flag is None
    assert result.trimmed is False
    assert result.text.startswith("I can't help")
