"""`GetConnections`: following the edges between memories, and the edges of that.

The tool exists because ranking by similarity cannot answer "how do these two relate" --
a connection is a claim somebody made, and the only honest way to report one is to read
it. Everything pinned here is a bound on that:

* an id the model was not shown is refused, and costs no query
* every neighbour it returns becomes citable **and** linkable, or the guard deletes the
  answer's own references to it
* the relation is worded from the side the question was asked from
* a memory with no edges gets a sentence that tells the model not to invent any
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.base import ContentType, ProcessingStatus, Relation
from app.models.vault import VaultItem
from app.services.chat_engine.budget import Budget
from app.services.chat_engine.cards import memory_link, short_id
from app.services.chat_engine.toolbox import MemoryToolbox
from app.services.chat_engine.validation import validate_answer

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item(title: str, **kwargs: Any) -> VaultItem:
    defaults: dict[str, Any] = {
        "summary": f"a summary of {title}",
        "source_url": f"https://example.com/{title.replace(' ', '-')}",
        "processing_status": ProcessingStatus.completed,
        "created_at": datetime.now(UTC) - timedelta(hours=1),
    }
    defaults.update(kwargs)
    return VaultItem(
        id=uuid.uuid4(), user_id=_USER, type=ContentType.article, title=title, **defaults
    )


class _Neighbour:
    def __init__(self, item: VaultItem, relation: Relation, direction: str) -> None:
        self.item = item
        self.direction = direction
        self.connection = type("Edge", (), {"relation": relation.value})()


class FakeConnections:
    def __init__(self, neighbours: Sequence[_Neighbour] = (), total: int | None = None):
        self.neighbours = list(neighbours)
        self.total = len(self.neighbours) if total is None else total
        self.calls: list[dict[str, Any]] = []

    async def list_for_item(
        self,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        statuses: Any = None,
        relation: Any = None,
        limit: int = 6,
    ) -> tuple[list[_Neighbour], int]:
        self.calls.append(
            {"user_id": user_id, "item_id": item_id, "relation": relation, "limit": limit}
        )
        return self.neighbours, self.total


def _box(reader: FakeConnections, **kwargs: Any) -> MemoryToolbox:
    return MemoryToolbox(_USER, None, connections=reader, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------


async def test_an_id_the_model_was_never_shown_is_refused_without_a_query() -> None:
    """The `get_memory` rule, applied to the tool that walks *between* memories.

    Not secrecy -- a short id is a prefix of a UUID its owner already has. It is that a
    model told by a *memory* to expand something did not get that id from the vault, and
    a connection is precisely a route from one page's text to another's.
    """
    reader = FakeConnections()
    box = _box(reader)

    answer = await box.get_connections("deadbeef")

    assert "has been shown to you" in answer
    assert reader.calls == []


async def test_every_neighbour_becomes_citable_and_linkable() -> None:
    """Both halves, because they fail differently and both fail silently.

    An id that is rendered but not surfaced has its citation stripped from the answer as a
    fabrication. A URL that is rendered but not in `allowed_urls` is replaced with
    `[link omitted]`. In each case the model did everything right and the reader gets a
    reply with a hole in it -- which reads as a model problem and is a guard one.
    """
    focus = _item("second brain")
    other = _item("smart notes")
    box = _box(FakeConnections([_Neighbour(other, Relation.expands, "incoming")]))
    box.register_snapshot([focus])

    await box.get_connections(short_id(focus))

    assert short_id(other) in box.allowed_ids
    assert other.source_url in box.allowed_urls
    assert memory_link(other) in box.allowed_urls

    # And end to end: an answer naming both of the neighbour's links survives the guard.
    reply = f"See [{short_id(other)}] at {other.source_url} or {memory_link(other)}"
    checked = validate_answer(reply, allowed_ids=box.allowed_ids, allowed_urls=box.allowed_urls)
    assert "[link omitted]" not in checked.text


async def test_the_relation_is_worded_from_the_side_that_asked() -> None:
    """One stored row, two readings, and **the focus is the subject of the sentence**.

    Handing the model the wrong one is a quiet inversion nobody reviews: "part of" and
    "has part" are both true of the same edge, of different memories. This test pinned the
    inverted string in its first version -- the label is keyed to the focus's side, so a
    sentence starting "this memory" made the *neighbour* the subject and reversed every
    directional relation. A test can lock a bug in as easily as it can catch one.
    """
    focus = _item("the book")
    chapter = _item("the chapter")
    box = _box(FakeConnections([_Neighbour(chapter, Relation.part_of, "incoming")]))
    box.register_snapshot([focus])

    rendered = await box.get_connections(short_id(focus))

    assert f"[{short_id(focus)}] has a part in this memory" in rendered
    # One line, like every other interpolated value: a neighbour's own text must not be
    # able to open a line that reads as the start of another memory.
    connection_lines = [ln for ln in rendered.splitlines() if "connection:" in ln]
    assert len(connection_lines) == 1


async def test_a_memory_with_no_connections_is_told_not_to_invent_any() -> None:
    focus = _item("lonely")
    box = _box(FakeConnections([]))
    box.register_snapshot([focus])

    answer = await box.get_connections(short_id(focus))

    assert "no connections yet" in answer
    assert "do not describe a link you worked out yourself" in answer


async def test_an_empty_expansion_still_counts_as_having_looked() -> None:
    """`found_nothing` separates "followed the links and found none" from "never went
    looking". Without the increment, a turn whose only tool call was this one would report
    the snapshot's memories as if nothing had been searched."""
    focus = _item("lonely")
    box = _box(FakeConnections([]))
    box.register_snapshot([focus])

    await box.get_connections(short_id(focus))

    assert box.lookups == 1


async def test_past_the_budget_it_answers_instead_of_running() -> None:
    """Every call still gets a `ToolMessage`. A call left unanswered is a malformed
    conversation and providers reject the *next* request outright."""
    focus = _item("focus")
    reader = FakeConnections([_Neighbour(_item("other"), Relation.related_to, "outgoing")])
    budget = Budget(max_calls=0, max_rounds=4, wall_clock_seconds=30, max_cards=10)
    box = _box(reader, budget=budget)
    box.register_snapshot([focus])

    answer = await box.get_connections(short_id(focus))

    assert answer and reader.calls == []


async def test_a_hallucinated_relation_narrows_nothing_rather_than_failing() -> None:
    """Model output on its way to a SQL filter. Dropped rather than refused, like
    `_content_types` and `_statuses`: a guessed word should cost a wider answer, not an
    error the model has to interpret and retry."""
    focus = _item("focus")
    reader = FakeConnections([_Neighbour(_item("other"), Relation.related_to, "outgoing")])
    box = _box(reader)
    box.register_snapshot([focus])

    await box.get_connections(short_id(focus), relation="vaguely_about")

    assert reader.calls[0]["relation"] is None


async def test_a_real_relation_is_passed_through() -> None:
    focus = _item("focus")
    reader = FakeConnections([_Neighbour(_item("other"), Relation.contradicts, "outgoing")])
    box = _box(reader)
    box.register_snapshot([focus])

    await box.get_connections(short_id(focus), relation="contradicts")

    assert reader.calls[0]["relation"] is Relation.contradicts


async def test_the_limit_is_bounded() -> None:
    focus = _item("focus")
    reader = FakeConnections([])
    box = _box(reader)
    box.register_snapshot([focus])

    await box.get_connections(short_id(focus), limit=999)

    assert reader.calls[0]["limit"] == 10


async def test_the_tenant_is_never_the_model_s_to_name() -> None:
    """`user_id` is fixed on the toolbox by the caller that resolved the account, so a
    prompt injection has nothing to ask another tenant's rows with."""
    focus = _item("focus")
    reader = FakeConnections([])
    box = _box(reader)
    box.register_snapshot([focus])

    await box.get_connections(short_id(focus))

    assert reader.calls[0]["user_id"] == _USER


async def test_the_total_is_reported_alongside_what_was_shown() -> None:
    """Six neighbours out of forty must not read as forty. Same rule the vault snapshot
    follows: rows shown are never the count."""
    focus = _item("focus")
    box = _box(
        FakeConnections([_Neighbour(_item("other"), Relation.related_to, "outgoing")], total=40)
    )
    box.register_snapshot([focus])

    rendered = await box.get_connections(short_id(focus))

    assert "40 memories are connected" in rendered
    assert "1 are below" in rendered
