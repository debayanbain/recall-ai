"""Keeping a card's picture, instead of borrowing one that expires.

A link's thumbnail is whatever its page advertised as `og:image`, and for the two sources
that dominate this vault -- Instagram and Facebook -- that is a **signed** CDN URL:
`scontent.*.fbcdn.net` and `*.cdninstagram.com` hand out links carrying an expiry, and
once it passes they answer `403` to everybody. Measured against the live vault on
2026-09-13: eleven of twelve stored stills were already dead, the survivor being the one
saved days rather than weeks earlier.

Storing that URL is therefore storing a credential with someone else's clock on it. The
failure is quiet in the worst way -- the capture is fine, the row is fine, the picture
simply stops arriving about a week later, and a broken `<img>` looks like a frontend bug.
It also cost real requests: a failed cover made the card try to re-mint a download link
for a memory that has no stored file at all, so every link card spent a round trip on a
guaranteed 404.

So the worker copies the image once, at capture time, into the same private bucket the
uploads live in, and the card is served from our own copy for as long as the memory
exists. Four rules hold it:

* **The fetch goes through `assert_safe_url`.** The URL was written by a scraped page, so
  this is the same SSRF surface as an extractor: without the guard, a page advertising
  `og:image` of `http://169.254.169.254/latest/meta-data/` would put cloud IAM credentials
  into a user's vault as their card art. Redirects are followed one hop at a time and each
  hop is re-validated, exactly as `ArticleExtractor` does.
* **The type is decided from the bytes**, never from `Content-Type` and never from the
  URL's extension -- same rule as `services/documents.py`. SVG is refused outright: it is
  executable in a browser context, and this is the one object in the bucket that a page is
  meant to render.
* **Failure is silent and total.** A thumbnail is decoration beside a memory. Nothing here
  may fail a capture, retry a task, or leave the item in a state a sweeper has to rescue --
  every exception is logged and swallowed, and the item keeps the scraped URL it already
  had, which at least works for the first week.
* **The key never reaches the browser.** Like `storage_key`, `thumbnail_key` stays
  server-side and the URL a response carries is minted per response, with a TTL long
  enough to outlive the page that renders it (`THUMBNAIL_LINK_TTL_SECONDS`).
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.net import UnsafeUrlError, assert_safe_url
from app.models.vault import VaultItem
from app.storage import ObjectStorage

log = get_logger("thumbnails")

#: What a card image may be, keyed by the signature that proves it. WebP and GIF both
#: start with a RIFF/GIF header this checks in `_sniff`; JPEG and PNG are fixed prefixes.
#: SVG is deliberately absent -- see the module docstring.
_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"
_GIF = b"GIF8"
_RIFF = b"RIFF"
_WEBP = b"WEBP"

#: How many redirects to follow. A share shortlink resolves in one or two; a chain longer
#: than this is not a picture, it is a loop.
_MAX_HOPS = 4

#: Enough of the body to identify it. Every signature this knows fits in the first 12.
_SNIFF_BYTES = 16

#: The reverse of `_sniff`, for re-declaring the type when a link is minted. Keyed by the
#: extension this module itself wrote into the object key, so it cannot be steered.
_MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
}


def _sniff(head: bytes) -> tuple[str, str] | None:
    """`(mime, extension)` for bytes we are willing to store, else None."""
    if head.startswith(_JPEG):
        return "image/jpeg", "jpg"
    if head.startswith(_PNG):
        return "image/png", "png"
    if head.startswith(_GIF):
        return "image/gif", "gif"
    if head.startswith(_RIFF) and head[8:12] == _WEBP:
        return "image/webp", "webp"
    return None


def object_key(user_id: uuid.UUID, item_id: uuid.UUID, ext: str) -> str:
    """`users/<user>/<item>/thumb-<random>.<ext>`.

    Same shape and the same reasoning as `documents.object_key` -- every component is
    server-generated, so nothing a page advertised can become part of a path -- with a
    `thumb-` prefix so a bucket listing says which object is the card art.
    """
    return f"users/{user_id}/{item_id}/thumb-{uuid.uuid4().hex}.{ext}"


async def _fetch(url: str) -> tuple[bytes, str, str] | None:
    """Download a card image, or None if it is not one we will keep.

    Redirects are followed by hand so that every hop is re-validated: a URL that resolves
    to a public address and then 302s to `169.254.169.254` is the whole reason the guard
    cannot be a one-shot check on the string the user supplied.
    """
    limit = settings.THUMBNAIL_MAX_BYTES
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
        for _ in range(_MAX_HOPS):
            assert_safe_url(url)
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    url = str(response.url.join(location))
                    continue
                if response.status_code != 200:
                    log.info("thumbnail_source_unavailable", status=response.status_code)
                    return None

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        # Abandoned rather than truncated: half an image is not an image,
                        # and reading the rest to find that out is the cost being avoided.
                        log.info("thumbnail_too_large", limit=limit)
                        return None

            sniffed = _sniff(bytes(body[:_SNIFF_BYTES]))
            if sniffed is None or not body:
                log.info("thumbnail_not_an_image")
                return None
            mime, ext = sniffed
            return bytes(body), mime, ext
    log.info("thumbnail_too_many_redirects")
    return None


async def mirror(item: VaultItem, storage: ObjectStorage | None) -> None:
    """Copy this item's scraped still into our bucket, if there is one to copy.

    Mutates `item.thumbnail_key` and `item.item_metadata` in place and never raises. The
    caller is mid-pipeline and will persist the item anyway; a thumbnail must not be able
    to change whether that succeeds.
    """
    source = item.thumbnail_url
    if not settings.MIRROR_THUMBNAILS or storage is None or not source:
        return
    # Re-processing re-runs the extractor, which hands back the same scraped URL. Copying
    # it again would spend a download and leave an orphan for an identical picture, so a
    # mirror is only refreshed when the page started advertising a different one.
    if item.thumbnail_key and item.item_metadata.get("thumbnail_source_url") == source:
        return

    try:
        fetched = await _fetch(source)
    except UnsafeUrlError as exc:
        # Worth a line of its own: a page advertising an internal address as its card
        # image is either badly broken or pointed at us on purpose.
        log.warning("thumbnail_unsafe_url", item_id=str(item.id), reason=str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - decoration must never fail a capture
        log.info("thumbnail_fetch_failed", item_id=str(item.id), error=type(exc).__name__)
        return
    if fetched is None:
        return

    data, mime, ext = fetched
    key = object_key(item.user_id, item.id, ext)
    try:
        await storage.upload(key, data, mime)
    except Exception as exc:  # noqa: BLE001 - same policy as the fetch above
        log.info("thumbnail_store_failed", item_id=str(item.id), error=type(exc).__name__)
        return

    previous = item.thumbnail_key
    item.thumbnail_key = key
    # The row now points at our copy. The scraped URL is kept beside it because it is the
    # only record of where the picture came from, and because it is what a re-run would
    # have to fetch again if this object were ever lost.
    item.item_metadata = {**item.item_metadata, "thumbnail_source_url": source}
    log.info("thumbnail_mirrored", item_id=str(item.id), mime=mime, size=len(data))

    if previous and previous != key:
        # Reprocessing replaces the picture; the old object is unreachable the moment the
        # column moves, and an orphan in the bucket is a bill with nothing pointing at it.
        try:
            await storage.delete(previous)
        except Exception as exc:  # noqa: BLE001
            log.info("thumbnail_cleanup_failed", error=type(exc).__name__)


async def presigned_urls(
    items: Sequence[VaultItem], storage: ObjectStorage | None
) -> dict[uuid.UUID, str]:
    """Per-item URLs for the mirrored thumbnails among `items`.

    Minted together rather than one route per picture: a card listing is one statement by
    design (see "Latency" in CLAUDE.md), and a `GET /vault/{id}/thumbnail` per card would
    put the round trips straight back. Signing is local HMAC, so the cost here is not the
    network -- items without a mirror are skipped entirely, and a failure to sign one is
    that card falling back to whatever `thumbnail_url` already held.
    """
    if storage is None:
        return {}
    keyed = [item for item in items if item.thumbnail_key]
    if not keyed:
        return {}

    async def one(item: VaultItem) -> tuple[uuid.UUID, str] | None:
        key = item.thumbnail_key or ""
        ext = key.rsplit(".", 1)[-1]
        try:
            url = await storage.presigned_get(
                key,
                filename=f"thumbnail.{ext}",
                # The type we decided from the bytes when we stored it, recovered from the
                # extension *we* wrote. `Content-Disposition: attachment` rides along as
                # it does on every object here; it is inert for an `<img>`, which renders
                # a subresource regardless, and it keeps the bucket unable to serve a page.
                content_type=_MIME_BY_EXT.get(ext, "application/octet-stream"),
                expires=settings.THUMBNAIL_LINK_TTL_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("thumbnail_sign_failed", item_id=str(item.id), error=type(exc).__name__)
            return None
        return item.id, url

    signed = await asyncio.gather(*(one(item) for item in keyed))
    return {item_id: url for item_id, url in (s for s in signed if s is not None)}
