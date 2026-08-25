"""The Apify callback endpoint.

It is an unauthenticated public URL by necessity, so the tests here are mostly about what
it refuses to do: trigger work without the shared secret, and trust anything in the body.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.queue import client as queue_client

SECRET = "test-webhook-secret"


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture what the endpoint would enqueue instead of hitting Redis."""
    seen: list[str] = []

    async def fake(run_id: str) -> None:
        seen.append(run_id)

    monkeypatch.setattr(settings, "APIFY_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(queue_client, "enqueue_finalize_run", fake)
    import app.api.v1.webhooks as wh

    monkeypatch.setattr(wh, "enqueue_finalize_run", fake)
    return seen


@pytest.fixture
def client() -> Any:
    with TestClient(app) as c:
        yield c


def _post(client: TestClient, secret: str, body: dict[str, Any]) -> Any:
    return client.post(f"/api/v1/webhooks/apify/{secret}", json=body)


def test_valid_callback_queues_the_run(client: TestClient, queued: list[str]) -> None:
    resp = _post(
        client, SECRET,
        {"eventType": "ACTOR.RUN.SUCCEEDED", "resource": {"id": "run-123"}},
    )
    assert resp.status_code == 202
    assert queued == ["run-123"]


def test_wrong_secret_is_a_404_and_queues_nothing(
    client: TestClient, queued: list[str]
) -> None:
    """404 rather than 403 so the path cannot be probed for a valid secret."""
    resp = _post(client, "wrong", {"resource": {"id": "run-123"}})
    assert resp.status_code == 404
    assert queued == []


def test_unset_secret_does_not_mean_allow_everything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def fake(run_id: str) -> None:
        seen.append(run_id)

    import app.api.v1.webhooks as wh

    monkeypatch.setattr(settings, "APIFY_WEBHOOK_SECRET", "")
    monkeypatch.setattr(wh, "enqueue_finalize_run", fake)

    assert _post(client, "", {"resource": {"id": "x"}}).status_code in (404, 405)
    assert _post(client, "anything", {"resource": {"id": "x"}}).status_code == 404
    assert seen == []


def test_test_pings_without_a_run_id_are_acknowledged(
    client: TestClient, queued: list[str]
) -> None:
    """Apify marks a webhook failing if it 4xxs, so a ping must not be rejected."""
    resp = _post(client, SECRET, {"eventType": "TEST"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"
    assert queued == []


def test_body_data_is_never_trusted(client: TestClient, queued: list[str]) -> None:
    """Only the run id is taken; status and dataset are re-read from Apify by the task."""
    resp = _post(
        client, SECRET,
        {
            "eventType": "ACTOR.RUN.SUCCEEDED",
            "resource": {
                "id": "run-9",
                "status": "SUCCEEDED",
                "defaultDatasetId": "attacker-controlled",
            },
        },
    )
    assert resp.status_code == 202
    # The dataset id in the body is ignored entirely — only the run id is forwarded.
    assert queued == ["run-9"]
