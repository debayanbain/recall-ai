"""Mirroring a card's picture into our own bucket.

The bug these exist for is silent by construction: a scraped `og:image` is a signed CDN
URL, it works for about a week, and then every link card loses its picture with nothing
failing anywhere. So the assertions here are less about the happy path than about the four
ways this could go wrong quietly -- fetching something that is not an image, fetching
something enormous, following a redirect into the private network, and letting any of that
reach the caller as an exception that would fail a capture.

Offline: no DB, no bucket, no network. `httpx` is served by a `MockTransport` and the SSRF
guard is stubbed per test so that *whether it was consulted* can be asserted directly.
"""
from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.config import settings
from app.core.net import UnsafeUrlError
from app.models.base import ContentType
from app.models.vault import VaultItem
from app.services import thumbnails

_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 64


class _Storage:
    """Just enough of `ObjectStorage` to see what was written and removed."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.deleted: list[str] = []

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def download(self, key: str) -> bytes:
        return self.objects[key][0]

    async def presigned_get(
        self, key: str, *, filename: str, content_type: str, expires: int
    ) -> str:
        return f"https://bucket.invalid/{key}?exp={expires}&type={content_type}"

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


class _FailingStorage(_Storage):
    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        raise RuntimeError("bucket unreachable")


def _item(url: str | None = "https://cdn.example.com/still.jpg", **kwargs: object) -> VaultItem:
    return VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.instagram,
        thumbnail_url=url,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _allow_every_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is real code with real DNS behind it; tests that care stub it themselves."""
    monkeypatch.setattr(thumbnails, "assert_safe_url", lambda url: None)


def _serve(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Point `thumbnails`' own httpx at a MockTransport, leaving its arguments intact."""
    real = httpx.AsyncClient

    def factory(**kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(thumbnails.httpx, "AsyncClient", factory)


def _always(body: bytes, *, status: int = 200, content_type: str = "image/jpeg") -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers={"content-type": content_type})

    return handler


async def test_mirror_stores_the_image_and_points_the_row_at_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _always(_JPEG))
    storage = _Storage()
    item = _item()

    await thumbnails.mirror(item, storage)

    assert item.thumbnail_key is not None
    assert item.thumbnail_key.startswith(f"users/{item.user_id}/{item.id}/thumb-")
    assert item.thumbnail_key.endswith(".jpg")
    assert storage.objects[item.thumbnail_key] == (_JPEG, "image/jpeg")
    # The scraped URL is kept: it is the only record of where the picture came from, and
    # it is what tells a later run that this source has already been mirrored.
    assert item.item_metadata["thumbnail_source_url"] == "https://cdn.example.com/still.jpg"


async def test_the_type_comes_from_the_bytes_not_the_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server claiming `image/jpeg` over PNG bytes is stored as what it actually is."""
    _serve(monkeypatch, _always(_PNG, content_type="image/jpeg"))
    storage = _Storage()
    item = _item()

    await thumbnails.mirror(item, storage)

    assert item.thumbnail_key is not None and item.thumbnail_key.endswith(".png")
    assert storage.objects[item.thumbnail_key][1] == "image/png"


async def test_webp_is_recognised_by_its_riff_container(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _always(_WEBP))
    storage = _Storage()
    item = _item()

    await thumbnails.mirror(item, storage)

    assert item.thumbnail_key is not None and item.thumbnail_key.endswith(".webp")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<!doctype html><html>login wall</html>", id="html"),
        pytest.param(b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', id="svg"),
        pytest.param(b"", id="empty"),
    ],
)
async def test_only_real_raster_images_are_stored(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    """SVG is refused with the rest: it is the one object here a page is meant to render."""
    _serve(monkeypatch, _always(body))
    storage = _Storage()
    item = _item()

    await thumbnails.mirror(item, storage)

    assert item.thumbnail_key is None
    assert storage.objects == {}


async def test_an_oversized_body_is_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "THUMBNAIL_MAX_BYTES", 32)
    _serve(monkeypatch, _always(b"\xff\xd8\xff\xe0" + b"0" * 4096))
    storage = _Storage()
    item = _item()

    await thumbnails.mirror(item, storage)

    assert item.thumbnail_key is None
    assert storage.objects == {}


async def test_a_dead_source_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 403 that started all this: the row is untouched and nothing raises."""
    _serve(monkeypatch, _always(b"Access denied", status=403))
    storage = _Storage()
    item = _item()

    await thumbnails.mirror(item, storage)

    assert item.thumbnail_key is None
    assert item.thumbnail_url == "https://cdn.example.com/still.jpg"


async def test_every_redirect_hop_is_revalidated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public hostname that 302s inward is the reason the guard cannot be a one-shot."""
    seen: list[str] = []
    monkeypatch.setattr(thumbnails, "assert_safe_url", lambda url: seen.append(url))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/still.jpg":
            return httpx.Response(302, headers={"location": "https://cdn2.example.com/real.jpg"})
        return httpx.Response(200, content=_JPEG)

    _serve(monkeypatch, handler)
    await thumbnails.mirror(_item(), _Storage())

    assert seen == ["https://cdn.example.com/still.jpg", "https://cdn2.example.com/real.jpg"]


async def test_an_unsafe_source_is_refused_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def guard(url: str) -> None:
        raise UnsafeUrlError("host resolves to a non-public address")

    monkeypatch.setattr(thumbnails, "assert_safe_url", guard)
    _serve(monkeypatch, _always(_JPEG))
    storage = _Storage()
    item = _item("http://169.254.169.254/latest/meta-data/")

    await thumbnails.mirror(item, storage)

    assert item.thumbnail_key is None
    assert storage.objects == {}


async def test_a_transport_failure_never_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _serve(monkeypatch, handler)
    item = _item()

    await thumbnails.mirror(item, _Storage())

    assert item.thumbnail_key is None


async def test_a_bucket_failure_leaves_the_row_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _always(_JPEG))
    item = _item()

    await thumbnails.mirror(item, _FailingStorage())

    assert item.thumbnail_key is None
    assert "thumbnail_source_url" not in item.item_metadata


async def test_an_unchanged_source_is_not_downloaded_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=_JPEG)

    _serve(monkeypatch, handler)
    storage = _Storage()
    item = _item()

    await thumbnails.mirror(item, storage)
    await thumbnails.mirror(item, storage)

    assert calls == 1


async def test_a_changed_source_replaces_and_removes_the_old_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An orphan in the bucket is a bill with nothing pointing at it."""
    _serve(monkeypatch, _always(_JPEG))
    storage = _Storage()
    item = _item()

    await thumbnails.mirror(item, storage)
    first = item.thumbnail_key
    item.thumbnail_url = "https://cdn.example.com/other.jpg"
    await thumbnails.mirror(item, storage)

    assert first is not None and item.thumbnail_key not in (None, first)
    assert storage.deleted == [first]


async def test_mirroring_is_skipped_without_a_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _always(_JPEG))
    item = _item()

    await thumbnails.mirror(item, None)

    assert item.thumbnail_key is None


async def test_the_switch_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "MIRROR_THUMBNAILS", False)
    _serve(monkeypatch, _always(_JPEG))
    storage = _Storage()

    await thumbnails.mirror(_item(), storage)

    assert storage.objects == {}


async def test_only_mirrored_items_are_signed() -> None:
    """A YouTube still stays the scraped URL: stable, public, and not ours to store."""
    mirrored = _item(thumbnail_key="users/a/b/thumb-1.png")
    scraped = _item()

    urls = await thumbnails.presigned_urls([mirrored, scraped], _Storage())

    assert set(urls) == {mirrored.id}
    assert urls[mirrored.id].startswith("https://bucket.invalid/users/a/b/thumb-1.png")
    assert f"exp={settings.THUMBNAIL_LINK_TTL_SECONDS}" in urls[mirrored.id]
    # The type is recovered from the extension we wrote, not guessed or defaulted.
    assert "type=image/png" in urls[mirrored.id]


async def test_signing_without_a_bucket_is_empty() -> None:
    assert await thumbnails.presigned_urls([_item(thumbnail_key="k.jpg")], None) == {}


# --------------------------------------------------------------------------------------
# Wiring: that something actually hands the pipeline a bucket, and that every card
# surface substitutes the mirror
# --------------------------------------------------------------------------------------


def test_every_pipeline_entry_point_is_given_a_bucket() -> None:
    """`mirror` returns immediately when `storage is None`, and returns *quietly*.

    So a `ProcessingService` built without one keeps the scraped `og:image` and nothing
    anywhere fails -- which is exactly how the deferred branch shipped mirroring that did
    not mirror. That branch is Instagram and Facebook: the signed `fbcdn` /
    `cdninstagram` stills that expire in about a week, and the whole reason this feature
    exists. The webhook path and the sweeper each construct their own service, so both
    have to be checked, and a third entry point added later would slip past a test that
    named only the two.
    """
    import ast
    from pathlib import Path

    from app.queue import tasks

    tree = ast.parse(Path(tasks.__file__).read_text(encoding="utf-8"))
    built = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ProcessingService"
    ]

    assert built, "no ProcessingService construction found -- has this moved?"
    for call in built:
        rendered = ast.unparse(call)
        assert "get_storage()" in rendered, (
            f"a pipeline entry point has no bucket, so it cannot mirror: {rendered}"
        )


async def test_read_cards_substitutes_the_mirror_for_the_scraped_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The substitution itself: a mirrored item is served our URL, not the dead one."""
    from app.api import cards as cards_module

    storage = _Storage()
    monkeypatch.setattr(cards_module, "get_storage", lambda: storage)
    item = _item("https://dead.example/x.jpg", thumbnail_key="users/a/b/c.jpg")

    rendered = await cards_module.read_cards([item])

    assert rendered[0].thumbnail_url is not None
    assert rendered[0].thumbnail_url.startswith("https://bucket.invalid/")


#: Responses that carry memory cards. A route returning one of these and *not* going
#: through `app/api/cards.py` serves whatever `thumbnail_url` the extractor scraped --
#: which for an Instagram or Facebook still is a signed URL that stops resolving after
#: about a week. `public.py` is deliberately excluded: see the test below.
_CARD_RESPONSES = {
    "VaultListResponse",
    "VaultItemDetail",
    "VaultItemRead",
    "SpaceDetail",
    "SpaceConnectionsResponse",
}


def test_every_authenticated_card_route_serves_the_mirror() -> None:
    """The same memory must not have two different pictures depending on where it is read.

    Search served `VaultItemRead.model_validate` directly while the vault listing, the
    detail page and a Space's items all went through `cards.read_cards` -- so the one
    surface showing the broken, expired still was the one somebody reaches by looking for
    the memory on purpose. Asserted over every card-returning route rather than that one,
    because the next route to forget is not this one.
    """
    import ast
    from pathlib import Path

    from app.api.v1 import search, spaces, vault

    missing: list[str] = []
    for module in (vault, spaces, search):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or node.returns is None:
                continue
            returns = ast.unparse(node.returns)
            if not any(name in returns for name in _CARD_RESPONSES):
                continue
            decorators = " ".join(ast.unparse(d) for d in node.decorator_list)
            if "HTTP_201_CREATED" in decorators:
                # A route that just *made* the row. `thumbnail_key` is definitionally NULL
                # at that moment -- mirroring happens in the worker, after the commit --
                # so going through `read_cards` would be a round trip that can only ever
                # find nothing. `reprocess` returns an *existing* item and is a 200, so it
                # is still covered, which is the line this draws.
                continue
            body = ast.unparse(node)
            if "read_cards" not in body and "read_detail" not in body:
                missing.append(f"{module.__name__}.{node.name} -> {returns}")

    assert missing == [], f"card routes not serving the mirror: {missing}"


def test_the_public_page_is_deliberately_left_on_the_scraped_url() -> None:
    """Not an oversight, and worth pinning so nobody "fixes" it into a leak.

    `cards.read_cards` mints presigned URLs, and a presigned URL is a bearer credential
    for its whole TTL. Putting one on an *unauthenticated* share page hands anyone who
    opens it a direct, time-limited handle on a private bucket object -- which is a very
    different thing from serving it to a signed-in member.

    The cost is real and accepted: a published Space shows broken pictures once the
    scraped stills expire. Fixing that properly means a public thumbnail route with its
    own access rules, not reaching for `read_cards` here.
    """
    from pathlib import Path

    from app.api.v1 import public

    source = Path(public.__file__).read_text(encoding="utf-8")
    assert "read_cards" not in source


def test_every_route_that_embeds_a_signed_thumbnail_forbids_shared_caching() -> None:
    """A presigned URL is a bearer credential for its whole TTL.

    `cards.no_store` is what keeps a response carrying one out of a shared cache, and the
    route it was missing from was `GET /spaces/{id}` -- the only one in the product that
    serves *other members'* memories, where a cached copy would hand one member's cards to
    whoever the cache answered next. Asserted against the source because the failure is a
    header that is simply absent: nothing raises, and no response body differs.
    """
    import ast
    from pathlib import Path

    from app.api.v1 import search, spaces, vault

    for module in (vault, spaces, search):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            body = ast.unparse(node)
            if "read_cards" not in body and "read_detail" not in body:
                continue
            assert "no_store" in body, (
                f"{module.__name__}.{node.name} serves signed thumbnails "
                "without marking the response uncacheable"
            )


# --- carousels ---------------------------------------------------------------
#
# A carousel is the case where losing the pictures loses the memory: the caption says
# "in this carousel I've shared 80+ trusted job platforms" and the platforms are pixels.
# Fourteen signed fbcdn URLs with a week on them is not a saved post, so these pin the
# mirroring rather than the fetching, which the tests above already cover.


def _carousel(n: int = 3) -> VaultItem:
    item = _item(url=None)
    item.item_metadata = {
        "slides": [f"https://cdn.example.com/slide-{i}.jpg" for i in range(n)]
    }
    return item


async def test_slides_are_mirrored_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _always(_JPEG))
    storage = _Storage()
    item = _carousel(3)

    await thumbnails.mirror_slides(item, storage)

    keys = item.item_metadata["slide_keys"]
    assert len(keys) == 3
    # The index is in the name so a bucket listing can say which picture is which slide.
    for index, key in enumerate(keys):
        assert key.startswith(f"users/{item.user_id}/{item.id}/slide-{index:02d}-")
        assert key in storage.objects


async def test_a_slide_that_fails_leaves_a_hole_rather_than_shifting_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slide 3 means the third picture. Compacting would silently renumber the carousel."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("slide-1.jpg"):
            return httpx.Response(404)
        return httpx.Response(200, content=_JPEG, headers={"content-type": "image/jpeg"})

    _serve(monkeypatch, handler)
    storage = _Storage()
    item = _carousel(3)

    await thumbnails.mirror_slides(item, storage)

    keys = item.item_metadata["slide_keys"]
    assert len(keys) == 3
    assert keys[1] is None
    assert keys[0] and keys[2]
    assert keys[2].startswith(f"users/{item.user_id}/{item.id}/slide-02-")


async def test_mirroring_the_same_slides_twice_copies_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reprocessing hands back the same URLs; re-copying spends N downloads for nothing."""
    _serve(monkeypatch, _always(_JPEG))
    storage = _Storage()
    item = _carousel(3)

    await thumbnails.mirror_slides(item, storage)
    first = list(item.item_metadata["slide_keys"])
    await thumbnails.mirror_slides(item, storage)

    assert item.item_metadata["slide_keys"] == first
    assert len(storage.objects) == 3
    assert storage.deleted == []


async def test_slides_never_raise_into_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same policy as the thumbnail: a picture must not decide whether a capture works."""
    _serve(monkeypatch, _always(_JPEG))
    item = _carousel(2)

    await thumbnails.mirror_slides(item, _FailingStorage())

    assert "slide_keys" not in item.item_metadata


async def test_an_unsafe_slide_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The URLs were written by a scraped page -- the same SSRF surface as an extractor."""

    def guard(url: str) -> None:
        raise UnsafeUrlError("link-local")

    monkeypatch.setattr(thumbnails, "assert_safe_url", guard)
    _serve(monkeypatch, _always(_JPEG))
    storage = _Storage()
    item = _carousel(2)

    await thumbnails.mirror_slides(item, storage)

    assert storage.objects == {}
    assert "slide_keys" not in item.item_metadata


async def test_presigned_slide_urls_keep_the_holes(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _always(_JPEG))
    storage = _Storage()
    item = _carousel(3)
    item.item_metadata = {**item.item_metadata, "slide_keys": ["a/one.jpg", None, "a/two.png"]}

    urls = await thumbnails.presigned_slide_urls(item, storage)

    assert len(urls) == 3
    assert urls[1] is None
    assert urls[0] and urls[0].startswith("https://bucket.invalid/a/one.jpg")
    # The type is recovered from the extension *we* wrote, never from anything scraped.
    assert "type=image/png" in (urls[2] or "")


def test_mime_for_key_reads_the_extension_we_wrote() -> None:
    """The bug this pins cost fourteen vision calls that could only fail.

    `describe_image` refuses an image whose type it was not told, so reading a slide with
    `mime_type=None` raises `VisionError` for every slide -- which in the log is
    indistinguishable from fourteen genuinely unreadable pictures.
    """
    assert thumbnails.mime_for_key("users/a/b/slide-00-abc.jpg") == "image/jpeg"
    assert thumbnails.mime_for_key("users/a/b/slide-01-abc.PNG") == "image/png"
    assert thumbnails.mime_for_key("users/a/b/thumb-abc.webp") == "image/webp"
    # Not a type this module ever wrote, so there is nothing to claim about the bytes.
    assert thumbnails.mime_for_key("users/a/b/slide-02-abc.svg") is None
    assert thumbnails.mime_for_key("no-extension") is None
