"""What gets offered beside an answer, and what must never be.

A suggestion is a tappable claim that two memories belong together, placed under an answer
the person is already inclined to trust. So most of what is pinned here is what it must not
do: follow an id the model invented, anchor on the snapshot and attach the same list to
every reply, word a directional relation backwards, or cost anyone their answer.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.ai.chat import history
from app.ai.chat.harness import graph
from app.ai.chat.harness.schemas import FinalAnswer, GetConnections
from app.core.config import settings
from app.models.base import ContentType, ProcessingStatus, Relation
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id
from app.services.chat_engine.engine import _as_events, _to_reply
from app.services.chat_engine.evidence import RetrievedMemory
from app.services.chat_engine.retrieval import MemoryRetriever
from app.services.chat_engine.suggestions import cited_ids, connected_to_answer
from app.services.chat_engine.toolbox import MemoryToolbox
from app.services.chat_engine.types import (
    ConnectedMemory,
    ConnectionsBlock,
    ConnectionsEvent,
    Delta,
    OutboundReply,
    ProposalBlock,
    QuestionBlock,
    StreamEnd,
    TextBlock,
)
from app.services.chat_engine.validation import validate_answer
from app.services.recall_agent import RecallAgentService
from app.services.recall_chat import RecallAnswer

_USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _item(title: str, **kwargs: Any) -> VaultItem:
    defaults: dict[str, Any] = {
        "summary": f"a summary of {title}",
        "content": f"the body of {title}",
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


class _Reader:
    """Records what it was asked, and answers `(asked_about, neighbour)` rows."""

    def __init__(
        self,
        rows: Sequence[tuple[uuid.UUID, _Neighbour]] = (),
        error: Exception | None = None,
    ) -> None:
        self.rows = list(rows)
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def list_for_item(self, *_: Any, **__: Any) -> Any:
        return [], 0

    async def neighbours_of_many(
        self,
        user_id: uuid.UUID,
        item_ids: Sequence[uuid.UUID],
        *,
        exclude: Sequence[uuid.UUID] = (),
        limit: int = 24,
    ) -> list[tuple[uuid.UUID, _Neighbour]]:
        self.calls.append(
            {"user_id": user_id, "item_ids": list(item_ids), "exclude": list(exclude),
             "limit": limit}
        )
        if self.error is not None:
            raise self.error
        return self.rows


def _box(reader: Any, repo: Any = None) -> MemoryToolbox:
    return MemoryToolbox(_USER, repo, connections=reader)


# --- which memories it looks around ---------------------------------------------------


async def test_a_cited_memory_is_what_gets_looked_around() -> None:
    cited, ignored = _item("cited"), _item("found but unused")
    reader = _Reader()
    box = _box(reader)
    box.surfaced.add(cited)
    box.surfaced.add(ignored)
    box.matched.extend([ignored, cited])

    await connected_to_answer(box, cited=[short_id(cited)], limit=3)

    assert reader.calls[0]["item_ids"] == [cited.id]
    assert reader.calls[0]["user_id"] == _USER


async def test_an_id_the_model_invented_looks_nothing_up() -> None:
    """A citation is model output. If it chose the lookup unchecked, a caption telling the
    model to cite something would decide what the person is offered."""
    reader = _Reader()

    assert await connected_to_answer(_box(reader), cited=["deadbeef"], limit=3) == ()
    assert reader.calls == []


async def test_with_no_citation_it_falls_back_to_what_a_search_found() -> None:
    first, second = _item("first"), _item("second")
    reader = _Reader()
    box = _box(reader)
    box.surfaced.add(first)
    box.surfaced.add(second)
    box.matched.extend([first, second, first])

    await connected_to_answer(box, cited=[], limit=3)

    # Relevance order survives, and a row found twice is looked around once.
    assert reader.calls[0]["item_ids"] == [first.id, second.id]


async def test_the_snapshot_alone_suggests_nothing() -> None:
    """The bug this exists to prevent. Those rows are in every prompt, so anchoring on them
    would put the same unrelated list under every reply, including a greeting."""
    reader = _Reader()
    box = _box(reader)
    box.register_snapshot([_item("newest"), _item("second newest")])

    assert await connected_to_answer(box, cited=[], limit=3) == ()
    assert reader.calls == []


async def test_a_row_the_model_was_never_shown_anchors_nothing() -> None:
    """The card budget can cut a result after the search found it."""
    reader = _Reader()
    box = _box(reader)
    box.matched.append(_item("cut by the budget"))

    assert await connected_to_answer(box, cited=[], limit=3) == ()
    assert reader.calls == []


async def test_the_number_of_memories_looked_around_is_bounded() -> None:
    items = [_item(f"row {n}") for n in range(6)]
    reader = _Reader()
    box = _box(reader)
    for item in items:
        box.surfaced.add(item)

    await connected_to_answer(box, cited=[short_id(item) for item in items], limit=3)

    assert reader.calls[0]["item_ids"] == [item.id for item in items[:3]]
    # All six are still hidden from the result: the answer already named them.
    assert reader.calls[0]["exclude"] == [item.id for item in items]


def test_cited_ids_reads_both_the_declaration_and_the_prose() -> None:
    """Prose citations are scrubbed before anyone reads them, so `removed` is the only
    record of them. Only a *real* id is recorded under this prefix."""
    checked = validate_answer(
        "See [aaaaaaaa] and [bbbbbbbb].", allowed_ids=["aaaaaaaa"], allowed_urls=[]
    )

    assert cited_ids(["[cccccccc] "], checked.removed) == ["cccccccc", "aaaaaaaa"]


def test_cited_ids_reads_a_group_written_in_one_pair_of_brackets() -> None:
    """The form a live model actually used. Unread, the answer's citations were lost and it
    fell back to whatever the search found, or to nothing."""
    checked = validate_answer(
        "Closely related.[aaaaaaaa,bbbbbbbb]",
        allowed_ids=["aaaaaaaa", "bbbbbbbb"],
        allowed_urls=[],
    )

    assert checked.text == "Closely related."
    assert cited_ids([], checked.removed) == ["aaaaaaaa", "bbbbbbbb"]


# --- what it says ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("relation", "direction", "phrase"),
    [
        # Asked about the book. The chapter is `part_of` the book, so the edge is incoming
        # from the book's side, and the chapter "is part of" it.
        (Relation.part_of, "incoming", "is part of"),
        (Relation.part_of, "outgoing", "has a part in"),
        (Relation.expands, "incoming", "expands"),
        (Relation.expands, "outgoing", "is expanded by"),
        (Relation.related_to, "outgoing", "is related to"),
    ],
)
async def test_a_relation_is_worded_with_the_neighbour_as_its_subject(
    relation: Relation, direction: str, phrase: str
) -> None:
    """A quiet inversion here tells someone the book is part of its own chapter, and nobody
    reviews a sentence that reads fine."""
    anchor, other = _item("anchor"), _item("other")
    box = _box(_Reader([(anchor.id, _Neighbour(other, relation, direction))]))
    box.surfaced.add(anchor)

    memories = await connected_to_answer(box, cited=[short_id(anchor)], limit=3)

    assert memories == (ConnectedMemory(item=other, phrase=phrase, about="anchor"),)


async def test_a_neighbour_reached_twice_is_offered_once_and_the_limit_holds() -> None:
    one, two = _item("one"), _item("two")
    shared, also, late = _item("shared"), _item("also"), _item("late")
    reader = _Reader(
        [
            (one.id, _Neighbour(shared, Relation.related_to, "outgoing")),
            (two.id, _Neighbour(shared, Relation.expands, "incoming")),
            (two.id, _Neighbour(also, Relation.related_to, "outgoing")),
            (two.id, _Neighbour(late, Relation.related_to, "outgoing")),
        ]
    )
    box = _box(reader)
    box.surfaced.add(one)
    box.surfaced.add(two)

    memories = await connected_to_answer(box, cited=[short_id(one), short_id(two)], limit=2)

    assert [memory.item.title for memory in memories] == ["shared", "also"]
    # The first reading wins, and it names the memory that reading came from.
    assert memories[0].about == "one"
    # Read past the limit, or the duplicate above would have cost a real suggestion.
    assert reader.calls[0]["limit"] > 2


# --- when it stays out of the way -----------------------------------------------------


async def test_a_failed_read_costs_the_suggestions_and_nothing_else() -> None:
    anchor = _item("anchor")
    box = _box(_Reader(error=RuntimeError("connection reset")))
    box.surfaced.add(anchor)

    assert await connected_to_answer(box, cited=[short_id(anchor)], limit=3) == ()


async def test_zero_turns_it_off_without_a_statement() -> None:
    anchor = _item("anchor")
    reader = _Reader()
    box = _box(reader)
    box.surfaced.add(anchor)

    assert await connected_to_answer(box, cited=[short_id(anchor)], limit=0) == ()
    assert reader.calls == []


async def test_a_reader_without_the_method_is_not_an_error() -> None:
    """Every older fake, and any deployment wired with a narrower reader."""

    class _Narrow:
        async def list_for_item(self, *_: Any, **__: Any) -> Any:
            return [], 0

    anchor = _item("anchor")
    box = _box(_Narrow())
    box.surfaced.add(anchor)

    assert await connected_to_answer(box, cited=[short_id(anchor)], limit=3) == ()


# --- what the tools record ------------------------------------------------------------


def _searching(monkeypatch: pytest.MonkeyPatch, memories: list[RetrievedMemory]) -> None:
    async def _recall(*_: Any, **__: Any) -> list[RetrievedMemory]:
        return memories

    monkeypatch.setattr(MemoryRetriever, "recall", _recall)


async def test_a_supported_search_records_its_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hit = _item("hit")
    _searching(monkeypatch, [RetrievedMemory(hit, 0.9)])
    box = _box(_Reader())

    await box.query_memories(text="something")

    assert box.matched == [hit]


async def test_a_weak_match_is_still_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Weak" hedges the wording and says nothing about whether the row is right. A measured
    true match scores 0.37 here against a strong line of 0.45, and a live question using
    nearly a memory's own title landed in this band. Excluding it cost that turn its list."""
    monkeypatch.setattr(settings, "RECALL_MIN_SCORE", 0.3)
    monkeypatch.setattr(settings, "RECALL_STRONG_SCORE", 0.45)
    weak = _item("weak but true")
    _searching(monkeypatch, [RetrievedMemory(weak, 0.37)])
    box = _box(_Reader())

    rendered = await box.query_memories(text="something")

    assert "WEAK" in rendered
    assert box.matched == [weak]


async def test_a_row_below_the_floor_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RECALL_MIN_SCORE", 0.3)
    _searching(monkeypatch, [RetrievedMemory(_item("noise"), 0.2)])
    box = _box(_Reader())

    await box.query_memories(text="something")

    assert box.matched == []


async def test_a_plain_listing_records_nothing() -> None:
    """A listing has no subject. "Show my last ten" is not about those ten."""

    class _Repo:
        async def list_filtered(self, *_: Any, **__: Any) -> Any:
            return [_item("listed")], 1

    box = _box(_Reader(), _Repo())

    await box.query_memories()

    assert box.matched == []


async def test_opening_a_memory_puts_it_first() -> None:
    first, opened = _item("first"), _item("opened")
    box = _box(_Reader())
    box.surfaced.add(opened)
    box.matched.append(first)

    await box.get_memory(short_id(opened))

    assert box.matched == [opened, first]


# --- the lane -------------------------------------------------------------------------


class _Lane:
    """The real `stream_agent` around a scripted turn: no model, no Redis, no database."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        reader: Any,
        *,
        said: str | None = "Here is your answer.",
        final: FinalAnswer | None = None,
        calls: Sequence[str] = ("QueryMemories",),
        failed: bool = False,
        before: Any = None,
    ) -> None:
        self.history: list[str] = []
        self.anchor = _item("anchor")
        anchor = self.anchor

        async def _run(
            question: str, past: Any, executor: MemoryToolbox, **_: Any
        ) -> AsyncIterator[graph.AgentEvent]:
            executor.surfaced.add(anchor)
            executor.matched.append(anchor)
            if before is not None:
                before(executor)
            if said:
                yield graph.AgentDelta(text=said)
            yield graph.AgentEnd(calls=list(calls), final=final, failed=failed)

        async def _load(_: str) -> list[Any]:
            return []

        async def _append(_session: str, question: str, answer: str) -> None:
            self.history.append(answer)

        async def _snapshot(*_: Any, **__: Any) -> None:
            return None

        monkeypatch.setattr(graph, "run_agent", _run)
        monkeypatch.setattr(history, "load", _load)
        monkeypatch.setattr(history, "append", _append)
        monkeypatch.setattr(RecallAgentService, "_snapshot", _snapshot)
        self.service = RecallAgentService(None, None, reader)  # type: ignore[arg-type]

    async def events(self) -> list[Any]:
        return [event async for event in self.service.stream_agent(_USER, "q", "session")]


def _connected() -> _Reader:
    return _Reader([(uuid.uuid4(), _Neighbour(_item("neighbour"), Relation.expands, "incoming"))])


def _shape(events: Sequence[Any]) -> list[type]:
    """The event types in order, one per run. The validator releases prose in fragments, so
    how many deltas a sentence takes is its business and not what these tests are about."""
    kinds: list[type] = []
    for event in events:
        if not kinds or kinds[-1] is not type(event):
            kinds.append(type(event))
    return kinds


async def test_the_suggestions_arrive_after_the_words_and_before_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = await _Lane(monkeypatch, _connected()).events()

    assert _shape(events) == [Delta, ConnectionsEvent, StreamEnd]
    assert events[-2].memories[0].item.title == "neighbour"


async def test_a_final_answer_turn_gets_them_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prompt ends every turn with `FinalAnswer`, whose text was never streamed, so this
    is the path a real turn usually takes rather than an edge case."""
    final = FinalAnswer(text="The answer.", cited_ids=[])
    events = await _Lane(monkeypatch, _connected(), said=None, final=final).events()

    assert _shape(events) == [Delta, ConnectionsEvent, StreamEnd]


async def test_a_suggestion_never_reaches_chat_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """History is replayed into the next prompt, and a neighbour's title is scraped text.
    This is the line that keeps the block out of every later prompt."""
    lane = _Lane(monkeypatch, _connected())

    await lane.events()

    assert lane.history == ["Here is your answer."]


async def test_a_decline_gets_none(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = _connected()
    final = FinalAnswer(text="I can't help with that.", declined_out_of_scope=True)
    events = await _Lane(monkeypatch, reader, said=None, final=final).events()

    assert not any(isinstance(event, ConnectionsEvent) for event in events)
    assert reader.calls == []


async def test_a_question_back_gets_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _asked(box: MemoryToolbox) -> None:
        box.question = QuestionBlock(question="Which one?")

    reader = _connected()
    events = await _Lane(monkeypatch, reader, said=None, before=_asked).events()

    assert not any(isinstance(event, ConnectionsEvent) for event in events)
    assert reader.calls == []


async def test_a_proposal_gets_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def _proposed(box: MemoryToolbox) -> None:
        box.proposal = ProposalBlock(preview="a note", accept_token="t")

    reader = _connected()
    events = await _Lane(monkeypatch, reader, before=_proposed).events()

    assert not any(isinstance(event, ConnectionsEvent) for event in events)
    assert reader.calls == []


async def test_a_turn_that_followed_the_edges_itself_does_not_repeat_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _connected()
    events = await _Lane(monkeypatch, reader, calls=[GetConnections.__name__]).events()

    assert not any(isinstance(event, ConnectionsEvent) for event in events)
    assert reader.calls == []


async def test_a_turn_that_failed_after_speaking_gets_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _connected()
    events = await _Lane(monkeypatch, reader, failed=True).events()

    assert not any(isinstance(event, ConnectionsEvent) for event in events)
    assert isinstance(events[-1], StreamEnd)


async def test_a_broken_read_still_ends_the_answer_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = await _Lane(monkeypatch, _Reader(error=RuntimeError("gone"))).events()

    assert _shape(events) == [Delta, StreamEnd]


async def test_the_whole_answer_path_carries_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a surface that sends whole messages actually calls."""
    lane = _Lane(monkeypatch, _connected())

    answer = await lane.service.agent(_USER, "q", "session")

    assert answer.text == "Here is your answer."
    assert [memory.item.title for memory in answer.connections] == ["neighbour"]


def test_they_become_a_block_after_the_prose() -> None:
    memory = ConnectedMemory(item=_item("neighbour"), phrase="expands", about="anchor")

    reply = _to_reply(RecallAnswer(text="The answer.", connections=(memory,)))

    assert reply.blocks == [
        TextBlock(text="The answer."),
        ConnectionsBlock(memories=(memory,)),
    ]


def test_a_block_survives_being_delivered_as_events() -> None:
    """A lane that cannot stream goes through here, and an unhandled block type used to
    fall into the branch that reads `.kind`."""
    memory = ConnectedMemory(item=_item("neighbour"), phrase="expands", about="anchor")

    events = _as_events(OutboundReply([ConnectionsBlock(memories=(memory,))]))

    assert events == [ConnectionsEvent(memories=(memory,)), StreamEnd()]
