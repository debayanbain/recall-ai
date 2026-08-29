"""Staying reachable, and saying so when we are not.

Three parts of one incident. Telegram remembers a delivery URL that nothing re-checks, so
a moved deployment goes silent while still accepting messages. The webhook only queues,
so with no worker the sender waits on a reply nobody will write. And the notice that
covers both must not itself become a way to spam someone.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.queue import health
from app.services.telegram import notices, webhook

_URL = "https://example.test/api/v1/webhooks/telegram/a-secret-of-sufficient-length"


class FakeClient:
    """Stands in for `TelegramClient`, recording what it was asked to do."""

    def __init__(self, info: dict[str, Any]) -> None:
        self.info = info
        self.registered: list[str] = []
        self.sent: list[tuple[str, str]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_webhook_info(self) -> dict[str, Any]:
        return self.info

    async def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        self.registered.append(url)
        return {"ok": True}

    async def send_message(self, chat_id: str, text: str, **kwargs: Any) -> None:
        self.sent.append((chat_id, text))


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://example.test")
    monkeypatch.setattr(
        settings, "TELEGRAM_WEBHOOK_SECRET", "a-secret-of-sufficient-length"
    )
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "recall_bot")


# --- registration reconciled at boot -------------------------------------------------


async def test_a_matching_registration_is_left_alone(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconditional setWebhook on every boot would drop pending updates each time."""
    client = FakeClient({"url": _URL, "pending_update_count": 0})
    monkeypatch.setattr(webhook, "TelegramClient", lambda: client)

    assert await webhook.ensure_registered() == "unchanged"
    assert client.registered == []


async def test_a_moved_deployment_re_registers_itself(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident: a stale URL means Telegram 404s and the bot goes quietly dead."""
    client = FakeClient(
        {
            "url": "https://old-tunnel.test/api/v1/webhooks/telegram/a-secret-of-sufficient-length",
            "pending_update_count": 3,
            "last_error_message": "Wrong response from the webhook: 404 Not Found",
        }
    )
    monkeypatch.setattr(webhook, "TelegramClient", lambda: client)

    assert await webhook.ensure_registered() == "registered"
    assert client.registered == [_URL]


async def test_a_plaintext_base_url_is_refused_rather_than_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telegram rejects non-https, so local development needs a tunnel, not a retry."""
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "http://localhost:8000")
    assert webhook.can_register() is False
    assert await webhook.ensure_registered() == "skipped"


async def test_telegram_being_down_does_not_stop_the_api_booting(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _explode() -> FakeClient:
        raise ConnectionError("telegram unreachable")

    monkeypatch.setattr(webhook, "TelegramClient", _explode)
    assert await webhook.ensure_registered_quietly() == "failed"


def test_the_registered_url_is_never_logged_whole() -> None:
    """The path *is* the credential; a log line gets copied into issues."""
    assert webhook.redacted(_URL).endswith("/<secret>")
    assert "a-secret-of-sufficient-length" not in webhook.redacted(_URL)


# --- the stall probe fails open ------------------------------------------------------


async def test_an_empty_queue_is_never_stalled(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _depth() -> int:
        return 0

    monkeypatch.setattr(health, "backlog_depth", _depth)
    assert await health.is_stalled() is False


async def test_an_unreachable_broker_is_not_reported_as_a_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken probe must never invent an outage the user then reads as real."""

    async def _depth() -> int:
        return -1

    monkeypatch.setattr(health, "backlog_depth", _depth)
    assert await health.is_stalled() is False


async def test_work_waiting_with_no_worker_is_a_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _depth() -> int:
        return 4

    async def _none() -> bool:
        return False

    monkeypatch.setattr(health, "backlog_depth", _depth)
    monkeypatch.setattr(health, "workers_available", _none)
    assert await health.is_stalled() is True


async def test_work_waiting_with_a_live_worker_is_just_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _depth() -> int:
        return 4

    async def _yes() -> bool:
        return True

    monkeypatch.setattr(health, "backlog_depth", _depth)
    monkeypatch.setattr(health, "workers_available", _yes)
    assert await health.is_stalled() is False


# --- the notice does not become a way to spam someone --------------------------------


async def test_a_burst_of_messages_gets_one_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient({})
    monkeypatch.setattr(notices, "TelegramClient", lambda: client)
    notices.reset_cooldowns()

    for _ in range(5):
        await notices.notify_degraded("4242", queued=True)

    assert len(client.sent) == 1


async def test_two_chats_are_told_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient({})
    monkeypatch.setattr(notices, "TelegramClient", lambda: client)
    notices.reset_cooldowns()

    await notices.notify_degraded("1", queued=True)
    await notices.notify_degraded("2", queued=True)

    assert [chat for chat, _ in client.sent] == ["1", "2"]


async def test_the_two_notices_make_different_promises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued means "it is safe"; not queued means "send it again". Never the reverse."""
    client = FakeClient({})
    monkeypatch.setattr(notices, "TelegramClient", lambda: client)
    notices.reset_cooldowns()

    await notices.notify_degraded("1", queued=True)
    await notices.notify_degraded("2", queued=False)

    safe, resend = client.sent[0][1], client.sent[1][1]
    assert "don't need to send it again" in safe
    assert "send it again" in resend and "wasn't saved" in resend


async def test_telegram_being_unreachable_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This runs on the failure path; raising here would loop Telegram's redelivery."""

    def _explode() -> FakeClient:
        raise ConnectionError("telegram unreachable")

    monkeypatch.setattr(notices, "TelegramClient", _explode)
    notices.reset_cooldowns()

    await notices.notify_degraded("4242", queued=True)
