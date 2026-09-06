"""`QueryMemories`: the model composes both the filter and the projection.

It replaces the fixed `SearchMemories` / `ListMemories` pair on the agent lane. The pair
was never short of power -- it was short of expressiveness, and a model that wanted "those
two, with their links" had no way to ask. What is pinned here is the shape of that
freedom and, more importantly, its edges: what the model may *not* choose.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services.chat_engine.cards import memory_link, short_id
from app.services.chat_engine.evidence import RetrievedMemory
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.chat_engine.toolbox import QUERY_FIELDS, MemoryToolbox

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item(title: str = "Redis persistence", **kwargs: Any) -> VaultItem:
    defaults: dict[str, Any] = {
        "summary": "RDB snapshots versus the append-only file.",
        "content": "The append-only file is rewritten when it doubles in size.",
        "source_url": "https://example.com/redis",
        "ai_tags": ["redis", "databases"],
        "ai_category": "Technology",
        "processing_status": ProcessingStatus.completed,
        "created_at": datetime.now(UTC) - timedelta(hours=2),
    }
    defaults.update(kwargs)
    return VaultItem(id=uuid.uuid4(), user_id=_USER, type=ContentType.article,
                     title=title, **defaults)


class FakeRepo:
    def __init__(self, items: Sequence[VaultItem] = ()) -> None:
        self.items = list(items)
        self.filters: list[dict[str, Any]] = []

    async def list_filtered(self, user_id: uuid.UUID, **kwargs: Any) -> Any:
        self.filters.append({"user_id": user_id, **kwargs})
        return self.items, len(self.items)

    async def list_for_user(self, user_id: uuid.UUID, limit: int = 20, **kw: Any) -> Any:
        return self.items[:limit], len(self.items)


def _searching(monkeypatch: pytest.MonkeyPatch, items: Sequence[VaultItem]) -> list[str]:
    seen: list[str] = []

    async def _recall(
        self: MemoryRetriever, user_id: uuid.UUID, question: str, filters: Any = None, **kw: Any
    ) -> list[RetrievedMemory]:
        seen.append(question)
        return [RetrievedMemory(item, 0.9) for item in items]

    monkeypatch.setattr(MemoryRetriever, "recall", _recall)
    return seen


# --- the links are not optional -------------------------------------------------------


async def test_every_result_carries_both_links_whatever_was_asked_for() -> None:
    """The one thing a projection may not omit.

    A `fields` list that could drop the links is a projection that reproduces the bug this
    tool was built to fix -- an answer that cannot hand over a memory it just described.
    So they live in the header, outside the model's choice.
    """
    item = _item()
    box = MemoryToolbox(_USER, FakeRepo([item]))  # type: ignore[arg-type]

    rendered = await box.query_memories(fields=["summary"])

    assert f'url="{item.source_url}"' in rendered
    assert f'link="{memory_link(item)}"' in rendered


async def test_the_answer_may_cite_either_link() -> None:
    """Both go into the allowlist, so the guard keeps both."""
    item = _item()
    box = MemoryToolbox(_USER, FakeRepo([item]))  # type: ignore[arg-type]
    await box.query_memories()

    assert item.source_url in box.allowed_urls
    assert memory_link(item) in box.allowed_urls


# --- the projection -------------------------------------------------------------------


async def test_only_the_requested_fields_come_back() -> None:
    """The token discipline `build_card`'s clipping used to enforce, now explicit."""
    box = MemoryToolbox(_USER, FakeRepo([_item()]))  # type: ignore[arg-type]

    rendered = await box.query_memories(fields=["summary"])

    assert "summary:" in rendered
    assert "tags:" not in rendered
    assert "excerpt:" not in rendered


async def test_the_body_is_only_returned_when_it_is_asked_for() -> None:
    """A ten-row listing must not ship ten article bodies."""
    box = MemoryToolbox(_USER, FakeRepo([_item()]))  # type: ignore[arg-type]

    without = await box.query_memories(fields=["summary"])
    with_body = await box.query_memories(fields=["excerpt"])

    assert "append-only file is rewritten" not in without
    assert "append-only file is rewritten" in with_body


async def test_an_unknown_field_is_dropped_rather_than_refused() -> None:
    """The value is a model's guess at a word.

    A result missing one field is a far better answer than an error the model has to
    interpret and spend a round retrying -- the same reasoning as `_content_types`.
    """
    box = MemoryToolbox(_USER, FakeRepo([_item()]))  # type: ignore[arg-type]

    rendered = await box.query_memories(fields=["summary", "nonsense", "author"])

    assert "summary:" in rendered
    assert "nonsense" not in rendered


async def test_asking_for_nothing_returns_a_useful_default() -> None:
    box = MemoryToolbox(_USER, FakeRepo([_item()]))  # type: ignore[arg-type]

    rendered = await box.query_memories()

    assert "summary:" in rendered and "status:" in rendered


@pytest.mark.parametrize("field", QUERY_FIELDS)
async def test_every_declared_field_renders(field: str) -> None:
    """A name the schema offers the model must produce something, or it is a lie."""
    box = MemoryToolbox(_USER, FakeRepo([_item()]))  # type: ignore[arg-type]

    rendered = await box.query_memories(fields=[field])

    assert f"{field}:" in rendered


# --- which rows ------------------------------------------------------------------------


async def test_text_takes_the_ranked_path(monkeypatch: pytest.MonkeyPatch) -> None:
    item = _item()
    seen = _searching(monkeypatch, [item])
    repo = FakeRepo([item])
    box = MemoryToolbox(_USER, repo)  # type: ignore[arg-type]

    await box.query_memories(text="redis persistence")

    assert seen == ["redis persistence"]
    assert repo.filters == [], "a ranked query must not also run the listing"


async def test_no_text_takes_the_listing_path_and_costs_no_embedding() -> None:
    """The right answer to a purely time- or kind-scoped question."""
    repo = FakeRepo([_item()])
    box = MemoryToolbox(_USER, repo)  # type: ignore[arg-type]

    await box.query_memories(days=7, content_types=["article"], status="completed")

    assert len(repo.filters) == 1
    assert repo.filters[0]["user_id"] == _USER
    assert repo.filters[0]["statuses"] == [ProcessingStatus.completed]


async def test_the_row_count_is_capped() -> None:
    """A model asking for a thousand gets twenty; the card ceiling still applies above."""
    repo = FakeRepo([_item(f"m{n}") for n in range(40)])
    box = MemoryToolbox(_USER, repo)  # type: ignore[arg-type]

    await box.query_memories(limit=1000)

    assert repo.filters[0]["limit"] == 20


async def test_a_tag_filter_applies_to_a_ranked_search_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`search_semantic` has no tag predicate, so this one is applied to the results.

    Adding a JSONB containment filter to the vector query is what makes the planner drop
    the HNSW index, which is why it is not pushed down.
    """
    keep = _item("Redis", ai_tags=["redis", "databases"])
    drop = _item("Postgres", ai_tags=["postgres"])
    _searching(monkeypatch, [keep, drop])
    box = MemoryToolbox(_USER, FakeRepo())  # type: ignore[arg-type]

    rendered = await box.query_memories(text="databases", tags=["redis"])

    assert short_id(keep) in rendered
    assert short_id(drop) not in rendered


# --- the boundary that does not move --------------------------------------------------


def test_the_query_schema_has_no_tenant_field() -> None:
    """`user_id` is bound on the toolbox. A tool that took it would be one to aim."""
    from app.ai.chat.harness.schemas import QueryMemories

    fields = set(QueryMemories.model_fields)
    assert not fields & {"user_id", "user", "owner", "account", "account_id"}
