"""Four card fields in one schema-checked call, and the fallback that makes it safe.

Two claims are being pinned. The first is the saving: one call carrying the item once,
instead of four each carrying it again -- input tokens are where enrichment's cost lives.
The second is that nothing was traded for it. Every rule the per-field prompts carried is
still in the instructions, every value is still re-derived before it reaches a column, and
a failure still lands on the older four-call path rather than on the user.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.ai import enrichment
from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.processing_service import ProcessingService

_GOOD = {
    "summary": "Redis persists with snapshots and an append-only file.",
    "tags": ["redis", "databases"],
    "category": "Technology",
    "label": "How Redis persistence works",
}


def _answering(monkeypatch: pytest.MonkeyPatch, payload: Any) -> list[str]:
    """Stand in for the provider. Records the text it was asked about."""
    asked: list[str] = []

    async def _call(text: str) -> Any:
        asked.append(text)
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr(settings, "ENRICHMENT_COMBINED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "a-key")
    monkeypatch.setattr(enrichment, "_call_provider", _call)
    return asked


# --- the happy path ---------------------------------------------------------------------


async def test_one_call_produces_all_four_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _answering(monkeypatch, _GOOD)

    result = await enrichment.enrich("Redis persists to an append-only file.")

    assert len(asked) == 1
    assert result.summary == _GOOD["summary"]
    assert result.tags == ["redis", "databases"]
    assert result.category == "Technology"
    assert result.label == "How Redis persistence works"


async def test_the_item_is_sent_once_and_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole cost argument. Four prompts each shipped the item again."""
    asked = _answering(monkeypatch, _GOOD)

    await enrichment.enrich("x" * 50_000)

    assert len(asked) == 1
    assert len(asked[0]) == enrichment.MAX_INPUT


def test_the_schema_pins_the_category_to_the_enum() -> None:
    """An unlisted category is not rejected after the fact -- it cannot be generated.

    That matters because the per-field path mapped anything unrecognised to "Other",
    which is how a model translating the category for Bengali content silently dropped
    the item into the catch-all.
    """
    assert enrichment._SCHEMA["properties"]["category"]["enum"] == list(
        enrichment.CATEGORIES
    )
    assert enrichment._SCHEMA["additionalProperties"] is False


def test_every_per_field_language_rule_survived_the_move() -> None:
    """A rule dropped while merging four prompts into one is a regression that nothing
    reports: the output still looks like a summary, just in the wrong language."""
    text = enrichment._INSTRUCTIONS
    assert text.count("SAME LANGUAGE") == 3  # summary, tags, label
    assert "ALWAYS the English word" in text  # and the category, which must not be


# --- values still reach a column, so they are still re-derived -------------------------------


async def test_tags_are_normalised_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """`strict` guarantees a list of strings. It says nothing about case, length,
    duplicates or how many, and all four reach a JSONB column and a card."""
    _answering(
        monkeypatch,
        {
            **_GOOD,
            "tags": ["Redis", "redis", "  DATABASES  ", "x" * 200]
            + [f"t{n}" for n in range(9)],
        },
    )

    result = await enrichment.enrich("text")

    assert result.tags[:2] == ["redis", "databases"]
    assert len(result.tags) == 7
    assert all(len(tag) <= 40 for tag in result.tags)


async def test_an_unknown_category_becomes_other(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreachable while the enum holds, which is why it is checked: the guarantee
    belongs to the provider, and providers change their minds about guarantees."""
    _answering(monkeypatch, {**_GOOD, "category": "Gardening"})
    assert (await enrichment.enrich("text")).category == "Other"


async def test_a_missing_summary_is_a_failure_not_a_blank_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answering(monkeypatch, {**_GOOD, "summary": "   "})
    with pytest.raises(enrichment.EnrichmentFailed):
        await enrichment.enrich("text")


async def test_a_non_object_answer_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _answering(monkeypatch, ["not", "an", "object"])
    with pytest.raises(enrichment.EnrichmentFailed):
        await enrichment.enrich("text")


async def test_the_providers_own_words_never_reach_the_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider error can name the account it rejected, and this string is stored on
    the row and shown to its owner."""
    _answering(monkeypatch, RuntimeError("401 for org-acme key sk-abc123"))

    with pytest.raises(enrichment.EnrichmentFailed) as raised:
        await enrichment.enrich("text")

    assert "sk-abc123" not in str(raised.value)
    assert "org-acme" not in str(raised.value)


async def test_empty_content_never_reaches_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = _answering(monkeypatch, _GOOD)
    with pytest.raises(enrichment.EnrichmentFailed):
        await enrichment.enrich("   ")
    assert asked == []


# --- availability ----------------------------------------------------------------------------


def test_it_is_off_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENRICHMENT_COMBINED", True)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    assert not enrichment.enrichment_available()


def test_the_setting_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ENRICHMENT_COMBINED", False)
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "a-key")
    assert not enrichment.enrichment_available()


# --- the pipeline keeps working either way ------------------------------------------------------


class _Repo:
    def __init__(self, item: VaultItem) -> None:
        self.item = item
        self.chunks: list[dict[str, Any]] = []

    async def get_unscoped(self, _item_id: uuid.UUID) -> VaultItem:
        return self.item

    async def add(self, item: VaultItem) -> VaultItem:
        return item

    async def upsert_chunk(self, **kwargs: Any) -> None:
        self.chunks.append(kwargs)


class _AI:
    def __init__(self) -> None:
        self.called: list[str] = []

    async def generate_summary(self, _t: str) -> str:
        self.called.append("summary")
        return "fallback summary"

    async def generate_tags(self, _t: str) -> list[str]:
        self.called.append("tags")
        return ["fallback"]

    async def generate_category(self, _t: str) -> str:
        self.called.append("category")
        return "Other"

    async def generate_label(self, _t: str) -> str:
        self.called.append("label")
        return "fallback label"

    async def generate_highlights(self, _t: str) -> list[str]:
        self.called.append("highlights")
        return []

    async def generate_embedding(self, _t: str) -> list[float]:
        return [0.0] * 8


def _service(item: VaultItem, ai: _AI) -> ProcessingService:
    service = ProcessingService(_Repo(item), None, None)  # type: ignore[arg-type]
    service.ai = ai  # type: ignore[assignment]
    return service


def _item() -> VaultItem:
    return VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.note,
        title="Redis",
        content="Redis persists to an append-only file.",
        processing_status=ProcessingStatus.pending,
    )


async def test_the_pipeline_prefers_the_combined_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answering(monkeypatch, _GOOD)
    item, ai = _item(), _AI()

    await _service(item, ai)._enrich(item)

    assert item.summary == _GOOD["summary"]
    assert ai.called == ["highlights"]  # the four per-field calls were not made


async def test_a_failed_combined_call_falls_back_rather_than_failing_the_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is a complete implementation of the same job, so paying for it beats
    failing an item over what is only an optimisation."""
    _answering(monkeypatch, RuntimeError("provider down"))
    item, ai = _item(), _AI()

    await _service(item, ai)._enrich(item)

    assert item.summary == "fallback summary"
    assert item.processing_status is ProcessingStatus.completed
    assert sorted(ai.called) == ["category", "highlights", "label", "summary", "tags"]
