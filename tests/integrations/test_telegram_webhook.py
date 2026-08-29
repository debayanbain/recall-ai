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


@pytest.fixture(autouse=True)
def _healthy_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy queue, asserted rather than discovered.

    Without this the stall check runs for real: it opens Redis and, finding anything at
    all in the queue, broadcasts a Celery ping and waits out its timeout -- per test. The
    degraded paths get their own tests below, with the probe stubbed the other way.
    """

    async def _not_stalled() -> bool:
        return False

    monkeypatch.setattr("app.api.v1.webhooks.is_stalled", _not_stalled)


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


# --- degraded: say so, rather than leaving the sender waiting -------------------------


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Capture what the sender would have been told, without touching Telegram."""
    sent: list[tuple[str, bool]] = []

    async def _notify(chat_id: str, *, queued: bool) -> None:
        sent.append((chat_id, queued))

    monkeypatch.setattr("app.api.v1.webhooks.notify_degraded", _notify)
    monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", _SECRET)
    return sent


def test_a_broker_outage_is_acknowledged_and_announced(
    monkeypatch: pytest.MonkeyPatch, notices: list[tuple[str, bool]]
) -> None:
    """A 500 here would be an infinite Telegram retry loop *and* still say nothing."""

    async def _boom(update: dict[str, Any]) -> None:
        raise ConnectionError("redis is down")

    monkeypatch.setattr("app.api.v1.webhooks.enqueue_telegram_update", _boom)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/webhooks/telegram/{_SECRET}",
            json=_update(),
            headers={_HEADER: _SECRET},
        )

    assert response.status_code == 202
    assert response.json() == {"status": "unavailable"}
    # queued=False: the update did not land, so the sender must be told to resend.
    assert notices == [("4242", False)]


def test_a_stalled_queue_tells_the_sender_their_message_is_safe(
    monkeypatch: pytest.MonkeyPatch, notices: list[tuple[str, bool]]
) -> None:
    """Durable but unattended: the opposite promise from a broker outage."""

    async def _fake(update: dict[str, Any]) -> None:
        return None

    async def _stalled() -> bool:
        return True

    monkeypatch.setattr("app.api.v1.webhooks.enqueue_telegram_update", _fake)
    monkeypatch.setattr("app.api.v1.webhooks.is_stalled", _stalled)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/webhooks/telegram/{_SECRET}",
            json=_update(),
            headers={_HEADER: _SECRET},
        )

    assert response.status_code == 202
    assert response.json() == {"status": "queued"}
    assert notices == [("4242", True)]


def test_a_healthy_queue_says_nothing(
    queued: list[dict[str, Any]], notices: list[tuple[str, bool]]
) -> None:
    with TestClient(app) as client:
        client.post(
            f"/api/v1/webhooks/telegram/{_SECRET}",
            json=_update(),
            headers={_HEADER: _SECRET},
        )

    assert queued and notices == []


def test_a_stall_check_that_itself_fails_stays_quiet(
    monkeypatch: pytest.MonkeyPatch, notices: list[tuple[str, bool]]
) -> None:
    """A broken probe must never invent an outage the user then reads as real."""

    async def _fake(update: dict[str, Any]) -> None:
        return None

    async def _explode() -> bool:
        raise RuntimeError("probe is broken")

    monkeypatch.setattr("app.api.v1.webhooks.enqueue_telegram_update", _fake)
    monkeypatch.setattr("app.api.v1.webhooks.is_stalled", _explode)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/webhooks/telegram/{_SECRET}",
            json=_update(),
            headers={_HEADER: _SECRET},
        )

    assert response.status_code == 202
    assert notices == []
