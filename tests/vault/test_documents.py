"""Upload validation, storage-key construction, and the B2 upload path.

Offline: the object store is a fake, the repository is a fake. What is pinned here is the
security boundary -- what may be uploaded, what it is renamed to, where it is put, and
who can get it back.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services import documents
from app.services import vault_service as vs
from app.services.documents import DocumentError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
DOCX = b"PK\x03\x04" + b"\x00" * 64
TEXT = b"the quick brown fox\n"


class _Repo:
    def __init__(self, existing: VaultItem | None = None) -> None:
        self.added: list[VaultItem] = []
        self.existing = existing
        self.deleted: list[VaultItem] = []

    async def add(self, item: VaultItem) -> VaultItem:
        self.added.append(item)
        return item

    async def get(self, _item_id: uuid.UUID, _user_id: uuid.UUID) -> VaultItem | None:
        return self.existing

    async def delete(self, item: VaultItem) -> None:
        self.deleted.append(item)


class _Storage:
    def __init__(self) -> None:
        self.uploaded: list[tuple[str, bytes, str]] = []
        self.deleted: list[str] = []
        self.signed: list[dict[str, Any]] = []

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        self.uploaded.append((key, data, content_type))

    async def presigned_get(
        self, key: str, *, filename: str, content_type: str, expires: int
    ) -> str:
        self.signed.append(
            {"key": key, "filename": filename, "content_type": content_type, "expires": expires}
        )
        return f"https://s3.example.invalid/{key}?X-Amz-Signature=deadbeef"

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(_: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(vs, "enqueue_process_item", _noop)


# --- what may be uploaded ------------------------------------------------------------


def test_type_comes_from_the_bytes_not_the_extension() -> None:
    """`report.pdf` can be anything -- the signature is the only claim worth trusting."""
    with pytest.raises(DocumentError, match="real .pdf"):
        documents.inspect(b"MZ\x90\x00 this is a windows executable", "report.pdf")


def test_executable_content_type_is_irrelevant() -> None:
    """The browser's Content-Type is never consulted; a PNG signature is."""
    doc = documents.inspect(PNG, "screenshot.png")
    assert doc.mime_type == "image/png"


def test_disallowed_types_are_refused() -> None:
    # SVG and HTML are executable in a browser; archives and binaries have no place here.
    for name, data in [
        ("payload.svg", b"<svg onload=alert(1)>"),
        ("page.html", b"<script>alert(1)</script>"),
        ("tool.exe", b"MZ\x90\x00"),
        ("archive.zip", b"PK\x03\x04"),
    ]:
        with pytest.raises(DocumentError, match="isn't supported"):
            documents.inspect(data, name)


def test_a_file_with_no_extension_is_refused() -> None:
    with pytest.raises(DocumentError, match="isn't supported"):
        documents.inspect(PNG, "screenshot")


def test_empty_and_oversize_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    with pytest.raises(DocumentError, match="empty"):
        documents.inspect(b"", "a.png")

    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    with pytest.raises(DocumentError, match="limit is 1MB"):
        documents.inspect(PNG + b"\x00" * (1024 * 1024), "big.png")


def test_binary_wearing_a_text_extension_is_refused() -> None:
    with pytest.raises(DocumentError, match="doesn't look like text"):
        documents.inspect(b"\x00\x01\x02binary", "notes.txt")


def test_riff_that_is_not_webp_is_refused() -> None:
    """RIFF also covers WAV and AVI; the form type is what makes it an image."""
    with pytest.raises(DocumentError, match="real .webp"):
        documents.inspect(b"RIFF\x00\x00\x00\x00AVI ", "clip.webp")


# --- what it gets called -------------------------------------------------------------


def test_display_name_drops_path_components() -> None:
    assert documents.safe_display_name("../../../etc/passwd.txt", "txt") == "passwd.txt"
    assert documents.safe_display_name(r"C:\Users\me\notes.txt", "txt") == "notes.txt"


def test_display_name_strips_dangerous_characters() -> None:
    cleaned = documents.safe_display_name('inv"oice;<script>.pdf', "pdf")
    assert '"' not in cleaned and "<" not in cleaned and ";" not in cleaned
    assert cleaned.endswith(".pdf")


def test_display_name_always_carries_the_verified_extension() -> None:
    assert documents.safe_display_name("", "png").endswith(".png")
    assert documents.safe_display_name("photo", "png") == "photo.png"


def test_display_name_is_length_capped() -> None:
    assert len(documents.safe_display_name("a" * 500 + ".pdf", "pdf")) <= 120


# --- where it is put -----------------------------------------------------------------


def test_object_key_is_entirely_server_generated() -> None:
    user_id, item_id = uuid.uuid4(), uuid.uuid4()
    key = documents.object_key(user_id, item_id, "pdf")

    assert key.startswith(f"users/{user_id}/{item_id}/")
    assert key.endswith(".pdf")
    # Nothing the uploader controls appears in the key, so `../` in a filename is only
    # ever a character a display name loses -- never a path.
    assert ".." not in key
    assert key != documents.object_key(user_id, item_id, "pdf")  # randomised per upload


# --- saving --------------------------------------------------------------------------


async def test_image_is_queued_for_the_vision_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """An image carries no text but is not therefore unreadable.

    The worker hands it to a vision model and the description becomes the body that gets
    summarised, tagged and embedded -- which is the difference between a screenshot you
    can find by asking about it and a file you have to remember the name of.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    repo, storage = _Repo(), _Storage()
    user_id = uuid.uuid4()

    item = await vs.VaultService(repo, storage).save_document(user_id, PNG, "holiday.png")

    assert item.type is ContentType.image
    assert item.processing_status is ProcessingStatus.pending
    assert item.file_name == "holiday.png"
    assert item.mime_type == "image/png"
    assert item.file_size == len(PNG)
    key, data, content_type = storage.uploaded[0]
    assert key.startswith(f"users/{user_id}/{item.id}/")
    assert data == PNG
    assert content_type == "image/png"


async def test_image_is_skipped_when_nothing_can_read_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decided at save time, not in the worker: a round trip to be skipped buys nothing."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    repo, storage = _Repo(), _Storage()

    item = await vs.VaultService(repo, storage).save_document(uuid.uuid4(), PNG, "a.png")

    assert item.processing_status is ProcessingStatus.skipped
    # Still stored and downloadable -- unreadable is not the same as unwanted.
    assert item.storage_key is not None


async def test_text_file_is_stored_and_indexed() -> None:
    repo, storage = _Repo(), _Storage()
    item = await vs.VaultService(repo, storage).save_document(uuid.uuid4(), TEXT, "notes.txt")

    assert item.type is ContentType.document
    assert item.content == "the quick brown fox"
    assert item.processing_status is ProcessingStatus.pending  # goes to the AI pipeline
    assert item.storage_key is not None


async def test_docx_is_kept_even_though_its_text_cannot_be_read() -> None:
    repo, storage = _Repo(), _Storage()
    item = await vs.VaultService(repo, storage).save_document(uuid.uuid4(), DOCX, "cv.docx")

    assert item.type is ContentType.document
    assert item.content is None
    assert item.processing_status is ProcessingStatus.skipped
    assert storage.uploaded


async def test_upload_happens_before_the_row_is_inserted() -> None:
    """A failed upload must leave no vault item pointing at a file that is not there."""

    class _Failing(_Storage):
        async def upload(self, key: str, data: bytes, content_type: str) -> None:
            raise RuntimeError("bucket is down")

    repo = _Repo()
    with pytest.raises(RuntimeError):
        await vs.VaultService(repo, _Failing()).save_document(uuid.uuid4(), PNG, "a.png")
    assert repo.added == []


async def test_binary_upload_is_refused_when_no_bucket_is_configured() -> None:
    """Accepting it would silently discard the user's file."""
    repo = _Repo()
    with pytest.raises(DocumentError, match="storage isn't configured"):
        await vs.VaultService(repo, None).save_document(uuid.uuid4(), JPEG, "photo.jpg")
    assert repo.added == []


async def test_text_upload_still_works_without_a_bucket() -> None:
    """The text is the memory; storage is an addition, not a prerequisite."""
    repo = _Repo()
    item = await vs.VaultService(repo, None).save_document(uuid.uuid4(), TEXT, "notes.txt")
    assert item.storage_key is None
    assert item.content == "the quick brown fox"


# --- getting it back -----------------------------------------------------------------


async def test_download_link_is_short_lived_and_forces_the_stored_name() -> None:
    from app.core.config import settings

    stored = VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.image,
        storage_key="users/u/i/abc.png",
        file_name="holiday.png",
        mime_type="image/png",
    )
    storage = _Storage()
    result = await vs.VaultService(_Repo(existing=stored), storage).file_link(
        stored.id, stored.user_id
    )

    assert result is not None
    url, item = result
    assert url.startswith("https://")
    assert item is stored
    assert storage.signed[0]["expires"] == settings.DOWNLOAD_LINK_TTL_SECONDS
    assert storage.signed[0]["filename"] == "holiday.png"


async def test_no_link_for_an_item_that_is_not_yours() -> None:
    """The repository scopes on user_id and returns None, which the route turns into 404."""
    storage = _Storage()
    result = await vs.VaultService(_Repo(existing=None), storage).file_link(
        uuid.uuid4(), uuid.uuid4()
    )
    assert result is None
    assert storage.signed == []


async def test_no_link_for_an_item_without_a_file() -> None:
    note = VaultItem(user_id=uuid.uuid4(), type=ContentType.note, title="just a note")
    result = await vs.VaultService(_Repo(existing=note), _Storage()).file_link(
        note.id, note.user_id
    )
    assert result is None


async def test_deleting_an_item_removes_its_object() -> None:
    stored = VaultItem(
        user_id=uuid.uuid4(),
        type=ContentType.image,
        storage_key="users/u/i/abc.png",
        file_name="holiday.png",
    )
    storage = _Storage()
    repo = _Repo(existing=stored)

    assert await vs.VaultService(repo, storage).delete(stored.id, stored.user_id) is True
    assert repo.deleted == [stored]
    assert storage.deleted == ["users/u/i/abc.png"]


# --- the download header --------------------------------------------------------------


def test_content_disposition_cannot_be_broken_out_of() -> None:
    """The stored name lands in a response header, so quotes and semicolons must not."""
    from app.storage.b2 import _content_disposition

    header = _content_disposition('evil";filename="pwned.exe')

    assert header.startswith('attachment; filename="')
    quoted = header.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in quoted and ";" not in quoted and "\\" not in quoted
    # One directive, ours. A second `filename=` would be the attacker's.
    assert header.count('filename="') == 1


def test_content_disposition_is_always_an_attachment() -> None:
    """Never `inline`: a file the origin renders is stored XSS."""
    from app.storage.b2 import _content_disposition

    assert _content_disposition("notes.pdf").startswith("attachment;")


def test_content_disposition_handles_non_ascii_names() -> None:
    from app.storage.b2 import _content_disposition

    header = _content_disposition("рецепт.pdf")
    assert 'filename="download.pdf"' in header  # ASCII fallback stays usable
    assert "filename*=UTF-8''" in header  # and the real name is percent-encoded


# --- deletion actually deletes ---------------------------------------------------------


async def test_delete_purges_every_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain DELETE on a "Keep all versions" bucket only hides the object.

    Verified against the live bucket: one delete left the bytes in place behind a delete
    marker. Deletion has to mean deletion regardless of the bucket's lifecycle setting.
    """
    from app.storage import b2

    deleted: list[tuple[str, str | None]] = []

    class _Client:
        def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "Versions": [
                    {"Key": "users/u/i/a.png", "VersionId": "v1"},
                    {"Key": "users/u/i/a.png", "VersionId": "v2"},
                    # A neighbour that merely shares the prefix must be left alone.
                    {"Key": "users/u/i/a.png.bak", "VersionId": "v9"},
                ],
                "DeleteMarkers": [{"Key": "users/u/i/a.png", "VersionId": "m1"}],
                "IsTruncated": False,
            }

        def delete_object(self, **kwargs: Any) -> None:
            deleted.append((kwargs["Key"], kwargs.get("VersionId")))

    monkeypatch.setattr(b2, "_client", lambda: _Client())
    await b2.B2Storage().delete("users/u/i/a.png")

    assert deleted == [
        ("users/u/i/a.png", "v1"),
        ("users/u/i/a.png", "v2"),
        ("users/u/i/a.png", "m1"),
    ]


async def test_delete_falls_back_to_a_plain_delete_when_unversioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.storage import b2

    deleted: list[tuple[str, str | None]] = []

    class _Client:
        def list_object_versions(self, **kwargs: Any) -> dict[str, Any]:
            return {"IsTruncated": False}

        def delete_object(self, **kwargs: Any) -> None:
            deleted.append((kwargs["Key"], kwargs.get("VersionId")))

    monkeypatch.setattr(b2, "_client", lambda: _Client())
    await b2.B2Storage().delete("users/u/i/a.png")

    assert deleted == [("users/u/i/a.png", None)]
