"""Reading a reel's video, and what each kind of failure costs.

Offline -- the download, the vision model and the transcription model are all fakes. What
is pinned is the policy around them, because every one of these decisions is invisible
when it goes wrong: a caption silently replaced by a machine's description, a phishing
domain read off a frame and rendered as a link, or a working memory failed to retry an
enhancement it did not need.
"""
from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from app.core.net import UnsafeUrlError
from app.extractors.base import ExtractedContent
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services import processing_service as ps
from app.services import video

CAPTION = "New drop is live. Full guide at https://example.com/guide"

FRAMES_TEXT = (
    "On-screen text:\nGET 20% OFF\nshop.example/sale\n\n"
    "Links:\nshop.example/sale\n\n"
    "What happens:\nSomeone unboxes a keyboard and types on it."
)


class _VaultRepo:
    def __init__(self, item: VaultItem) -> None:
        self.item = item
        self.chunks: list[Any] = []

    async def get_unscoped(self, _id: uuid.UUID) -> VaultItem:
        return self.item

    async def add(self, item: VaultItem) -> VaultItem:
        return item

    async def upsert_chunk(self, **kw: Any) -> None:
        self.chunks.append(kw)


class _Extractor:
    """Stands in for the Instagram reel extractor: a caption plus a video URL."""

    content_type = ContentType.instagram
    deferred = True

    def __init__(self, caption: str | None = CAPTION, video_url: str | None = None) -> None:
        self.caption = caption
        self.video_url = video_url

    def can_handle(self, _url: str) -> bool:
        return True

    async def start(self, _url: str) -> str:
        return "apify-run-1"

    def build(self, _items: list[dict[str, Any]]) -> ExtractedContent:
        return ExtractedContent(
            type=ContentType.instagram,
            title="A reel",
            content=self.caption,
            metadata={"owner": "someone", "video_url": self.video_url},
        )


class _AI:
    def __init__(self) -> None:
        self.summarised: list[str] = []

    async def generate_summary(self, t: str) -> str:
        self.summarised.append(t)
        return "a summary"

    async def generate_tags(self, _t: str) -> list[str]:
        return ["tag"]

    async def generate_category(self, _t: str) -> str:
        return "Shopping"

    async def generate_label(self, _t: str) -> str:
        return "a distinctive label"

    async def generate_highlights(self, t: str) -> list[str]:
        return [t[:40]]

    async def generate_embedding(self, _t: str) -> list[float]:
        return [0.0] * 1536


def _item() -> VaultItem:
    return VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.instagram,
        source_url="https://www.instagram.com/reel/x/",
        processing_status=ProcessingStatus.pending,
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    caption: str | None = CAPTION,
    video_url: str | None = "https://cdn.example/v.mp4",
    reading: video.VideoReading | None = None,
    raises: Exception | None = None,
    enabled: bool = True,
) -> tuple[VaultItem, _AI]:
    extractor = _Extractor(caption, video_url)
    ai = _AI()
    monkeypatch.setattr(ps, "get_extractor", lambda _u: extractor)
    monkeypatch.setattr(ps, "get_ai_provider", lambda: ai)
    monkeypatch.setattr(ps.video, "video_understanding_enabled", lambda: enabled)

    async def _fetch(_url: str) -> bytes:
        if raises is not None:
            raise raises
        return b"fake-mp4-bytes"

    async def _read(_data: bytes) -> video.VideoReading:
        assert reading is not None
        return reading

    monkeypatch.setattr(ps.video, "fetch_video", _fetch)
    monkeypatch.setattr(ps.video, "read_video", _read)
    return _item(), ai


async def _finalize(item: VaultItem) -> None:
    await ps.ProcessingService(_VaultRepo(item)).finalize(item.id, [{}])


# --- what a successful reading does --------------------------------------------------


async def test_the_reading_is_appended_to_the_caption_never_replacing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caption is what its author chose to write; the reading is a machine's account
    of what it saw. Both are kept, and both are labelled, so neither is mistaken for the
    other by the model or by the reader."""
    reading = video.VideoReading(
        frames_text=FRAMES_TEXT, speech="Link is in my bio", frames_read=8
    )
    item, ai = _wire(monkeypatch, reading=reading)

    await _finalize(item)

    assert item.content is not None
    assert CAPTION in item.content
    assert "GET 20% OFF" in item.content
    assert "Link is in my bio" in item.content
    # Labelled, so the model reading this knows which half was heard and which was seen.
    assert "Spoken in the video:" in item.content
    assert "Seen in the video:" in item.content
    assert item.processing_status is ProcessingStatus.completed


async def test_the_video_is_read_before_the_item_is_enriched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order is the point: a summary, tags, label and embedding drawn from the caption
    alone would describe a video nobody read."""
    reading = video.VideoReading(frames_text=FRAMES_TEXT, speech=None, frames_read=8)
    item, ai = _wire(monkeypatch, reading=reading)

    await _finalize(item)

    assert ai.summarised and "GET 20% OFF" in ai.summarised[0]


async def test_a_machine_written_body_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rendering a model's account of a video identically to words a person wrote is the
    one way this feature can lie."""
    reading = video.VideoReading(
        frames_text=FRAMES_TEXT, speech=None, frames_read=8, duration_seconds=31.5
    )
    item, _ = _wire(monkeypatch, reading=reading)

    await _finalize(item)

    assert item.item_metadata["content_source"] == "video"
    assert item.item_metadata["video_read"] is True
    assert item.item_metadata["video_frames_read"] == 8
    assert item.item_metadata["video_duration_seconds"] == 31.5


# --- links, and where they came from -------------------------------------------------


async def test_links_are_collected_from_every_source_and_credited_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A link the creator typed into the caption is a fact. The same link read off a
    blurry frame is a guess that happens to be right, and one transcribed from speech is
    a guess about a spoken domain. The UI has to be able to say which it is showing."""
    reading = video.VideoReading(
        frames_text=FRAMES_TEXT,
        speech="everything is over at spoken.example slash x, and https://audio.example/y",
        frames_read=8,
    )
    item, _ = _wire(monkeypatch, reading=reading)

    await _finalize(item)

    by_source = {link["url"]: link["source"] for link in item.item_metadata["links"]}
    assert by_source["https://example.com/guide"] == "caption"
    assert by_source["https://shop.example/sale"] == "video"
    assert by_source["https://audio.example/y"] == "speech"


async def test_a_hostile_link_burned_into_a_frame_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anyone can put text on a video. A `javascript:` URL and a userinfo link that reads
    as one host and resolves to another must not reach a page as something tappable."""
    reading = video.VideoReading(
        frames_text=(
            "On-screen text:\njavascript:alert(1)\nhttps://instagram.com@evil.example/\n"
            "https://real.example/ok"
        ),
        speech=None,
        frames_read=4,
    )
    item, _ = _wire(monkeypatch, caption=None, reading=reading)

    await _finalize(item)

    urls = [link["url"] for link in item.item_metadata["links"]]
    assert urls == ["https://real.example/ok"]


async def test_the_link_list_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded list is a JSONB column anyone with a video can grow for free."""
    monkeypatch.setattr(ps.settings, "MAX_EXTRACTED_LINKS", 3)
    reading = video.VideoReading(
        frames_text=" ".join(f"https://s{i}.example/x" for i in range(40)),
        speech=None,
        frames_read=4,
    )
    item, _ = _wire(monkeypatch, caption=None, reading=reading)

    await _finalize(item)

    assert len(item.item_metadata["links"]) == 3


# --- when it does not work -----------------------------------------------------------


async def test_an_unreadable_video_leaves_the_caption_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Oversized, expired, undecodable: all answers, not faults. The item completes on
    its caption and records why the reading is missing."""
    item, _ = _wire(
        monkeypatch, raises=video.VideoError("That video is larger than 30MB.")
    )

    await _finalize(item)

    assert item.content == CAPTION
    assert item.item_metadata["video_read"] is False
    assert item.item_metadata["video_read_error"] == "VideoError"
    assert item.processing_status is ProcessingStatus.completed


async def test_a_video_url_we_refuse_to_fetch_is_not_a_failed_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL arrives inside a scraper payload derived from a pasted link, so it is
    attacker-influencable and the SSRF guard is expected to reject some of them. That is
    the guard working, not the item breaking."""
    item, _ = _wire(monkeypatch, raises=UnsafeUrlError("host resolves to a non-public address"))

    await _finalize(item)

    assert item.content == CAPTION
    assert item.item_metadata["video_read_error"] == "UnsafeUrlError"
    assert item.processing_status is ProcessingStatus.completed


async def test_a_provider_fault_degrades_when_there_is_a_caption_to_degrade_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing an item whose caption is a perfectly good memory, in order to retry an
    enhancement, trades a working memory for a spinner."""
    item, _ = _wire(monkeypatch, raises=video.VideoFailed("provider is down"))

    await _finalize(item)

    assert item.content == CAPTION
    assert item.item_metadata["video_read_error"] == "VideoFailed"
    assert item.processing_status is ProcessingStatus.completed


async def test_a_provider_fault_is_retried_when_the_video_was_the_only_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is nothing to degrade *to*. Enriching an empty item is how a memory ends up
    summarising its own URL, so this is raised and Celery retries it."""
    item, _ = _wire(monkeypatch, caption=None, raises=video.VideoFailed("provider is down"))

    with pytest.raises(video.VideoFailed):
        await _finalize(item)

    assert item.processing_status is ProcessingStatus.failed


async def test_an_item_with_no_video_url_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A static post has no video. Nothing should be fetched and nothing paid for."""
    item, _ = _wire(monkeypatch, video_url=None)

    await _finalize(item)

    assert item.content == CAPTION
    assert "video_read" not in item.item_metadata
    assert "links" not in item.item_metadata


async def test_the_switch_being_off_reads_no_videos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, _ = _wire(monkeypatch, enabled=False)

    await _finalize(item)

    assert item.content == CAPTION
    assert "video_read" not in item.item_metadata


# --- the download itself -------------------------------------------------------------


def _client_factory(handler: Any) -> Any:
    class _Client(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            kwargs.pop("follow_redirects", None)
            super().__init__(
                transport=httpx.MockTransport(handler), follow_redirects=False, **kwargs
            )

    return _Client


async def test_the_size_cap_is_enforced_while_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not from Content-Length, which the CDN writes. A lie there is a memory-exhaustion
    bug, and reading the body to find out is the only honest check."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (3 * 1024 * 1024))

    monkeypatch.setattr(video.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(video.settings, "MAX_VIDEO_MB", 1)
    monkeypatch.setattr(video, "assert_safe_url", lambda _u: None)

    with pytest.raises(video.VideoError, match="larger than 1MB"):
        await video.fetch_video("https://cdn.example/v.mp4")


async def test_every_redirect_hop_is_revalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An allowed external URL is free to redirect to an internal one, so validating the
    first hop and trusting the chain is the same as not validating at all."""
    checked: list[str] = []

    def guard(url: str) -> None:
        checked.append(url)
        if "127.0.0.1" in url or "169.254" in url:
            raise UnsafeUrlError("host resolves to a non-public address")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/x"})
        return httpx.Response(200, content=b"never reached")

    monkeypatch.setattr(video.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(video, "assert_safe_url", guard)

    with pytest.raises(UnsafeUrlError):
        await video.fetch_video("https://cdn.example/v.mp4")

    assert checked == ["https://cdn.example/v.mp4", "http://127.0.0.1:8000/x"]


async def test_an_expired_cdn_link_is_an_answer_not_a_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scraper's media URL is signed and dies within hours, so a 403 here is the normal
    end of a video that was not read in time -- not something to retry into."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"")

    monkeypatch.setattr(video.httpx, "AsyncClient", _client_factory(handler))
    monkeypatch.setattr(video, "assert_safe_url", lambda _u: None)

    with pytest.raises(video.VideoError, match="may have expired"):
        await video.fetch_video("https://cdn.example/v.mp4")


# --- the shape of what comes back ----------------------------------------------------


class _Completion:
    def __init__(self, text: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


async def test_markdown_headings_are_flattened(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model is told to write plain headings and returns `### On-screen text:` often
    enough to matter -- a prompt is a request, not a constraint. The body is stored as
    `content` and rendered as plain text, so the markers land literally on the page."""

    async def _call(_frames: list[bytes]) -> Any:
        return _Completion(
            "### On-screen text:\nGET 20% OFF\n\n"
            "## Links:\nshop.example/sale\n\n"
            "**What happens:**\nSomeone unboxes a keyboard."
        )

    monkeypatch.setattr(video, "_call_provider", _call)
    monkeypatch.setattr(video, "video_understanding_enabled", lambda: True)

    text = await video.read_frames([b"jpeg"])

    assert text.startswith("On-screen text:")
    assert "\nLinks:" in text
    assert "\nWhat happens:" in text
    assert "#" not in text and "**" not in text


async def test_transcribed_frame_text_is_never_reformatted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The critical half of that fix. The on-screen section is a verbatim transcription,
    so a frame that genuinely reads `**SALE**` or `#1 BEST SELLER` must survive intact --
    a cosmetic cleanup that edits quoted material is a worse bug than the cosmetic
    problem it set out to solve."""

    async def _call(_frames: list[bytes]) -> Any:
        return _Completion(
            "### On-screen text:\n**SALE**\n#1 BEST SELLER\n### 50% off\n\n"
            "Links:\nnone\n\nWhat happens:\nA product spins."
        )

    monkeypatch.setattr(video, "_call_provider", _call)
    monkeypatch.setattr(video, "video_understanding_enabled", lambda: True)

    text = await video.read_frames([b"jpeg"])

    assert "**SALE**" in text
    assert "#1 BEST SELLER" in text
    # A heading-looking line that is not one of ours is content, not syntax.
    assert "### 50% off" in text
    # Ours is still repaired.
    assert text.startswith("On-screen text:")


async def test_an_empty_reading_is_unreadable_not_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _call(_frames: list[bytes]) -> Any:
        return _Completion("   ")

    monkeypatch.setattr(video, "_call_provider", _call)
    monkeypatch.setattr(video, "video_understanding_enabled", lambda: True)

    with pytest.raises(video.VideoError):
        await video.read_frames([b"jpeg"])


async def test_a_provider_fault_answers_in_our_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Theirs can name the account it rejected."""

    async def _call(_frames: list[bytes]) -> Any:
        raise RuntimeError("401 Incorrect API key sk-proj-abc123 provided")

    monkeypatch.setattr(video, "_call_provider", _call)
    monkeypatch.setattr(video, "video_understanding_enabled", lambda: True)

    with pytest.raises(video.VideoFailed) as exc:
        await video.read_frames([b"jpeg"])
    assert "sk-proj" not in str(exc.value)
