"""Connection request/response DTOs.

Same two shapes of care as `app/schemas/space.py`.

**Nothing is a passthrough of the model.** A write model lists exactly the fields a
caller may set, so `user_id`, `origin`, `status`, `score`, `ai_reason` and the two
generated pair columns are not reachable by adding them to a JSON body. There is nothing
here to overpost with: who owns an edge comes from the session, and how it was created
comes from which code path created it.

**`pair_low` / `pair_high` never leave the database.** They are an index, not data.

`direction` is on the response and not on the row because it is computed relative to the
memory that was asked about -- the same stored edge is outgoing from one of its memories
and incoming from the other, and both readings are true.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.base import ConnectionOrigin, ConnectionStatus, Relation
from app.models.connection import NOTE_MAX
from app.schemas.vault import VaultItemRead


class CreateConnectionRequest(BaseModel):
    """Connect two of the caller's own memories.

    Both ids are in the body rather than one in the path so the self-edge check is a
    schema validator: it answers 422 before any statement runs. The database carries the
    same rule as a CHECK constraint, which is defence in depth rather than the thing
    anyone sees.
    """

    source_id: uuid.UUID
    target_id: uuid.UUID
    #: Defaults to the weakest claim, which is also what a derived edge gets. A person
    #: who has not said how two memories relate has said they relate.
    relation: Relation = Relation.related_to
    note: str | None = Field(default=None, max_length=NOTE_MAX)

    @model_validator(mode="after")
    def _no_self_edge(self) -> CreateConnectionRequest:
        if self.source_id == self.target_id:
            raise ValueError("A memory cannot be connected to itself.")
        return self


class UpdateConnectionRequest(BaseModel):
    """Re-label or re-caption an edge.

    `None` means "leave it alone"; the empty string is how a note is cleared. A nullable
    field with two meanings for null is a field nobody can use correctly -- the same rule
    `UpdateSpaceRequest` follows for `icon` and `accent`.
    """

    relation: Relation | None = None
    note: str | None = Field(default=None, max_length=NOTE_MAX)


class ConnectionRead(BaseModel):
    """One edge, as seen from the memory that was asked about."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    relation: Relation
    #: Relative to the memory this was read from. See the module docstring.
    direction: Literal["outgoing", "incoming"]
    origin: ConnectionOrigin
    status: ConnectionStatus
    #: The similarity a derived edge was drawn at, 0..1. Null for a hand-made one -- the
    #: UI shows nothing rather than a zero, because "not measured" and "not similar" are
    #: different claims.
    score: float | None = None
    #: The person's own words.
    note: str | None = None
    #: A model's. Rendered marked as machine-written, never styled like `note`.
    ai_reason: str | None = None
    created_at: datetime

    #: The other end, as a card. `VaultItemRead` and never `VaultItemDetail`: a page of
    #: neighbours carrying article bodies is kilobytes nothing renders.
    memory: VaultItemRead


class NeighbourhoodResponse(BaseModel):
    """Everything connected to one memory."""

    focus: VaultItemRead
    connections: list[ConnectionRead] = Field(default_factory=list)
    total: int = 0


class SuggestionRead(BaseModel):
    """An edge nobody has accepted yet, with both of its memories.

    Both ends, unlike `ConnectionRead`: a suggestion is read from an inbox rather than
    from one memory's page, so there is no "the memory you asked about" to be the other
    end of.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    relation: Relation
    score: float | None = None
    ai_reason: str | None = None
    created_at: datetime
    source: VaultItemRead
    target: VaultItemRead


class SuggestionListResponse(BaseModel):
    suggestions: list[SuggestionRead] = Field(default_factory=list)
    total: int = 0


class HubRead(BaseModel):
    """One memory and how many things connect to it."""

    memory: VaultItemRead
    connection_count: int


class HubListResponse(BaseModel):
    """The busiest memories, busiest first.

    What `/connections` opens on when no memory is named. An empty list is a real answer
    -- a vault with no edges yet -- and the page falls back to offering recent memories
    rather than inventing a focus.
    """

    hubs: list[HubRead] = Field(default_factory=list)


class CreateConnectionResponse(BaseModel):
    """`created` is false when the pair already had an edge, in either order.

    That is a 200 and not a 409: re-adding is a normal outcome, not an error. Somebody
    connects A to B from A's page and, a week later, from B's -- the obvious action must
    not look like a broken server.
    """

    connection: ConnectionRead
    created: bool
