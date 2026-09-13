"""Connection data access. One statement per read, and the tenant predicate in every one.

Three shapes here carry weight beyond what the method names say.

**A neighbourhood is a `UNION ALL` of two halves, not an `OR`.** An edge is stored once,
directionally, so "everything connected to this memory" is its outgoing edges plus its
incoming ones. Written as `WHERE source_item_id = :id OR target_item_id = :id` that is
one predicate over two indexes and the planner is free to decline both; written as a
union it is two index scans that cannot be declined. Same species of care as
`VaultRepository.search_semantic` refusing `DISTINCT ON` -- the query shape is the thing
being chosen, not just the result.

**The listing and its total are one statement.** `count(*) OVER ()`, as `_page` does, for
the reason written out there: against a database in another region a separate
`SELECT count(*)` is a second ~290ms round trip to learn a number the scan already knew.

**`deleted_at` is filtered on the neighbour, always.** `VaultRepository.delete` is a soft
delete that scrubs the row rather than removing it, so an unfiltered join renders a
tombstone as `Untitled` beside a live memory -- the deleted thing still visibly related
to something. `delete` also removes an item's edges outright, so this is defence in depth
rather than the only thing standing between the two.

Writes go through `pg_insert(...)` for the ON CONFLICT clause, not because of the
generated columns -- SQLAlchemy omits those from an INSERT by itself. And every statement
that RETURNs a row carries `populate_existing`; see `_FRESH` for the bug that is.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, literal, literal_column
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased, load_only
from sqlmodel import col, func, or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.base import ConnectionOrigin, ConnectionStatus, Relation
from app.models.connection import MemoryConnection
from app.models.space import SpaceItem
from app.models.vault import VaultItem
from app.repositories.vault import VaultRepository

#: What a neighbourhood returns when the caller names no ceiling. Matches what a page can
#: show without the radial layout becoming a scribble.
DEFAULT_NEIGHBOUR_LIMIT = 24

#: Postgres' own answer to "did that INSERT ... ON CONFLICT insert, or update?". A freshly
#: inserted tuple has no deleting-transaction id, so `xmax` is 0; one that replaced an
#: existing row carries the id of the transaction that superseded it. Reading it in the
#: same RETURNING saves the SELECT that asking any other way would cost -- and the answer
#: matters, because the API reports `created` and the UI words itself from it.
#: Every RETURNING statement here carries this, and it is not optional.
#:
#: SQLAlchemy's identity map hands back the object it already has for a primary key
#: rather than rebuilding it from the row, so an `UPDATE ... RETURNING` against a row the
#: session has already loaded returns the **stale** instance: the database is correct and
#: the response is not. It was caught by the dismiss-then-reconnect test, where the row
#: came back saying `dismissed` immediately after being revived to `confirmed`.
#:
#: That is the worst shape a bug can have here -- the write succeeds, so nothing errors,
#: and only the reply is wrong.
_FRESH = {"populate_existing": True}

_WAS_INSERTED: Any = literal_column("(xmax = 0)").label("created")


def _endpoints() -> Any:
    """Every edge, once per end: `(user_id, status, item_id, other_id)`.

    An edge is stored one way round and read from both, so anything that counts *per
    memory* has to see it twice -- once as A's neighbour B, once as B's neighbour A. A
    `UNION ALL` of the two projections is how, and writing it once means the two callers
    that count cannot drift into disagreeing about what a connection count is.
    """
    outgoing: Any = select(
        col(MemoryConnection.user_id).label("user_id"),
        col(MemoryConnection.status).label("status"),
        col(MemoryConnection.source_item_id).label("item_id"),
        col(MemoryConnection.target_item_id).label("other_id"),
    )
    incoming: Any = select(
        col(MemoryConnection.user_id).label("user_id"),
        col(MemoryConnection.status).label("status"),
        col(MemoryConnection.target_item_id).label("item_id"),
        col(MemoryConnection.source_item_id).label("other_id"),
    )
    return outgoing.union_all(incoming).subquery()


_ENDPOINTS = _endpoints()


@dataclass(frozen=True, slots=True)
class Neighbour:
    """One edge, read from one end of it.

    `direction` is computed **relative to the memory that was asked about**, which is why
    it lives here and not on the row: the same stored edge is "outgoing" from one of its
    two memories and "incoming" from the other, and both readings are correct.
    """

    connection: MemoryConnection
    item: VaultItem
    direction: str


class ConnectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- reads ---------------------------------------------------------------

    async def list_for_item(
        self,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        *,
        statuses: Sequence[ConnectionStatus] | None = None,
        relation: Relation | None = None,
        limit: int = DEFAULT_NEIGHBOUR_LIMIT,
    ) -> tuple[list[Neighbour], int]:
        """Everything connected to one memory, from both ends, newest first. ONE statement."""
        wanted = list(statuses) if statuses else [ConnectionStatus.confirmed]

        def half(mine: Any, theirs: Any, direction: str) -> Any:
            query = select(
                MemoryConnection,
                literal(direction).label("direction"),
                theirs.label("neighbour_id"),
            ).where(
                MemoryConnection.user_id == user_id,
                mine == item_id,
                col(MemoryConnection.status).in_([s.value for s in wanted]),
            )
            if relation is not None:
                query = query.where(MemoryConnection.relation == relation.value)
            return query

        union = half(
            col(MemoryConnection.source_item_id),
            col(MemoryConnection.target_item_id),
            "outgoing",
        ).union_all(
            half(
                col(MemoryConnection.target_item_id),
                col(MemoryConnection.source_item_id),
                "incoming",
            )
        )
        edges = union.subquery()
        edge = aliased(MemoryConnection, edges)

        total_col = func.count().over().label("total")
        query = (
            select(edge, edges.c.direction, VaultItem, total_col)
            .join(VaultItem, col(VaultItem.id) == edges.c.neighbour_id)
            .where(
                # The tenant predicate on the *neighbour* as well as on the edge.
                # Redundant while the service refuses to write an edge whose ends are not
                # both the caller's -- and stated anyway, exactly as `search_semantic`
                # states it on both its tables: these rows reach a prompt, and this is the
                # last query before they do, so it states the constraint rather than
                # inheriting it from a caller that could change.
                VaultItem.user_id == user_id,
                col(VaultItem.deleted_at).is_(None),
            )
            .order_by(edges.c.created_at.desc())
            .limit(limit)
            # A neighbour is rendered as a card, so it is read as one. Without this the
            # join drags `content`, `item_metadata` and `ai_highlights` for every edge --
            # an article body is kilobytes and two dozen of them is the whole response.
            # `raiseload` is what makes a field added to the response but not to
            # `_CARD_COLUMNS` fail loudly instead of emitting a silent per-row query.
            .options(load_only(*_card_columns(VaultItem), raiseload=True))
        )
        # `execute`, not `exec`: SQLModel's `exec()` narrows a select back to one entity
        # and drops the extra columns, so `direction` and the window total would vanish.
        rows = (await self.session.execute(query)).all()
        if not rows:
            return [], 0
        found = [
            Neighbour(
                connection=cast("MemoryConnection", row[0]),
                item=cast("VaultItem", row[2]),
                direction=str(row[1]),
            )
            for row in rows
        ]
        return found, int(rows[0][3])

    async def list_suggestions(
        self, user_id: uuid.UUID, *, limit: int = DEFAULT_NEIGHBOUR_LIMIT
    ) -> tuple[list[tuple[MemoryConnection, VaultItem, VaultItem]], int]:
        """Undecided edges, strongest first, with both of their memories. ONE statement.

        Per user rather than per item on purpose: an edge discovered from a new capture's
        side is a suggestion on *both* memories' pages, so offering it per item offers the
        same decision twice and lets one be accepted while the other still asks.
        """
        source = aliased(VaultItem)
        target = aliased(VaultItem)
        total_col = func.count().over().label("total")
        query = (
            select(MemoryConnection, source, target, total_col)
            .join(source, col(source.id) == col(MemoryConnection.source_item_id))
            .join(target, col(target.id) == col(MemoryConnection.target_item_id))
            .where(
                MemoryConnection.user_id == user_id,
                MemoryConnection.status == ConnectionStatus.suggested.value,
                # Deliberate duplication -- see `list_for_item`.
                col(source.user_id) == user_id,
                col(target.user_id) == user_id,
                col(source.deleted_at).is_(None),
                col(target.deleted_at).is_(None),
            )
            .order_by(col(MemoryConnection.score).desc().nullslast())
            .limit(limit)
            # Two cards per suggestion, so two narrowings. Full rows here would be the
            # same waste as above, doubled.
            .options(
                load_only(*_card_columns(source), raiseload=True),
                load_only(*_card_columns(target), raiseload=True),
            )
        )
        rows = (await self.session.execute(query)).all()
        if not rows:
            return [], 0
        found = [
            (
                cast("MemoryConnection", row[0]),
                cast("VaultItem", row[1]),
                cast("VaultItem", row[2]),
            )
            for row in rows
        ]
        return found, int(rows[0][3])

    async def most_connected(
        self, user_id: uuid.UUID, *, limit: int = DEFAULT_NEIGHBOUR_LIMIT
    ) -> list[tuple[VaultItem, int]]:
        """The memories with the most edges, busiest first. ONE statement.

        What `/connections` needs to be a destination rather than a chore. Reached from
        the nav with no memory named, the page can only offer a list to choose from, and a
        list of the twenty *newest* memories says nothing about which of them is worth
        opening -- the newest capture is usually the one with the fewest connections,
        because everything it connects to was found from its side and nothing has been
        saved since.

        Counts confirmed edges only, and only where the neighbour is still alive: a hub
        whose count includes deleted memories promises a page that will not deliver them.

        The count subquery is ordered and limited *before* the join, so the row fetch is
        over at most `limit` items rather than over every memory that has an edge.
        """
        counts = (
            select(_ENDPOINTS.c.item_id, func.count().label("n"))
            .join(VaultItem, col(VaultItem.id) == _ENDPOINTS.c.other_id)
            .where(
                _ENDPOINTS.c.user_id == user_id,
                _ENDPOINTS.c.status == ConnectionStatus.confirmed.value,
                # Deliberate duplication -- see `list_for_item`.
                VaultItem.user_id == user_id,
                col(VaultItem.deleted_at).is_(None),
            )
            .group_by(_ENDPOINTS.c.item_id)
            .order_by(func.count().desc())
            .limit(limit)
            .subquery()
        )
        query = (
            select(VaultItem, counts.c.n)
            .join(counts, col(VaultItem.id) == counts.c.item_id)
            .where(
                VaultItem.user_id == user_id,
                col(VaultItem.deleted_at).is_(None),
            )
            .order_by(counts.c.n.desc())
            .options(load_only(*_card_columns(VaultItem), raiseload=True))
        )
        rows = (await self.session.execute(query)).all()
        return [(cast("VaultItem", row[0]), int(row[1])) for row in rows]

    async def list_in_space(
        self,
        user_id: uuid.UUID,
        space_id: uuid.UUID,
        *,
        limit: int = DEFAULT_NEIGHBOUR_LIMIT,
    ) -> tuple[list[tuple[MemoryConnection, VaultItem, VaultItem]], int]:
        """The caller's own edges whose **both** ends are in this Space. ONE statement.

        Three predicates, and the middle one is the sharing boundary rather than a filter:

        * `user_id` -- **only the caller's own edges**. A Space is the one place somebody
          reads another person's rows, and this deliberately does not widen that: a
          connection is a claim *you* made about *your* memories, and "A contradicts B" is
          a judgement, not a fact about the Space. Two members looking at the same Space
          see different graphs, which is honest. Widening this later is easy; narrowing it
          after people have seen each other's judgements is not.
        * both ends in the Space -- an edge to a memory outside it would render a card the
          member may not be entitled to see at all.
        * both ends alive -- a scrubbed tombstone must never appear beside a live memory.

        Joined against `space_items` twice rather than fetching the Space's item ids first
        and passing them in: that would be a second round trip and an `IN` list that grows
        with the Space.
        """
        source_member = aliased(SpaceItem)
        target_member = aliased(SpaceItem)
        source = aliased(VaultItem)
        target = aliased(VaultItem)
        total_col = func.count().over().label("total")
        query = (
            select(MemoryConnection, source, target, total_col)
            .join(
                source_member,
                col(source_member.vault_item_id) == col(MemoryConnection.source_item_id),
            )
            .join(
                target_member,
                col(target_member.vault_item_id) == col(MemoryConnection.target_item_id),
            )
            .join(source, col(source.id) == col(MemoryConnection.source_item_id))
            .join(target, col(target.id) == col(MemoryConnection.target_item_id))
            .where(
                MemoryConnection.user_id == user_id,
                MemoryConnection.status == ConnectionStatus.confirmed.value,
                source_member.space_id == space_id,
                target_member.space_id == space_id,
                # Both ends, the caller's. See `list_for_item` -- and it matters more
                # here than anywhere: a Space is the one place a person reads rows they
                # do not own, so a missing predicate would not merely be redundant.
                col(source.user_id) == user_id,
                col(target.user_id) == user_id,
                col(source.deleted_at).is_(None),
                col(target.deleted_at).is_(None),
            )
            .order_by(col(MemoryConnection.created_at).desc())
            .limit(limit)
            .options(
                load_only(*_card_columns(source), raiseload=True),
                load_only(*_card_columns(target), raiseload=True),
            )
        )
        rows = (await self.session.execute(query)).all()
        if not rows:
            return [], 0
        found = [
            (
                cast("MemoryConnection", row[0]),
                cast("VaultItem", row[1]),
                cast("VaultItem", row[2]),
            )
            for row in rows
        ]
        return found, int(rows[0][3])

    async def get_scoped(
        self, connection_id: uuid.UUID, user_id: uuid.UUID
    ) -> MemoryConnection | None:
        """One edge, or `None` for missing *and* for not-yours -- indistinguishable by design."""
        result = await self.session.exec(
            select(MemoryConnection).where(
                MemoryConnection.id == connection_id,
                MemoryConnection.user_id == user_id,
            )
        )
        return result.first()

    async def counts_for_items(
        self, user_id: uuid.UUID, item_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """How many confirmed edges each of these memories has. ONE grouped statement.

        Deliberately not a column on `vault_items`. A cached count is wrong the moment
        anything is dismissed or deleted, and the one caller that matters -- the chat
        agent -- reads this number and then *follows it inside the same turn*, so a stale
        one makes the model contradict itself in a single reply.
        """
        if not item_ids:
            return {}
        wanted = list(dict.fromkeys(item_ids))
        counts: dict[uuid.UUID, int] = {}
        rows = await self.session.execute(
            select(_ENDPOINTS.c.item_id, func.count())
            # The neighbour has to still exist, or the count promises a card the
            # neighbourhood read will not return.
            .join(VaultItem, col(VaultItem.id) == _ENDPOINTS.c.other_id)
            .where(
                _ENDPOINTS.c.user_id == user_id,
                _ENDPOINTS.c.status == ConnectionStatus.confirmed.value,
                _ENDPOINTS.c.item_id.in_(wanted),
                # Deliberate duplication -- see `list_for_item`.
                VaultItem.user_id == user_id,
                col(VaultItem.deleted_at).is_(None),
            )
            .group_by(_ENDPOINTS.c.item_id)
        )
        for item_id, count in rows.all():
            counts[item_id] = int(count)
        return counts

    # ---- writes --------------------------------------------------------------

    async def upsert_manual(
        self,
        user_id: uuid.UUID,
        source_item_id: uuid.UUID,
        target_item_id: uuid.UUID,
        *,
        relation: Relation,
        note: str | None,
    ) -> tuple[MemoryConnection, bool]:
        """Draw an edge a person asked for. Returns the row and whether it was new.

        `ON CONFLICT DO UPDATE` rather than `DO NOTHING`, and that is the whole design of
        the dismissal rule: a pair somebody declined keeps its row forever so the
        derivation cannot re-propose it, which would otherwise leave them no way to ever
        connect those two by hand. A deliberate manual connect **revives** the row --
        a person overriding their own earlier "don't suggest this" is exactly what this
        action is. Re-connecting an already-confirmed pair just re-labels it, which is
        why this answers 200 rather than 409: re-adding is a normal outcome, not an error
        (the same lesson `SpaceRepository.add_items` learned).

        `xmax = 0` is Postgres' own answer to "was this inserted or updated": on a fresh
        tuple the deleting-transaction id is zero, on one that replaced an existing row it
        is not. It saves the SELECT that asking any other way would cost.
        """
        values = {
            "user_id": user_id,
            "source_item_id": source_item_id,
            "target_item_id": target_item_id,
            "relation": relation.value,
            "origin": ConnectionOrigin.user.value,
            "status": ConnectionStatus.confirmed.value,
            "note": note,
            "confirmed_at": datetime.now(UTC),
        }
        revived = {
            "relation": relation.value,
            "origin": ConnectionOrigin.user.value,
            "status": ConnectionStatus.confirmed.value,
            "note": note,
            # A model's guess about a pair a person has now labelled themselves is stale
            # the moment they do it, and it renders differently from a note.
            "ai_reason": None,
            # And so is the similarity it was derived at. Once a person draws this edge it
            # is hand-made, and `score` means "the number this was measured at" -- keeping
            # the old one would report a measurement nobody took for an edge nobody
            # derived. The model docstring says NULL for a hand-made edge; this is what
            # makes that true after a dismissal is revived.
            "score": None,
            "dismissed_at": None,
            "confirmed_at": values["confirmed_at"],
        }
        statement = (
            pg_insert(MemoryConnection)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_memory_connections_pair", set_=revived
            )
            .returning(MemoryConnection, _WAS_INSERTED)
            .execution_options(**_FRESH)
        )
        row = (await self.session.execute(statement)).one()
        return cast("MemoryConnection", row[0]), bool(row[1])

    async def suggest_many(
        self,
        user_id: uuid.UUID,
        source_item_id: uuid.UUID,
        candidates: Sequence[tuple[uuid.UUID, float]],
    ) -> int:
        """Propose edges from one new memory. Returns how many were actually new.

        `ON CONFLICT DO NOTHING`, which does three jobs at once and is why the dismissal
        lives on this table rather than its own:

        * a pair already connected is left alone rather than re-proposed,
        * a pair somebody **dismissed** is left alone -- the declined row *is* the
          conflicting row, so "never suggest this" needs no second lookup,
        * re-running the derivation (a `reprocess`, or two captures racing) is a no-op
          rather than a `UniqueViolation`, which for this caller is the *normal* outcome
          and not the edge one.

        Suggestions only. Nothing here writes a `confirmed` edge: a derived connection is
        a proposal until a person taps it, which is both what Feature 20 asks for and the
        thing standing between a scraped page and the agent's context.
        """
        if not candidates:
            return 0
        rows = [
            {
                "user_id": user_id,
                "source_item_id": source_item_id,
                "target_item_id": target_id,
                "relation": Relation.related_to.value,
                "origin": ConnectionOrigin.ai.value,
                "status": ConnectionStatus.suggested.value,
                "score": score,
            }
            for target_id, score in candidates
            if target_id != source_item_id
        ]
        if not rows:
            return 0
        result = await self.session.execute(
            pg_insert(MemoryConnection)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["user_id", "pair_low", "pair_high"]
            )
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def count_for_item(self, user_id: uuid.UUID, item_id: uuid.UUID) -> int:
        """How many edges this memory already holds, in any state. ONE statement.

        Counts dismissed and suggested rows too, on purpose: the ceiling it feeds exists
        to bound how much of one memory's page anything can occupy, and a wall of pending
        suggestions is exactly as much of a wall as a wall of confirmed ones.
        """
        result = await self.session.exec(
            select(func.count()).where(
                MemoryConnection.user_id == user_id,
                or_(
                    col(MemoryConnection.source_item_id) == item_id,
                    col(MemoryConnection.target_item_id) == item_id,
                ),
            )
        )
        return int(result.one())

    async def update_scoped(
        self,
        connection_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        relation: Relation | None,
        note: str | None,
        clear_note: bool,
    ) -> MemoryConnection | None:
        """Re-label or re-caption an edge. `None` when it is not the caller's to touch."""
        changes: dict[str, Any] = {}
        if relation is not None:
            changes["relation"] = relation.value
        if clear_note:
            changes["note"] = None
        elif note is not None:
            changes["note"] = note
        if not changes:
            return await self.get_scoped(connection_id, user_id)
        statement = (
            sa_update(MemoryConnection)
            .where(
                col(MemoryConnection.id) == connection_id,
                col(MemoryConnection.user_id) == user_id,
            )
            .values(**changes)
            .returning(MemoryConnection)
            .execution_options(**_FRESH)
        )
        row = (await self.session.execute(statement)).first()
        return cast("MemoryConnection", row[0]) if row else None

    async def delete_scoped(self, connection_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Remove an edge. The tenant predicate in the DELETE *is* the ownership check.

        Never SELECT-then-delete: that is two round trips and a window in which the row
        can change owner in theory and change at all in practice.
        """
        result = await self.session.execute(
            sa_delete(MemoryConnection).where(
                col(MemoryConnection.id) == connection_id,
                col(MemoryConnection.user_id) == user_id,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def dismiss(self, connection_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Decline a suggestion, keeping the row so it is never proposed again."""
        result = await self.session.execute(
            sa_update(MemoryConnection)
            .where(
                col(MemoryConnection.id) == connection_id,
                col(MemoryConnection.user_id) == user_id,
                col(MemoryConnection.status) != ConnectionStatus.dismissed.value,
            )
            .values(
                status=ConnectionStatus.dismissed.value,
                dismissed_at=datetime.now(UTC),
                confirmed_at=None,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def confirm(
        self, connection_id: uuid.UUID, user_id: uuid.UUID
    ) -> MemoryConnection | None:
        """Accept a suggestion. The one place an `ai` edge becomes something the agent reads."""
        statement = (
            sa_update(MemoryConnection)
            .where(
                col(MemoryConnection.id) == connection_id,
                col(MemoryConnection.user_id) == user_id,
                col(MemoryConnection.status) == ConnectionStatus.suggested.value,
            )
            .values(
                status=ConnectionStatus.confirmed.value,
                confirmed_at=datetime.now(UTC),
                dismissed_at=None,
            )
            .returning(MemoryConnection)
            .execution_options(**_FRESH)
        )
        row = (await self.session.execute(statement)).first()
        return cast("MemoryConnection", row[0]) if row else None

    async def delete_for_item(self, item_id: uuid.UUID) -> int:
        """Every edge touching one memory, gone. Called when that memory is deleted.

        Hard, not soft, and for the reason the chunk delete beside it is hard: an edge is
        derived data asserting "this memory is about the same thing as that one", which is
        a statement about content somebody asked to be rid of. It also frees the pair, so
        a tombstone cannot block a connection someone draws later.
        """
        result = await self.session.execute(
            sa_delete(MemoryConnection).where(
                or_(
                    col(MemoryConnection.source_item_id) == item_id,
                    col(MemoryConnection.target_item_id) == item_id,
                )
            )
        )
        return int(cast("CursorResult[Any]", result).rowcount or 0)


def _card_columns(entity: Any) -> tuple[Any, ...]:
    """The card columns of `VaultItem`, or of an alias of it.

    Reads `VaultRepository._CARD_COLUMNS` rather than keeping a second list: the set a
    card needs is one fact, and two copies of it means the copy nobody updated is the one
    that raises under `raiseload` when a field is added to `VaultItemRead`.
    """
    return tuple(getattr(entity, name) for name in VaultRepository._CARD_COLUMNS)
