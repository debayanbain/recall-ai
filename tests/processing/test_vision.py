"""Reading an uploaded image: what gets sent, what comes back, and what a failure means.

Offline -- the provider and the object store are fakes. What is pinned is the boundary:
the image *bytes* go to the model and the presigned URL never does, an unreadable image is
`skipped` rather than retried three times, and the stored description is marked as
machine-written so the reader never presents it as the user's own words.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services import vision
from app.services.processing_service import ProcessingService
from app.storage import StorageError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class _Repo:
    def __init__(self, item: VaultItem) -> None:
        self.item = item
        self.chunks: list[dict[str, Any]] = []

    async def get_unscoped(self, _item_id: uuid.UUID) -> VaultItem:
        return self.item

    async def add(self, item: VaultItem) -> VaultItem:
        return item

    async def upsert_chunk(self, **kwargs: Any) -> None:
        self.chunks.append(kwargs)


class _Storage:
    def __init__(self, data: bytes = PNG, fail: bool = False) -> None:
        self.data = data
        self.fail = fail
        self.downloaded: list[str] = []

    async def download(self, key: str) -> bytes:
        if self.fail:
            raise StorageError("We couldn't read that file back from storage.")
        self.downloaded.append(key)
        return self.data


class _AI:
    async def generate_summary(self, _text: str) -> str:
        return "A receipt from a hardware shop."

    async def generate_tags(self, _text: str) -> list[str]:
        return ["receipts"]

    async def generate_label(self, _text: str) -> str:
        return "Hardware shop receipt"

    async def generate_highlights(self, _text: str) -> list[str]:
        return []

    async def generate_category(self, _text: str) -> str:
        return "Finance"

    async def generate_embedding(self, _text: str) -> list[float]:
        return [0.0] * 8


def _image_item() -> VaultItem:
    return VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.image,
        source_url=None,
        title="receipt.png",
        file_name="receipt.png",
        mime_type="image/png",
        file_size=len(PNG),
        storage_key="users/u/i/abc.png",
        processing_status=ProcessingStatus.pending,
    )


def _service(item: VaultItem, storage: _Storage) -> ProcessingService:
    service = ProcessingService(_Repo(item), None, storage)  # type: ignore[arg-type]
    service.ai = _AI()  # type: ignore[assignment]
    return service


# --- what is sent --------------------------------------------------------------------


async def test_the_bytes_go_to_the_model_not_a_signed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A presigned URL is a bearer credential; handing one to a third party would make a
    private object externally fetchable for the length of its TTL."""
    sent: list[str] = []

    async def _call(data_url: str) -> Any:
        sent.append(data_url)
        return _Completion("A receipt for two litres of paint.")

    monkeypatch.setattr(vision, "_call_provider", _call)
    monkeypatch.setattr(vision.settings, "OPENAI_API_KEY", "sk-test")

    await vision.describe_image(PNG, "image/png")

    assert sent[0].startswith("data:image/png;base64,")
    assert "http" not in sent[0][:64]


class _Completion:
    def __init__(self, text: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


# --- what a refusal means ------------------------------------------------------------


@pytest.mark.parametrize(
    ("mime", "size"),
    [("image/heic", 1000), ("application/pdf", 1000), (None, 1000), ("image/png", 0)],
)
def test_unreadable_images_are_never_queued(
    mime: str | None, size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vision.settings, "OPENAI_API_KEY", "sk-test")
    assert vision.can_describe(mime, size) is False


def test_oversized_images_are_never_queued(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vision.settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(vision.settings, "MAX_VISION_IMAGE_MB", 1)
    assert vision.can_describe("image/png", 2 * 1024 * 1024) is False


async def test_a_provider_fault_answers_in_our_own_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _call(_data_url: str) -> Any:
        raise RuntimeError("401 Incorrect API key sk-proj-abc123 provided")

    monkeypatch.setattr(vision, "_call_provider", _call)
    monkeypatch.setattr(vision.settings, "OPENAI_API_KEY", "sk-test")

    with pytest.raises(vision.VisionFailed) as exc:
        await vision.describe_image(PNG, "image/png")
    assert "sk-proj" not in str(exc.value)


async def test_an_empty_description_is_unreadable_not_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _call(_data_url: str) -> Any:
        return _Completion("   ")

    monkeypatch.setattr(vision, "_call_provider", _call)
    monkeypatch.setattr(vision.settings, "OPENAI_API_KEY", "sk-test")

    with pytest.raises(vision.VisionError):
        await vision.describe_image(PNG, "image/png")


# --- what the pipeline does with it --------------------------------------------------


async def test_a_described_image_becomes_a_searchable_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _describe(_data: bytes, _mime: str | None) -> str:
        return "A paper receipt on a wooden table.\nText in image: TOTAL 24.50"

    monkeypatch.setattr(vision, "vision_enabled", lambda: True)
    monkeypatch.setattr(vision, "describe_image", _describe)

    item = _image_item()
    storage = _Storage()
    service = _service(item, storage)

    await service.process(item.id)

    assert storage.downloaded == ["users/u/i/abc.png"]
    assert item.content is not None and "TOTAL 24.50" in item.content
    # Marked as machine-written: presenting a model's account of a picture as the user's
    # own words is the one way this feature can lie.
    assert item.item_metadata["content_source"] == "vision"
    assert item.processing_status is ProcessingStatus.completed
    assert item.ai_tags == ["receipts"] and item.ai_category == "Finance"


async def test_an_unreadable_image_is_skipped_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`skipped` stops Celery paying for three more readings of the same picture."""

    async def _describe(_data: bytes, _mime: str | None) -> str:
        raise vision.VisionError("Nothing readable was found in that image.")

    monkeypatch.setattr(vision, "vision_enabled", lambda: True)
    monkeypatch.setattr(vision, "describe_image", _describe)

    item = _image_item()
    service = _service(item, _Storage())

    await service.process(item.id)

    assert item.processing_status is ProcessingStatus.skipped
    assert item.processing_error is None


async def test_an_unreachable_bucket_is_a_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vision, "vision_enabled", lambda: True)

    item = _image_item()
    service = _service(item, _Storage(fail=True))

    with pytest.raises(StorageError):
        await service.process(item.id)
    assert item.processing_status is ProcessingStatus.failed
    # Scrubbed, and it names the class so an empty provider message still says something.
    assert item.processing_error and item.processing_error.startswith("StorageError")


async def test_no_vision_key_leaves_the_image_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vision, "vision_enabled", lambda: False)

    item = _image_item()
    service = _service(item, _Storage())

    await service.process(item.id)
    assert item.processing_status is ProcessingStatus.skipped
