"""Connections between a person's own memories.

Shaped like `app/api/v1/spaces.py`, with one deliberate difference: **there is no 403
here**. A connection has exactly one owner, so "no such connection" and "not yours" are
the same answer and must look identical from outside -- a 403 would confirm that an id
exists to somebody who may not see it. Spaces need both codes because a member can be
shown a Space and still be refused an action inside it; nothing in this feature has that
shape.

Every mutating route carries `assert_same_site`, like the auth, integrations and Spaces
routes and unlike the older vault ones. Worth knowing in development: the guard compares
`Origin` against `CORS_ORIGINS` + `FRONTEND_URL`, so browsing `http://localhost:3000`
while those point at a tunnel gets a **403 on writes** while reads and the whole vault
keep working. That is the guard doing its job, not a bug.

Cards are serialised with `VaultItemRead`, never `VaultItemDetail`. Both ends of an edge
belong to the caller here, so this is a payload boundary rather than an authz one -- but
a page of neighbours carrying article bodies is kilobytes nothing renders, which is the
same argument `_CARD_COLUMNS` makes.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response, status

from app.api import cards
from app.api.deps import ConnectionServiceDep, CurrentUser, assert_same_site
from app.models.base import ConnectionStatus, Relation
from app.models.connection import MemoryConnection
from app.models.vault import VaultItem
from app.repositories.connection import Neighbour
from app.schemas.connection import (
    ConnectionRead,
    CreateConnectionRequest,
    CreateConnectionResponse,
    GraphEdge,
    GraphResponse,
    HubListResponse,
    HubRead,
    NeighbourhoodResponse,
    SuggestionListResponse,
    SuggestionRead,
    UpdateConnectionRequest,
)
from app.schemas.vault import VaultItemRead

router = APIRouter(prefix="/connections", tags=["connections"])

#: State changes only. A GET carries no CSRF risk and the dependency there would reject a
#: legitimate cross-origin read for no benefit.
_WRITE = [Depends(assert_same_site)]

#: What an edge exposes. Named once so no response can drift from another, and so adding
#: a column to the model does not publish it by accident. `pair_low` and `pair_high` are
#: absent on purpose: they are an index, not data. `user_id` is absent because the caller
#: is the only possible answer.
_PUBLIC_FIELDS = {
    "id",
    "relation",
    "origin",
    "status",
    "score",
    "note",
    "ai_reason",
    "created_at",
}


def _edge(neighbour: Neighbour, card: VaultItemRead) -> ConnectionRead:
    return ConnectionRead(
        **neighbour.connection.model_dump(include=_PUBLIC_FIELDS),
        direction=neighbour.direction,
        memory=card,
    )


@router.get("/suggestions", response_model=SuggestionListResponse)
async def list_suggestions(
    user: CurrentUser,
    service: ConnectionServiceDep,
    response: Response,
    limit: int | None = Query(default=None, ge=1, le=100),
) -> SuggestionListResponse:
    """Edges nobody has accepted yet, strongest first.

    Per user rather than per memory: an edge found from a new capture's side is a
    suggestion on *both* of its memories, so offering it per memory offers one decision
    twice. Declared above `/{connection_id}` -- a literal segment that could be read as a
    parameter has to be matched first.
    """
    cards.no_store(response)
    found, total = await service.suggestions(user.id, limit=limit)
    items = [item for _edge_row, source, target in found for item in (source, target)]
    by_id = {card.id: card for card in await cards.read_cards(items)}
    return SuggestionListResponse(
        suggestions=[
            SuggestionRead(
                **row.model_dump(include={"id", "relation", "score", "ai_reason", "created_at"}),
                source=by_id[source.id],
                target=by_id[target.id],
            )
            for row, source, target in found
        ],
        total=total,
    )


@router.get("/graph", response_model=GraphResponse)
async def read_graph(
    user: CurrentUser,
    service: ConnectionServiceDep,
    response: Response,
    limit: int | None = Query(default=None, ge=1, le=1000),
    include_dismissed: bool = Query(default=False),
) -> GraphResponse:
    """Every edge in the caller's vault, with each memory an edge touches sent once.

    What the canvas is drawn from. Declared above `/{connection_id}` for the same reason
    `/suggestions` and `/hubs` are -- a literal segment that could be read as a parameter
    has to be matched first.

    `include_dismissed` is off by default and is a debugging affordance rather than a
    view: a dismissed row is kept so the derivation cannot re-propose the pair, and
    drawing somebody's own "no" back onto the canvas would make a decision they already
    took look undone.

    `no_store`, like every response carrying presigned thumbnail URLs -- those embed a
    credential with a six-hour life, and a shared cache is exactly where one should not
    sit.
    """
    cards.no_store(response)
    statuses = (
        (ConnectionStatus.confirmed, ConnectionStatus.suggested, ConnectionStatus.dismissed)
        if include_dismissed
        else None
    )
    found, total = await service.graph(user.id, statuses=statuses, limit=limit)

    # One card per memory however many edges touch it. `read_cards` presigns thumbnails,
    # which is a request per distinct key -- sending a hub's card six times would pay for
    # that six times and hand the browser six URLs for one picture.
    seen: dict[uuid.UUID, VaultItem] = {}
    for _row, source, target in found:
        seen.setdefault(source.id, source)
        seen.setdefault(target.id, target)
    nodes = await cards.read_cards(list(seen.values()))

    return GraphResponse(
        nodes=nodes,
        edges=[
            GraphEdge(
                **row.model_dump(include=_PUBLIC_FIELDS),
                source_id=row.source_item_id,
                target_id=row.target_item_id,
            )
            for row, _source, _target in found
        ],
        total=total,
        truncated=total > len(found),
    )


@router.get("/hubs", response_model=HubListResponse)
async def list_hubs(
    user: CurrentUser,
    service: ConnectionServiceDep,
    response: Response,
    limit: int | None = Query(default=None, ge=1, le=100),
) -> HubListResponse:
    """The caller's most-connected memories, busiest first.

    What the page opens on when nobody named a memory. Reached from the nav, the only
    other option is a list to choose from -- and a list of the *newest* memories says
    nothing about which are worth opening, since the newest capture is usually the one
    with the fewest edges.

    An empty list is a real answer, not a 404: a vault with no connections yet has no hubs
    and the page falls back to offering recent memories.

    Declared above `/{connection_id}` -- a literal segment that could be read as a
    parameter has to be matched first.
    """
    cards.no_store(response)
    found = await service.hubs(user.id, limit=limit)
    rendered = await cards.read_cards([item for item, _count in found])
    return HubListResponse(
        hubs=[
            HubRead(memory=card, connection_count=count)
            for card, (_item, count) in zip(rendered, found, strict=True)
        ]
    )


@router.get("/for-item/{item_id}", response_model=NeighbourhoodResponse)
async def neighbourhood(
    item_id: uuid.UUID,
    user: CurrentUser,
    service: ConnectionServiceDep,
    response: Response,
    relation: Relation | None = None,
    include_suggested: bool = False,
    limit: int | None = Query(default=None, ge=1, le=100),
) -> NeighbourhoodResponse:
    """One memory and everything connected to it, read from both ends.

    Two statements: the ownership gate on the focus, then the neighbourhood and its total
    together. Inferring "not yours" from an empty edge list would save the first one and
    would stop being an ownership check.

    Declared above `/{connection_id}` for the literal-segment reason above.
    """
    cards.no_store(response)
    statuses = (
        [ConnectionStatus.confirmed, ConnectionStatus.suggested]
        if include_suggested
        else [ConnectionStatus.confirmed]
    )
    focus, found, total = await service.neighbourhood(
        item_id, user.id, statuses=statuses, relation=relation, limit=limit
    )
    rendered = await cards.read_cards([focus, *(n.item for n in found)])
    focus_card, neighbour_cards = rendered[0], rendered[1:]
    return NeighbourhoodResponse(
        focus=focus_card,
        connections=[
            _edge(neighbour, card)
            for neighbour, card in zip(found, neighbour_cards, strict=True)
        ],
        total=total,
    )


@router.post(
    "", response_model=CreateConnectionResponse, dependencies=_WRITE
)
async def create_connection(
    payload: CreateConnectionRequest,
    user: CurrentUser,
    service: ConnectionServiceDep,
    response: Response,
) -> CreateConnectionResponse:
    """Connect two of the caller's own memories.

    **200, not 201, and never 409.** The pair is unique regardless of order, so connecting
    B to A when A to B already exists re-labels the one edge rather than failing --
    re-adding is a normal outcome, not an error, and `created` says which happened. A 409
    would make the obvious action look like a broken server.

    Both ids are checked against the caller *per item*, not per request: a body carrying
    two ids is exactly where a stranger's is easiest to slip in.
    """
    cards.no_store(response)
    connection, created, other = await service.connect(
        user.id,
        payload.source_id,
        payload.target_id,
        relation=payload.relation,
        note=payload.note,
    )
    return CreateConnectionResponse(
        connection=await _one(connection, other, payload.source_id),
        created=created,
    )


@router.patch(
    "/{connection_id}", response_model=ConnectionRead, dependencies=_WRITE
)
async def update_connection(
    connection_id: uuid.UUID,
    payload: UpdateConnectionRequest,
    user: CurrentUser,
    service: ConnectionServiceDep,
    response: Response,
) -> ConnectionRead:
    """Re-label an edge, or change its note. An empty note clears it."""
    cards.no_store(response)
    connection, other = await service.update(
        connection_id, user.id, relation=payload.relation, note=payload.note
    )
    return await _one(connection, other, connection.source_item_id)


@router.post(
    "/{connection_id}/confirm", response_model=ConnectionRead, dependencies=_WRITE
)
async def confirm_connection(
    connection_id: uuid.UUID,
    user: CurrentUser,
    service: ConnectionServiceDep,
    response: Response,
) -> ConnectionRead:
    """Accept a suggestion. Until this happens no lane that reads connections sees it."""
    cards.no_store(response)
    connection, other = await service.confirm(connection_id, user.id)
    return await _one(connection, other, connection.source_item_id)


@router.post(
    "/{connection_id}/retype", response_model=ConnectionRead, dependencies=_WRITE
)
async def retype_connection(
    connection_id: uuid.UUID,
    user: CurrentUser,
    service: ConnectionServiceDep,
    response: Response,
) -> ConnectionRead:
    """Ask a model how these two memories relate, and label the edge with its answer.

    The only connection route that spends a model call, which is why it is the only one
    behind a per-user hourly cap. 503 when typing is switched off, unconfigured, or the
    caller has used up that cap -- one answer for three causes, because they are the same
    thing from outside and separating them would report which of an operator's settings is
    off.

    The answer is written to `ai_reason`, never to `note`: one is rendered as
    machine-written and the other as the owner's own words.
    """
    cards.no_store(response)
    connection, other = await service.retype(connection_id, user.id)
    return await _one(connection, other, connection.source_item_id)


@router.post(
    "/{connection_id}/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=_WRITE,
)
async def dismiss_connection(
    connection_id: uuid.UUID, user: CurrentUser, service: ConnectionServiceDep
) -> None:
    """Decline a suggestion, and never be offered that pair again.

    The row is kept rather than deleted: it is what stops the derivation re-proposing the
    pair. Connecting the two by hand later revives it.
    """
    await service.dismiss(connection_id, user.id)


@router.delete(
    "/{connection_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=_WRITE
)
async def delete_connection(
    connection_id: uuid.UUID, user: CurrentUser, service: ConnectionServiceDep
) -> None:
    await service.delete(connection_id, user.id)


async def _one(
    connection: MemoryConnection, other: VaultItem, read_from: uuid.UUID
) -> ConnectionRead:
    """Render one edge as seen from `read_from` -- the end the caller named.

    A write answers with the edge it just acted on, so "the other end" is decided by which
    id the caller sent. It is the same computation the neighbourhood read makes, done for
    a single row, and the row itself has already been read and ownership-checked by the
    service -- this adds no statement.
    """
    outgoing = connection.source_item_id == read_from
    card = (await cards.read_cards([other]))[0]
    return ConnectionRead(
        **connection.model_dump(include=_PUBLIC_FIELDS),
        direction="outgoing" if outgoing else "incoming",
        memory=card,
    )
