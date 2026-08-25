"""HTTP contract for uploads and downloads.

Offline: the service and the current user are dependency-overridden, so this exercises
routing, status codes and headers without a database or a bucket.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api import deps
from app.core.config import settings
from app.main import create_app
from app.models.base import ContentType, ProcessingStatus
from app.models.user import User
from app.models.vault import VaultItem
from app.services.documents import DocumentError

PREFIX = settings.API_V1_PREFIX
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

USER = User(id=uuid.uuid4(), email="owner@example.com", name="Owner")


class _Service:
    """Stands in for VaultService; records what the route asked of it."""

    def __init__(self, link: tuple[str, VaultItem] | None = None) -> None:
        self.link = link
        self.saved: list[tuple[uuid.UUID, int, str | None]] = []
        self.raise_on_save: Exception | None = None

    async def save_document(
        self, user_id: uuid.UUID, data: bytes, filename: str | None
    ) -> VaultItem:
        if self.raise_on_save:
            raise self.raise_on_save
        self.saved.append((user_id, len(data), filename))
        return VaultItem(
            user_id=user_id,
            type=ContentType.image,
            title=filename,
            file_name=filename,
            file_size=len(data),
            mime_type="image/png",
            processing_status=ProcessingStatus.skipped,
        )

    async def file_link(
        self, _item_id: uuid.UUID, _user_id: uuid.UUID
    ) -> tuple[str, VaultItem] | None:
        return self.link


@pytest_asyncio.fixture
async def api(request: pytest.FixtureRequest) -> AsyncGenerator[tuple[AsyncClient, Any], None]:
    service = getattr(request, "param", None) or _Service()
    app = create_app()
    app.dependency_overrides[deps.get_current_user] = lambda: USER
    app.dependency_overrides[deps.get_vault_service] = lambda: service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, service
    app.dependency_overrides.clear()


async def test_upload_accepts_a_file(api: tuple[AsyncClient, Any]) -> None:
    client, service = api
    response = await client.post(
        f"{PREFIX}/vault/upload", files={"file": ("holiday.png", PNG, "image/png")}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["file_name"] == "holiday.png"
    assert body["mime_type"] == "image/png"
    # The key is internal: the only way to a file is GET /vault/{id}/file.
    assert "storage_key" not in body
    assert service.saved == [(USER.id, len(PNG), "holiday.png")]


async def test_oversize_upload_is_refused_before_the_service_sees_it(
    api: tuple[AsyncClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read is capped at the limit plus one byte, so a lying Content-Length gains nothing."""
    client, service = api
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)

    response = await client.post(
        f"{PREFIX}/vault/upload",
        files={"file": ("big.png", PNG + b"\x00" * (1024 * 1024), "image/png")},
    )

    assert response.status_code == 413
    assert service.saved == []


async def test_rejected_file_type_is_a_422_with_a_human_message(
    api: tuple[AsyncClient, Any],
) -> None:
    client, service = api
    service.raise_on_save = DocumentError("That file type isn't supported.")

    response = await client.post(
        f"{PREFIX}/vault/upload", files={"file": ("x.svg", b"<svg />", "image/svg+xml")}
    )

    assert response.status_code == 422
    assert "supported" in response.json()["detail"]


async def test_download_link_is_returned_and_not_cached() -> None:
    item = VaultItem(
        user_id=USER.id,
        type=ContentType.image,
        file_name="holiday.png",
        file_size=len(PNG),
        mime_type="image/png",
        storage_key="users/u/i/abc.png",
    )
    service = _Service(link=("https://s3.example.invalid/abc?X-Amz-Signature=x", item))
    app = create_app()
    app.dependency_overrides[deps.get_current_user] = lambda: USER
    app.dependency_overrides[deps.get_vault_service] = lambda: service
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(f"{PREFIX}/vault/{item.id}/file")

    assert response.status_code == 200
    body = response.json()
    assert body["url"].startswith("https://")
    assert body["expires_in"] == settings.DOWNLOAD_LINK_TTL_SECONDS
    assert body["file_name"] == "holiday.png"
    # A signed URL is a bearer credential until it expires.
    assert response.headers["cache-control"] == "no-store"


async def test_missing_or_foreign_file_is_a_404(api: tuple[AsyncClient, Any]) -> None:
    """Same answer for "not yours" and "does not exist" -- ids cannot be probed."""
    client, _ = api
    response = await client.get(f"{PREFIX}/vault/{uuid.uuid4()}/file")
    assert response.status_code == 404


async def test_anonymous_callers_get_401() -> None:
    app = create_app()  # no dependency overrides: the real cookie check runs
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        upload = await client.post(
            f"{PREFIX}/vault/upload", files={"file": ("a.png", PNG, "image/png")}
        )
        download = await client.get(f"{PREFIX}/vault/{uuid.uuid4()}/file")

    assert upload.status_code == 401
    assert download.status_code == 401


async def test_limits_endpoint_tells_the_client_what_it_may_send(
    api: tuple[AsyncClient, Any],
) -> None:
    client, _ = api
    body = (await client.get(f"{PREFIX}/vault/uploads/limits")).json()

    assert body["max_bytes"] == settings.MAX_UPLOAD_MB * 1024 * 1024
    assert "pdf" in body["extensions"]
    # Refused outright, so they never appear as options.
    assert "svg" not in body["extensions"] and "html" not in body["extensions"]
    assert body["storage_enabled"] == settings.storage_enabled
