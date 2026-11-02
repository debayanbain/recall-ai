"""What to offer beside an answer: the memories connected to the ones it used.

`GetConnections` already lets the model follow an edge, but only when it decides to, and a
person asking about one reel has no way to know that three others are linked to it. So
this runs after the answer, in code, on every turn that was about specific memories.

**No model is involved at any point, and that is what makes it safe to do every time.**
Neighbours are read from the database and handed straight to a surface. They never enter a
prompt and never enter chat history, so a scraped title reaching this block cannot steer
this turn or the next one. Reading edges into the prompt instead would cost tokens per turn
and would be the "relevance attack" route the injection note in CLAUDE.md describes.

Three rules decide what appears:

* **Confirmed edges only.** `ConnectionRepository.neighbours_of_many` has no way to ask
  for anything else. A suggestion is a guess nobody approved, and offering one here would
  skip the tap that is the security control on derived edges.
* **Anchored on what the answer used, not on what the model saw.** Cited ids first, because
  that is the answer saying what it is about; failing that, what a search found. Never the
  snapshot: those rows are in every prompt, so anchoring on them would attach the same
  unrelated suggestions to every reply, including a greeting.
* **Failure costs the suggestions, never the answer.** By the time this runs the person
  has already been answered.

Pure of any surface: it returns rows, and `ConnectionsBlock` says what they are.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from app.core.logging import get_logger
from app.models.vault import VaultItem
from app.services.chat_engine.cards import short_id
from app.services.chat_engine.toolbox import MemoryToolbox, relation_words
from app.services.chat_engine.types import ConnectedMemory

log = get_logger("recall.agent")

#: How many of the answer's memories to look around. An answer drawing on more than this is
#: a list, where a suggestion per row would be longer than the list.
_MAX_ANCHORS = 3

#: Rows to read per suggestion wanted. One neighbour reached from two anchors is two rows
#: and one suggestion, so reading exactly `limit` could come back short.
_OVERFETCH = 3

#: Marks an id `validation.scrub` took out of the text because it was real. Citations never
#: reach a reader, so this is the only record left of what the prose cited.
_CITED_PREFIX = "id:"

_OPPOSITE = {"outgoing": "incoming", "incoming": "outgoing"}


def cited_ids(declared: Iterable[str], removed: Iterable[str]) -> list[str]:
    """Every id the answer named: in `FinalAnswer.cited_ids`, then in its own prose."""
    named = [value.strip("[] ") for value in declared]
    named += [
        entry.removeprefix(_CITED_PREFIX)
        for entry in removed
        if entry.startswith(_CITED_PREFIX)
    ]
    return named


async def connected_to_answer(
    toolbox: MemoryToolbox, *, cited: Sequence[str], limit: int
) -> tuple[ConnectedMemory, ...]:
    """Up to `limit` memories connected to what this turn's answer used. Never raises."""
    reader = getattr(toolbox.connections, "neighbours_of_many", None)
    if limit <= 0 or reader is None:
        return ()

    # Checked against the surfaced set rather than trusted: a model that invented a citation
    # would otherwise choose what gets looked up.
    named = _unique(item for item in map(toolbox.surfaced.get, cited) if item is not None)
    # Through `surfaced` as well, so a row the card budget cut from a result, which the
    # model never saw, cannot anchor anything.
    found = (toolbox.surfaced.get(short_id(item)) for item in toolbox.matched)
    anchors = (named or _unique(item for item in found if item is not None))[:_MAX_ANCHORS]
    if not anchors:
        return ()

    titles = {item.id: _title(item) for item in anchors}
    try:
        rows = await reader(
            toolbox.user_id,
            [item.id for item in anchors],
            exclude=[item.id for item in named],
            limit=limit * _OVERFETCH,
        )
    except Exception as exc:  # noqa: BLE001 - the answer has already been given
        log.warning("agent_connection_suggestions_failed", error=type(exc).__name__)
        return ()

    memories = _suggestions(rows, titles, limit)
    if memories:
        log.info("agent_connections_suggested", count=len(memories), anchors=len(anchors))
    return memories


def _suggestions(
    rows: Iterable[tuple[uuid.UUID, Any]], titles: dict[uuid.UUID, str], limit: int
) -> tuple[ConnectedMemory, ...]:
    """Rows as suggestions: each neighbour once, in the order they were read."""
    memories: list[ConnectedMemory] = []
    seen: set[uuid.UUID] = set()
    for asked_about, neighbour in rows:
        if neighbour.item.id in seen:
            continue
        seen.add(neighbour.item.id)
        memories.append(
            ConnectedMemory(
                item=neighbour.item,
                # `direction` is relative to the memory asked about, so the stored label
                # reads with *that* one as subject. The neighbour is the subject of this
                # sentence, so the other reading is the true one: a chapter `part_of` a
                # book, asked about from the book, is "is part of", not "has a part in".
                phrase=relation_words(
                    str(neighbour.connection.relation), _OPPOSITE[neighbour.direction]
                ),
                about=titles.get(asked_about, "this memory"),
            )
        )
        if len(memories) == limit:
            break
    return tuple(memories)


def _unique(items: Iterable[VaultItem]) -> list[VaultItem]:
    """Each row once, at its first position. Order is relevance, so it has to survive."""
    unique: dict[uuid.UUID, VaultItem] = {}
    for item in items:
        unique.setdefault(item.id, item)
    return list(unique.values())


def _title(item: VaultItem) -> str:
    return item.title or item.source_url or "this memory"
