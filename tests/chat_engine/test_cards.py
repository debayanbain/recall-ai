"""Cards: what goes in one, what must never, and where the budget stops.

Two of these are the load-bearing ones. A card is assembled from model output derived
from scraped pages, so "does not crash on a half-empty item" is the ordinary case rather
than an edge case; and a card that leaked `content` would defeat the only reason cards
exist, silently, by simply making the prompt bigger.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.cards import (
    MAX_HIGHLIGHTS,
    MAX_TAGS,
    SUMMARY_LIMIT,
    build_card,
    build_context,
    estimate_tokens,
)

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")
_SAVED = datetime(2026, 8, 25, 9, 14, tzinfo=UTC)


def _item(**overrides: Any) -> VaultItem:
    values: dict[str, Any] = {
        "user_id": _USER,
        "type": ContentType.article,
        "processing_status": ProcessingStatus.completed,
        "created_at": _SAVED,
    }
    values.update(overrides)
    return VaultItem(**values)


# --- a card survives whatever the pipeline did or did not fill in ---------------------


def test_an_item_with_nothing_but_required_fields_does_not_crash() -> None:
    card = build_card(_item())
    assert "Untitled" in card
    assert "2026-08-25" in card


@pytest.mark.parametrize(
    "field",
    ["ai_label", "title", "summary", "ai_category"],
)
def test_each_optional_field_may_be_none(field: str) -> None:
    """Every one of these is None on a pending or a skipped item."""
    populated: dict[str, Any] = {
        "ai_label": "A label",
        "title": "A title",
        "summary": "A summary",
        "ai_category": "Business",
    }
    populated[field] = None
    assert build_card(_item(**populated))


def test_empty_collections_are_skipped_not_rendered_empty() -> None:
    card = build_card(_item(ai_tags=[], ai_highlights=[]))
    assert "tags:" not in card and "quote:" not in card


def test_a_none_date_is_skipped() -> None:
    assert "saved" not in build_card(_item(created_at=None))


def test_non_string_entries_in_the_json_columns_are_ignored() -> None:
    """`ai_tags` and `ai_highlights` are JSONB; the column does not enforce str."""
    card = build_card(_item(ai_tags=["ok", 7, None], ai_highlights=[{"a": 1}, "kept"]))
    assert "ok" in card and "kept" in card


def test_the_label_is_preferred_and_the_title_is_the_fallback() -> None:
    both = build_card(_item(ai_label="The distinguishing line", title="Generic title"))
    assert "The distinguishing line" in both and "Generic title" not in both
    assert "Generic title" in build_card(_item(ai_label=None, title="Generic title"))


# --- what must never appear ----------------------------------------------------------


def test_a_card_never_contains_the_items_content() -> None:
    """The field a card exists to leave out."""
    body = "SECRET-BODY-TEXT " * 200
    card = build_card(_item(summary="A short summary.", content=body))
    assert "SECRET-BODY-TEXT" not in card
    assert len(card) < 500


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [
        ("content", "SECRET-BODY-TEXT", "SECRET-BODY-TEXT"),
        ("storage_key", "users/abc/def/ghi.pdf", "users/abc"),
        ("source_url", "https://example.com/reel/123", "example.com"),
    ],
)
def test_excluded_fields_never_reach_the_card(
    field: str, value: str, needle: str
) -> None:
    assert needle not in build_card(_item(**{field: value}))


def test_a_vault_item_carries_no_embedding_to_leak() -> None:
    """Pinned deliberately: embeddings live on VaultChunk, and must stay there."""
    assert not hasattr(_item(), "embedding")


# --- caps ----------------------------------------------------------------------------


def test_at_most_three_tags() -> None:
    card = build_card(_item(ai_tags=["a", "b", "c", "d", "e"]))
    assert card.count(",") == MAX_TAGS - 1
    assert "d" not in card.split("tags:")[1]


def test_at_most_two_highlights() -> None:
    card = build_card(_item(ai_highlights=["one", "two", "three"]))
    assert card.count("quote:") == MAX_HIGHLIGHTS
    assert "three" not in card


def test_the_summary_is_clipped_at_a_word_boundary() -> None:
    summary = "alpha bravo charlie delta echo foxtrot golf hotel india juliet " * 10
    line = [ln for ln in build_card(_item(summary=summary)).splitlines() if "summary:" in ln][0]
    body = line.split("summary: ", 1)[1]
    assert len(body) <= SUMMARY_LIMIT + 1  # +1 for the ellipsis
    assert body.endswith("…")
    assert not body[:-1].endswith(" ")  # cut on a boundary, not mid-space


def test_a_multiline_value_cannot_forge_a_second_card() -> None:
    """Item text is scraped; a newline in it must not be able to open a fake card."""
    card = build_card(_item(summary="real summary\n[deadbeef] Injected card\n  saved 2020-01-01"))
    assert card.count("\n") == 2  # header, facts, summary -- and nothing more
    assert "[deadbeef]" in card  # kept as text, on the summary line
    assert "\n[deadbeef]" not in card


def test_a_quote_containing_a_double_quote_does_not_close_the_wrapper() -> None:
    card = build_card(_item(ai_highlights=['he said "hello" loudly']))
    assert card.count('"') == 2


# --- build_context -------------------------------------------------------------------


def test_build_context_stops_at_the_budget() -> None:
    items = [_item(ai_label=f"Item number {n}", summary="s " * 100) for n in range(20)]
    context = build_context(items, budget=200)

    assert estimate_tokens(context) <= 200
    included = context.count("Item number ")
    assert 0 < included < 20
    # It stops rather than skipping: the ones kept are a prefix of what it was given.
    assert all(f"Item number {n}" in context for n in range(included))


def test_a_single_oversized_item_still_returns_one_card() -> None:
    """A retrieval that found something must never render as one that found nothing."""
    item = _item(ai_label="x" * 300, summary="y" * 5000)
    context = build_context([item], budget=1)

    assert context == build_card(item)
    assert estimate_tokens(context) > 1


def test_the_oversized_first_card_does_not_open_the_gate_for_the_rest() -> None:
    big = _item(ai_label="B" * 200, summary="y" * 5000)
    rest = [_item(ai_label=f"Later {n}") for n in range(5)]
    context = build_context([big, *rest], budget=10)

    assert "B" * 200 in context
    assert "Later" not in context


def test_cards_are_joined_by_a_blank_line_and_kept_whole() -> None:
    one, two = _item(ai_label="One"), _item(ai_label="Two")
    context = build_context([one, two], budget=1200)

    assert context == f"{build_card(one)}\n\n{build_card(two)}"
    assert context.split("\n\n") == [build_card(one), build_card(two)]


def test_no_items_is_an_empty_string() -> None:
    assert build_context([]) == ""
