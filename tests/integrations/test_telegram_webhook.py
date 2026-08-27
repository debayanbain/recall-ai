"""The Telegram webhook's two gates, and its refusal to ever 4xx a real update.

The endpoint is unauthenticated by design -- Telegram carries no session -- so the shared
secret is the whole of its access control, and it is checked twice: once in the path and
once in the header Telegram echoes back. A leaked URL alone must not be enough.

The second property is subtler and just as load-bearing: anything Telegram delivers that
we cannot act on is acknowledged, not rejected. Telegram redelivers non-2xx responses
with the same bytes, so one 500 on a malformed update becomes an endless retry loop.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

_SECRET = "test-telegram-webhook-secret-value-0123456789"
_HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _update(**overrides: Any) -> dict[str, Any]:
    message = {
        "message_id": 7,
        "chat": {"id": 4242, "type": "private"},
        "from": {"id": 4242, "first_name": "Ada"},
        "text": "https://example.com/article",
    }
    message.update(overrides)
    return {"update_id": 100, "message": message}


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture what would have been handed to Celery, without a broker."""
    calls: list[dict[str, Any]] = []

    async def _fake(update: dict[str, Any]) -> None:
        calls.append(update)

    monkeypatch.setattr("app.api.v1.webhooks.enqueue_telegram_update", _fake)
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", _SECRET)
    return calls


@pytest.fixture
def client() -> Any:
    with TestClient(app) as c:
        yield c


def _post(client: TestClient, secret: str, header: str | None, body: Any) -> Any:
    headers = {} if header is None else {_HEADER: header}
    return client.post(f"/api/v1/webhooks/telegram/{secret}", json=body, headers=headers)


def test_valid_update_is_queued(client: TestClient, queued: list[dict[str, Any]]) -> None:
    response = _post(client, _SECRET, _SECRET, _update())
    assert response.status_code == 202
    assert response.json() == {"status": "queued"}
    assert len(queued) == 1


def test_wrong_path_secret_is_404(client: TestClient, queued: list[dict[str, Any]]) -> None:
    response = _post(client, "not-the-secret", _SECRET, _update())
    assert response.status_code == 404
    assert queued == []


def test_wrong_header_secret_is_404(client: TestClient, queued: list[dict[str, Any]]) -> None:
    """A leaked URL is not enough on its own -- that is the whole point of the header."""
    response = _post(client, _SECRET, "not-the-secret", _update())
    assert response.status_code == 404
    assert queued == []


def test_missing_header_is_404(client: TestClient, queued: list[dict[str, Any]]) -> None:
    response = _post(client, _SECRET, None, _update())
    assert response.status_code == 404
    assert queued == []


def test_unset_secret_rejects_everything(
    client: TestClient, queued: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconfigured secret must not mean "allow anything"."""
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "")
    assert _post(client, "", "", _update()).status_code == 404
    assert queued == []


def test_malformed_json_is_400_not_500(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    response = client.post(
        f"/api/v1/webhooks/telegram/{_SECRET}",
        content=b"{not json",
        headers={_HEADER: _SECRET, "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert queued == []


def test_update_without_a_message_is_acknowledged(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    """Acknowledged, not rejected: a 4xx here would be redelivered forever."""
    response = _post(client, _SECRET, _SECRET, {"update_id": 1, "edited_message": {}})
    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}
    assert queued == []


def test_oversized_body_is_refused_before_parsing(
    client: TestClient, queued: list[dict[str, Any]]
) -> None:
    huge = _update(text="x" * 1_100_000)
    response = _post(client, _SECRET, _SECRET, huge)
    assert response.status_code == 413
    assert queued == []
