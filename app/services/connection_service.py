"""Connections: the use-cases, and the one ownership rule that holds the whole feature up.

**You may only connect memories you own, and that is checked per item.** One statement
reads both endpoints scoped to the caller and requires *two* rows back; anything less is
`ConnectionNotFound`. This is `SpaceService._attach`'s rule and it exists because that
check was once missing there -- a real cross-tenant IDOR. A request carrying two ids is
exactly where a stranger's id is easiest to slip in, and checking "the caller owns
something" instead of "the caller owns each of these" is how it gets through.

**There is no 403 in this feature.** A connection has one owner, so "not yours" and "no
such thing" are the same answer and must look the same from outside -- otherwise the API
confirms an id exists to somebody who may not see it. Spaces need both codes because a
member can be shown a Space and still be refused an action inside it; nothing here has
that shape.

**Symmetric relations are normalised on write.** `related_to` and `contradicts` read the
same from both ends, so the stored direction carries no meaning; leaving it to chance
makes half of them render backwards and invites someone to "fix" a row that was never
wrong. Everything else keeps the direction it was drawn in.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.ai import connections as connections_ai
from app.core import rate_limit
from app.core.config import settings
from app.core.logging import get_logger
from app.models.base import SYMMETRIC_RELATIONS, ConnectionStatus, Relation
from app.models.connection import MemoryConnection
from app.models.vault import VaultItem
from app.repositories.connection import ConnectionRepository, Neighbour
from app.repositories.vault import VaultRepository
from app.services.chat_engine.cards import build_card

log = get_logger("recall.connections")

#: What a neighbourhood returns when the caller names no ceiling, and the ceiling it is
#: clamped to. Bounds the page, the radial layout, and -- see the injection note in
#: `CLAUDE.md` -- how much of one memory's page a single scraped page can occupy.
DEFAULT_LIMIT = 24
MAX_LIMIT = 100


class ConnectionNotFound(LookupError):
    """No such connection, or no such memory, or neither is the caller's. One answer."""


class TypingUnavailable(RuntimeError):
    """Relation typing is switched off, unconfigured, or the caller has used up its cap.

    One exception for three causes, answered as 503. They are genuinely the same thing
    from outside -- "ask again later or not at all" -- and separating them would tell a
    caller which of an operator's settings is off.
    """


class ConnectionService:
    def __init__(self, repo: ConnectionRepository, vault_repo: VaultRepository) -> None:
        self.repo = repo
        self.vault_repo = vault_repo

    # ---- reads ---------------------------------------------------------------

    async def neighbourhood(
        self,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        statuses: Sequence[ConnectionStatus] | None = None,
        relation: Relation | None = None,
        limit: int | None = None,
    ) -> tuple[VaultItem, list[Neighbour], int]:
        """The focus memory and everything connected to it. Two statements.

        The first is the ownership gate -- `VaultRepository.get` returns `None` for
        missing, for not-yours and for soft-deleted alike -- and it is also what stops a
        tombstone being used as a focus. Inferring "not yours" from an empty edge list
        would save it and would stop being an ownership check.
        """
        focus = await self.vault_repo.get(item_id, user_id)
        if focus is None:
            raise ConnectionNotFound(str(item_id))
        found, total = await self.repo.list_for_item(
            user_id,
            item_id,
            statuses=statuses,
            relation=relation,
            limit=_limit(limit),
        )
        return focus, found, total

    async def hubs(
        self, user_id: uuid.UUID, *, limit: int | None = None
    ) -> list[tuple[VaultItem, int]]:
        """The caller's most-connected memories. One statement, no ownership gate needed.

        The scan is already scoped to `user_id` and nothing not theirs can appear in it,
        so unlike `neighbourhood` there is no id to check first: this answers a question
        about the caller's own vault rather than about a row they named.
        """
        return await self.repo.most_connected(user_id, limit=_limit(limit))

    async def suggestions(
        self, user_id: uuid.UUID, *, limit: int | None = None
    ) -> tuple[list[tuple[MemoryConnection, VaultItem, VaultItem]], int]:
        return await self.repo.list_suggestions(user_id, limit=_limit(limit))

    # ---- writes --------------------------------------------------------------

    async def connect(
        self,
        user_id: uuid.UUID,
        source_id: uuid.UUID,
        target_id: uuid.UUID,
        *,
        relation: Relation,
        note: str | None,
    ) -> tuple[MemoryConnection, bool, VaultItem]:
        """Draw an edge between two of the caller's memories.

        Idempotent in either direction: the unique key is the unordered pair, so
        connecting B to A when A to B already exists re-labels the one edge rather than
        raising. Re-adding is a normal outcome.

        Answers with the *far* end -- the memory the caller named as the target -- because
        that is what the response renders as a card, and the ownership check has already
        read it. Returning ids alone would buy a second round trip for a row in hand.
        """
        if source_id == target_id:
            # The schema already refuses this with a 422; the service refuses it too,
            # because a second caller (a script, a task) does not go through the schema.
            raise ConnectionNotFound(str(source_id))
        owned = await self._assert_owns_both(user_id, source_id, target_id)

        source, target = _orient(source_id, target_id, relation)
        connection, created = await self.repo.upsert_manual(
            user_id, source, target, relation=relation, note=_clean_note(note)
        )
        log.info(
            "connection_created" if created else "connection_updated",
            relation=relation.value,
        )
        return connection, created, owned[target_id]

    async def update(
        self,
        connection_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        relation: Relation | None,
        note: str | None,
    ) -> tuple[MemoryConnection, VaultItem]:
        """Re-label or re-caption. An empty note clears it; `None` leaves it alone.

        Changing a relation can change whether it is symmetric, so the stored direction is
        re-oriented here too -- otherwise a `part_of` edge retyped to `contradicts` keeps a
        direction that now means nothing and renders backwards from one of its two pages.
        """
        clear = note is not None and not note.strip()
        updated = await self.repo.update_scoped(
            connection_id,
            user_id,
            relation=relation,
            note=None if clear else _clean_note(note),
            clear_note=clear,
        )
        if updated is None:
            raise ConnectionNotFound(str(connection_id))
        if relation is not None and relation in SYMMETRIC_RELATIONS:
            await self._normalise(updated)
        return updated, await self._far_end(updated, user_id)

    async def confirm(
        self, connection_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[MemoryConnection, VaultItem]:
        """Accept a suggestion. The only way a derived edge becomes one the agent reads."""
        confirmed = await self.repo.confirm(connection_id, user_id)
        if confirmed is None:
            raise ConnectionNotFound(str(connection_id))
        log.info("connection_confirmed", relation=confirmed.relation)
        return confirmed, await self._far_end(confirmed, user_id)

    async def retype(
        self, connection_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[MemoryConnection, VaultItem]:
        """Ask a model how these two memories relate, and record its answer as its own.

        **On demand, on an edge that already exists.** Nothing calls this during capture:
        the derivation writes `related_to`, which is the strongest claim a cosine distance
        supports, and a person asks for more only when they want it. That ordering is the
        whole reason this phase is last -- it is the only part of the feature that can be
        confidently, fluently wrong, and a wrong label filed automatically on every
        capture is one nobody would go back and check.

        **A failure keeps the relation the edge already had.** `related_to` is honest, so
        degrading to it costs nothing; the model is an improvement on the default, never a
        prerequisite for it.

        Three statements: the scoped read, the two cards' rows, and the update.
        """
        if not connections_ai.typing_available():
            raise TypingUnavailable("Relation labelling is not available.")

        # Ownership before the quota. Charging first lets a loop over random ids burn the
        # caller's own hourly allowance without a single model call being made -- the
        # quota exists to bound spend, and nothing is spent on an edge that is not theirs.
        connection = await self.repo.get_scoped(connection_id, user_id)
        if connection is None:
            raise ConnectionNotFound(str(connection_id))

        if not await rate_limit.consume(
            "connection_typing", str(user_id), settings.CONNECTION_TYPING_PER_HOUR
        ):
            log.info("connection_typing_rate_limited")
            raise TypingUnavailable("You have reached this hour's limit.")

        ends = await self.vault_repo.owned_items(
            {connection.source_item_id, connection.target_item_id}, user_id
        )
        source = ends.get(connection.source_item_id)
        target = ends.get(connection.target_item_id)
        if source is None or target is None:
            # One end was deleted between the edge being written and this call. The same
            # answer as never having found the edge.
            raise ConnectionNotFound(str(connection_id))

        typed = await connections_ai.type_connection(
            build_card(source), build_card(target)
        )
        log.info(
            "connection_typed", relation=typed.relation.value, swapped=typed.swap
        )

        if typed.swap:
            # "B is part of A" arrives as part_of + b_to_a. Swapping the ends is free:
            # the unique key is the unordered pair, so the row keeps its identity.
            connection.source_item_id, connection.target_item_id = (
                connection.target_item_id,
                connection.source_item_id,
            )
        connection.relation = typed.relation.value
        # The model's own words, in the column that is rendered as machine-written. Never
        # `note`, which is where a person's words go -- showing a model's sentence as
        # something its owner typed is the one way this feature can lie.
        connection.ai_reason = typed.reason or None
        if typed.relation in SYMMETRIC_RELATIONS:
            await self._normalise(connection)
        self.repo.session.add(connection)
        await self.repo.session.flush()
        # Read **after** both swaps, never before. `_normalise` can reorder the ends
        # again, so a far end captured earlier is the row that is now the *source* -- and
        # the response would then show the focus memory as its own neighbour. `update()`
        # has always called `_far_end` last for this reason.
        return connection, await self._far_end(connection, user_id)

    async def dismiss(self, connection_id: uuid.UUID, user_id: uuid.UUID) -> None:
        """Decline a suggestion, and never be offered that pair again.

        The row is kept rather than deleted: it *is* the record that stops the derivation
        re-proposing the pair on the next capture. A deliberate manual connect later
        revives it -- a person overriding their own earlier no is what that action means.
        """
        if not await self.repo.dismiss(connection_id, user_id):
            raise ConnectionNotFound(str(connection_id))
        log.info("connection_dismissed")

    async def delete(self, connection_id: uuid.UUID, user_id: uuid.UUID) -> None:
        if not await self.repo.delete_scoped(connection_id, user_id):
            raise ConnectionNotFound(str(connection_id))
        log.info("connection_deleted")

    # ---- internals -----------------------------------------------------------

    async def _assert_owns_both(
        self, user_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
    ) -> dict[uuid.UUID, VaultItem]:
        """Both endpoints, scoped to the caller, in ONE statement. Two rows or nothing.

        Per item, never per request. See the module docstring.
        """
        owned = await self.vault_repo.owned_items({source_id, target_id}, user_id)
        if set(owned) != {source_id, target_id}:
            log.info("connection_refused_unowned")
            raise ConnectionNotFound(str(source_id))
        return owned

    async def _far_end(self, connection: MemoryConnection, user_id: uuid.UUID) -> VaultItem:
        """The memory at the other end of an edge, from the stored source's point of view.

        One scoped read, and it is scoped like every other: a write route answers with a
        card, and the row it renders must be re-checked rather than trusted because the
        edge referenced it.
        """
        other = await self.vault_repo.get(connection.target_item_id, user_id)
        if other is None:
            # The edge points at something this caller cannot see -- deleted between the
            # write and this read. The same answer as never having found the edge.
            raise ConnectionNotFound(str(connection.id))
        return other

    async def _normalise(self, connection: MemoryConnection) -> None:
        """Point a symmetric edge from the lower id, so both ends read one label."""
        if connection.source_item_id <= connection.target_item_id:
            return
        connection.source_item_id, connection.target_item_id = (
            connection.target_item_id,
            connection.source_item_id,
        )
        self.repo.session.add(connection)
        await self.repo.session.flush()


def _orient(
    source_id: uuid.UUID, target_id: uuid.UUID, relation: Relation
) -> tuple[uuid.UUID, uuid.UUID]:
    """The order an edge is stored in. Meaningful for a directional relation, fixed for
    a symmetric one -- see the module docstring."""
    if relation in SYMMETRIC_RELATIONS and source_id > target_id:
        return target_id, source_id
    return source_id, target_id


def _clean_note(note: str | None) -> str | None:
    """A person's own words, trimmed. Empty becomes absent rather than an empty string."""
    if note is None:
        return None
    cleaned = note.strip()
    return cleaned or None


def _limit(value: int | None) -> int:
    if not value or value < 1:
        return DEFAULT_LIMIT
    return min(int(value), MAX_LIMIT)
