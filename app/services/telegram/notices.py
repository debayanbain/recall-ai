"""Telling someone the service is degraded, at the moment they would otherwise wait.

Sent from the web process rather than the worker, because the worker is the part that is
missing. That is the whole point: when the pipeline is down, the only component still
running is the one that took the update, so it is the only one that can say so.

**Rate-limited per chat, in memory.** Redis is frequently the thing that is broken during
an incident, so a Redis-backed cooldown would be unavailable exactly when this fires. An
in-process dict is per API replica -- with several replicas a user could see one notice
per replica -- which is a far smaller harm than five identical apologies from one, or a
cooldown that cannot be read.

Never raises. This runs on the failure path, and an exception here would turn a degraded
reply into a webhook error, which Telegram would then redeliver forever.
"""
from __future__ import annotations

import time

from app.core.logging import get_logger
from app.services.telegram import formatting
from app.services.telegram.client import TelegramClient

log = get_logger("telegram")

#: One notice per chat per minute. Long enough that a burst of messages gets one answer,
#: short enough that a user who waits and retries is told again rather than ignored.
_COOLDOWN_SECONDS = 60.0
#: Bounded so a flood of distinct senders cannot grow this without limit.
_MAX_TRACKED_CHATS = 10_000

_last_notified: dict[str, float] = {}


def _should_notify(chat_id: str) -> bool:
    now = time.monotonic()
    last = _last_notified.get(chat_id)
    if last is not None and now - last < _COOLDOWN_SECONDS:
        return False
    if len(_last_notified) >= _MAX_TRACKED_CHATS:
        # Drop the oldest half rather than clearing: clearing would let a flood reset
        # everyone's cooldown and produce the storm this exists to prevent.
        for stale in sorted(_last_notified, key=lambda k: _last_notified[k])[
            : _MAX_TRACKED_CHATS // 2
        ]:
            del _last_notified[stale]
    _last_notified[chat_id] = now
    return True


async def notify_degraded(chat_id: str, *, queued: bool) -> None:
    """Say the service is degraded, at most once a minute per chat."""
    if not chat_id or not _should_notify(chat_id):
        return
    try:
        async with TelegramClient() as client:
            await client.send_message(chat_id, formatting.service_degraded(queued))
        log.info("telegram_degraded_notice_sent", queued=queued)
    except Exception as exc:  # noqa: BLE001 - the failure path must not fail
        log.warning("telegram_degraded_notice_failed", error=type(exc).__name__)


def reset_cooldowns() -> None:
    """For tests, and for a process that has just recovered."""
    _last_notified.clear()
