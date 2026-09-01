"""Nothing captured is allowed to sit in limbo unnoticed.

Three mechanisms, tested together because they only make sense as one story: the stored
failure reason is scrubbed before anyone can read it, a stranded row is rescued by the
sweeper rather than waiting for a person to notice, and the owner can re-drive an item
that finished badly.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.core.errors import redact_text, safe_error_text
from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services import vault_service as vs
from app.services.vault_service import ItemNotFound, ReprocessError


def _item(
    status: ProcessingStatus,
    *,
    age_seconds: int = 600,
    retries: int = 0,
    kind: ContentType = ContentType.article,
    storage_key: str | None = None,
) -> VaultItem:
    item = VaultItem(
        user_id=uuid.uuid4(),
        type=kind,
        source_url="https://example.com/a",
        storage_key=storage_key,
        processing_status=status,
        retry_count=retries,
    )
    item.updated_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return item


class _Repo:
    def __init__(self, item: VaultItem | None = None) -> None:
        self.item = item
        self.added: list[VaultItem] = []

    async def get(self, _item_id: uuid.UUID, _user_id: uuid.UUID) -> VaultItem | None:
        return self.item

    async def add(self, item: VaultItem) -> VaultItem:
        self.added.append(item)
        return item


@pytest.fixture(autouse=True)
def _queue(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    queued: list[uuid.UUID] = []

    async def _capture(item_id: uuid.UUID) -> None:
        queued.append(item_id)

    monkeypatch.setattr(vs, "enqueue_process_item", _capture)
    return queued


# --- what a failure is allowed to say ------------------------------------------------


def test_credentials_are_stripped_from_a_stored_failure() -> None:
    """`processing_error` is read back by its owner and pasted into issues.

    An httpx error carries the whole request URL, and Apify's carry a live token in the
    query string. Scrubbed on the way *in*: a redaction that only happens at render time
    is one a second render path forgets.
    """
    text = redact_text(
        "GET https://api.apify.com/v2/acts/x/runs?token=apify_api_ABCD1234EFGH failed"
    )
    assert "apify_api_ABCD1234EFGH" not in text
    assert "token=<redacted>" in text
    # The part a person can act on survives.
    assert "api.apify.com" in text and "failed" in text


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-AAAABBBBCCCCDDDD",
        "Bearer eyJhbGciOiJIUzI1NiJ9",
        "AIzaSyA1234567890abcdef",
        "123456789:AAF-abcdefghijklmnopqrstuvwxyz0123456",
        "xoxb-123456789-abcdefghijkl",
    ],
)
def test_credential_shapes_are_stripped_outside_query_strings(secret: str) -> None:
    assert secret not in redact_text(f"upstream said: {secret} rejected")


def test_an_empty_message_still_names_the_failure() -> None:
    """A bare TimeoutError reads as nothing at all without its class name."""
    assert safe_error_text(TimeoutError()) == "TimeoutError"


def test_a_stored_failure_is_capped() -> None:
    assert len(safe_error_text(RuntimeError("x" * 5000))) <= 500


# --- re-driving an item its owner can see --------------------------------------------


async def test_a_failed_item_can_be_re_driven(_queue: list[uuid.UUID]) -> None:
    item = _item(ProcessingStatus.failed, retries=3)
    item.processing_error = "RuntimeError: upstream timed out"
    repo = _Repo(item)

    result = await vs.VaultService(repo, None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]

    assert result.processing_status is ProcessingStatus.pending
    assert result.processing_error is None
    # A manual retry is a new attempt at the job, not a continuation of the exhausted one.
    assert result.retry_count == 0
    assert _queue == [item.id]


async def test_a_skipped_item_can_be_re_driven(_queue: list[uuid.UUID]) -> None:
    """`skipped` describes the deployment as much as the file.

    An image saved before a vision key existed becomes readable the moment one does, and
    without this there is no way to ask for that.
    """
    item = _item(ProcessingStatus.skipped)
    result = await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert result.processing_status is ProcessingStatus.pending
    assert _queue == [item.id]


@pytest.mark.parametrize(
    "status", [ProcessingStatus.pending, ProcessingStatus.processing]
)
async def test_work_already_in_flight_is_refused(
    status: ProcessingStatus, _queue: list[uuid.UUID]
) -> None:
    item = _item(status)
    with pytest.raises(ReprocessError, match="already being processed"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []


async def test_a_finished_item_is_refused(_queue: list[uuid.UUID]) -> None:
    """Re-running a good item would spend the whole AI bill to replace it with itself."""
    item = _item(ProcessingStatus.completed)
    with pytest.raises(ReprocessError, match="finished already"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []


async def test_a_double_click_hits_the_cooldown(_queue: list[uuid.UUID]) -> None:
    item = _item(ProcessingStatus.failed, age_seconds=2)
    with pytest.raises(ReprocessError, match="more seconds"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []


async def test_someone_elses_item_is_not_found(_queue: list[uuid.UUID]) -> None:
    """The repository scopes on user_id, so "not yours" and "missing" are one answer."""
    with pytest.raises(ItemNotFound):
        await vs.VaultService(_Repo(None), None).reprocess(uuid.uuid4(), uuid.uuid4())  # type: ignore[arg-type]
    assert _queue == []


# --- a voice note is the one thing worth re-running after it succeeded ----------------


def _voice(status: ProcessingStatus = ProcessingStatus.completed) -> VaultItem:
    item = _item(status, kind=ContentType.voice, storage_key="users/u/i/a.webm")
    item.content = "今天視頻就拍到這裡啦"  # a Bengali clip heard as Chinese
    item.item_metadata = {"source": "voice"}
    return item


async def test_a_finished_voice_note_can_be_re_transcribed(
    _queue: list[uuid.UUID],
) -> None:
    """Its transcript is the one output that can be confidently, fluently wrong."""
    item = _voice()

    result = await vs.VaultService(_Repo(item), None).reprocess(  # type: ignore[arg-type]
        item.id, item.user_id, "bn"
    )

    # Cleared so the worker re-reads the audio instead of re-enriching the wrong words.
    assert result.content is None
    assert result.processing_status is ProcessingStatus.pending
    # Pinned, so the re-run is a different attempt rather than the same coin flip.
    assert result.item_metadata["transcribe_language"] == "bn"
    assert _queue == [item.id]


async def test_a_junk_language_falls_back_to_detection(_queue: list[uuid.UUID]) -> None:
    """A client bug must not stand between someone and a fixed transcript."""
    item = _voice()
    result = await vs.VaultService(_Repo(item), None).reprocess(  # type: ignore[arg-type]
        item.id, item.user_id, "klingon"
    )
    assert "transcribe_language" not in result.item_metadata
    assert result.content is None


async def test_a_voice_note_with_no_audio_kept_is_still_refused(
    _queue: list[uuid.UUID],
) -> None:
    """Clearing the words with nothing to re-read them from destroys the memory."""
    item = _item(ProcessingStatus.completed, kind=ContentType.voice, storage_key=None)
    item.content = "the words are all that is left"

    with pytest.raises(ReprocessError, match="finished already"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert item.content == "the words are all that is left"
    assert _queue == []


async def test_a_finished_article_is_still_refused(_queue: list[uuid.UUID]) -> None:
    """Re-running a good article spends the whole pipeline to reproduce itself."""
    item = _item(ProcessingStatus.completed)
    with pytest.raises(ReprocessError, match="finished already"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []


# --- the sweep -----------------------------------------------------------------------


class _SweepRepo:
    def __init__(self, pending: list[VaultItem], processing: list[VaultItem]) -> None:
        self.by_status = {
            ProcessingStatus.pending: pending,
            ProcessingStatus.processing: processing,
        }

    async def list_stranded(
        self, status: ProcessingStatus, _minutes: int, limit: int = 100
    ) -> list[VaultItem]:
        return self.by_status.get(status, [])[:limit]


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _install_sweep(
    monkeypatch: pytest.MonkeyPatch, repo: _SweepRepo, queue: list[uuid.UUID]
) -> _Session:
    from contextlib import asynccontextmanager

    from app.queue import tasks

    session = _Session()

    @asynccontextmanager
    async def _task_session() -> Any:
        yield session

    async def _enqueue(item_id: uuid.UUID) -> None:
        queue.append(item_id)

    monkeypatch.setattr(tasks, "task_session", _task_session)
    monkeypatch.setattr(tasks, "VaultRepository", lambda _s: repo)
    monkeypatch.setattr(tasks, "enqueue_process_item", _enqueue)
    return session


async def test_an_item_that_was_never_queued_is_re_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The usual cause is `vault_enqueue_failed`: the row committed, Redis was down."""
    from app.queue.tasks import _sweep_stranded_items

    stranded = _item(ProcessingStatus.pending)
    queue: list[uuid.UUID] = []
    session = _install_sweep(monkeypatch, _SweepRepo([stranded], []), queue)

    result = await _sweep_stranded_items()

    assert result == {"requeued": 1, "failed": 0}
    assert queue == [stranded.id]
    # Still pending: it has not failed, it just had not started.
    assert stranded.processing_status is ProcessingStatus.pending
    assert stranded.retry_count == 1
    assert session.commits == 1


async def test_endless_requeues_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An item that kills the worker on load must not be re-driven forever."""
    from app.core.config import settings
    from app.queue.tasks import _sweep_stranded_items

    stranded = _item(ProcessingStatus.pending, retries=settings.MAX_SWEEP_REQUEUES)
    queue: list[uuid.UUID] = []
    _install_sweep(monkeypatch, _SweepRepo([stranded], []), queue)

    result = await _sweep_stranded_items()

    assert result == {"requeued": 0, "failed": 1}
    assert queue == []
    assert stranded.processing_status is ProcessingStatus.failed
    assert stranded.processing_error and "queue" in stranded.processing_error


async def test_a_worker_that_died_mid_job_stops_spinning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`acks_late` returns the message; nothing resets the row. This does."""
    from app.queue.tasks import _sweep_stranded_items

    stuck = _item(ProcessingStatus.processing)
    queue: list[uuid.UUID] = []
    _install_sweep(monkeypatch, _SweepRepo([], [stuck]), queue)

    result = await _sweep_stranded_items()

    assert result == {"requeued": 0, "failed": 1}
    assert stuck.processing_status is ProcessingStatus.failed
    # Worded for the person who will read it, and it says nothing was lost -- because
    # the row and any stored file are both still there.
    assert stuck.processing_error and "try again" in stuck.processing_error.lower()
    # Not re-queued: there is no safe way to know how far the half-finished run got.
    assert queue == []


async def test_a_quiet_sweep_reports_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.queue.tasks import _sweep_stranded_items

    _install_sweep(monkeypatch, _SweepRepo([], []), [])
    assert await _sweep_stranded_items() == {"requeued": 0, "failed": 0}


# --- re-reading a video a memory was saved without ------------------------------------


def _reel(
    *,
    status: ProcessingStatus = ProcessingStatus.completed,
    video_url: str | None = "https://cdn.example/v.mp4",
    video_read: bool | None = None,
    source_url: str | None = "https://www.instagram.com/reel/x/",
) -> VaultItem:
    item = _item(status, kind=ContentType.instagram)
    item.source_url = source_url
    item.content = "New drop is live."
    metadata: dict[str, Any] = {"owner": "someone"}
    if video_url is not None:
        metadata["video_url"] = video_url
    if video_read is not None:
        metadata["video_read"] = video_read
    item.item_metadata = metadata
    return item


@pytest.fixture
def _video_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vs.settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(vs.settings, "VIDEO_UNDERSTANDING_ENABLED", True)


async def test_a_reel_saved_before_video_reading_can_be_re_read(
    _queue: list[uuid.UUID], _video_on: None
) -> None:
    """The case this exists for: a completed reel holding a caption and nothing of what
    was on screen or said. `completed` is what would otherwise make that permanent."""
    item = _reel()

    result = await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]

    assert result.processing_status is ProcessingStatus.pending
    assert result.retry_count == 0
    assert _queue == [item.id]


async def test_the_caption_is_not_cleared_on_the_way(
    _queue: list[uuid.UUID], _video_on: None
) -> None:
    """Unlike the voice path. `_apply` overwrites `content` from the fresh scrape before
    `_read_video` runs, so clearing here would only widen the window in which the row
    holds neither the old body nor the new."""
    item = _reel()

    result = await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]

    assert result.content == "New drop is live."
    assert result.item_metadata["video_url"] == "https://cdn.example/v.mp4"


async def test_a_failed_video_read_is_re_drivable(
    _queue: list[uuid.UUID], _video_on: None
) -> None:
    """`video_read: false` is a read that was attempted and did not land -- a provider
    outage, an expired link. That is exactly the case worth offering again."""
    item = _reel(video_read=False)

    result = await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]

    assert result.processing_status is ProcessingStatus.pending
    assert _queue == [item.id]


async def test_a_video_already_read_is_refused(
    _queue: list[uuid.UUID], _video_on: None
) -> None:
    """Same rule as every other finished item: re-running it spends a scraper run and the
    whole pipeline to replace a result with itself."""
    item = _reel(video_read=True)

    with pytest.raises(ReprocessError, match="finished already"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []


async def test_an_item_with_no_video_is_refused(
    _queue: list[uuid.UUID], _video_on: None
) -> None:
    """A static post carries no video. There is nothing a re-read could add."""
    item = _reel(video_url=None)

    with pytest.raises(ReprocessError, match="finished already"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []


async def test_an_item_with_no_source_url_is_refused(
    _queue: list[uuid.UUID], _video_on: None
) -> None:
    """The stored `video_url` is a signed CDN link that dies within hours, so the only
    route to a playable one is re-running the extractor. With no URL to re-scrape there
    is nothing to re-read, and offering it would spend a queue trip to fail."""
    item = _reel(source_url=None)

    with pytest.raises(ReprocessError, match="finished already"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []


async def test_the_capability_being_off_refuses_the_re_read(
    _queue: list[uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without it the whole round trip -- a paid scraper run included -- reproduces the
    result the item already has."""
    monkeypatch.setattr(vs.settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(vs.settings, "VIDEO_UNDERSTANDING_ENABLED", False)
    item = _reel()

    with pytest.raises(ReprocessError, match="finished already"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []


async def test_a_reel_still_being_processed_is_refused(
    _queue: list[uuid.UUID], _video_on: None
) -> None:
    """Reaching here means a double click or a script; the UI disables the button."""
    item = _reel(status=ProcessingStatus.processing)

    with pytest.raises(ReprocessError, match="already being processed"):
        await vs.VaultService(_Repo(item), None).reprocess(item.id, item.user_id)  # type: ignore[arg-type]
    assert _queue == []
