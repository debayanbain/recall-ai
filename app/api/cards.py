"""Turning rows into cards, in the one place that has to know about the bucket.

`VaultItemRead` is a pydantic model and pydantic cannot await, but a mirrored thumbnail
lives in a private bucket and its URL has to be signed per response. So the signing
happens here, once per response, for every route that serves cards -- the vault listing,
a memory's detail, and a Space's items.

Kept in `api/` rather than in a service because it is presentation: it exists to fill in a
field of a response model, and nothing below the router needs it.
"""
from __future__ import annotations

from collections.abc import Sequence

from fastapi import Response

from app.models.vault import VaultItem
from app.schemas.vault import TrashItemRead, VaultItemDetail, VaultItemRead
from app.services import thumbnails
from app.storage import get_storage


def no_store(response: Response) -> None:
    """Mark a response that carries presigned URLs as uncacheable by anyone else.

    A signed thumbnail link is a bearer credential for as long as it lasts, exactly like
    the one `GET /vault/{id}/file` mints, so a listing that embeds one must not sit in a
    shared cache. The images themselves are still cached by the browser under their own
    (signed) URLs, which is where caching actually pays here.
    """
    response.headers["Cache-Control"] = "private, no-store"


async def read_cards(items: Sequence[VaultItem]) -> list[VaultItemRead]:
    """Serialise cards, with `thumbnail_url` pointing at our mirror where there is one.

    Items with no mirror keep whatever the extractor scraped -- which is right for a
    YouTube still (stable, public, and not ours to store) and is the honest fallback for
    a link whose mirror failed.
    """
    urls = await thumbnails.presigned_urls(items, get_storage())
    cards = []
    for item in items:
        card = VaultItemRead.model_validate(item)
        mirrored = urls.get(item.id)
        if mirrored:
            card.thumbnail_url = mirrored
        cards.append(card)
    return cards


async def read_detail(item: VaultItem) -> VaultItemDetail:
    """The same substitution for a single memory's detail response."""
    urls = await thumbnails.presigned_urls([item], get_storage())
    detail = VaultItemDetail.model_validate(item)
    mirrored = urls.get(item.id)
    if mirrored:
        detail.thumbnail_url = mirrored
    return detail


async def read_trash_cards(items: Sequence[VaultItem]) -> list[TrashItemRead]:
    """The same cards, plus when each one stops being recoverable.

    `purge_after` rides along as a computed field on the schema, derived from the row's
    own `deleted_at` and the live setting -- so the date a person reads is the date the
    sweep will act on, not a copy written when the memory was deleted.
    """
    urls = await thumbnails.presigned_urls(items, get_storage())
    cards: list[TrashItemRead] = []
    for item in items:
        card = TrashItemRead.model_validate(item)
        mirrored = urls.get(item.id)
        if mirrored:
            card.thumbnail_url = mirrored
        cards.append(card)
    return cards
