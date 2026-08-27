"""Per-Telegram-user hourly caps.

The API's own rate limiter keys on client IP, which is useless here: every update arrives
from Telegram's infrastructure, so one key would cover every user of the bot. This one
keys on the Telegram sender, which is the identity that actually costs money -- a recall
is an embedding plus two model calls.

Counting lives in Redis because the worker is prefork: an in-process dict would give each
child its own allowance, so the real limit would be the cap times the concurrency.

**Fails open.** Redis being unreachable must not stop a user saving a link; the queue is
already down in that scenario and the failure is logged.
"""
from __future__ import annotations

from enum import StrEnum

import redis.asyncio as redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("telegram")

_WINDOW_SECONDS = 3600


class Action(StrEnum):
    capture = "capture"
    recall = "recall"


def _limit_for(action: Action) -> int:
    if action is Action.recall:
        return settings.TELEGRAM_RECALLS_PER_HOUR
    return settings.TELEGRAM_CAPTURES_PER_HOUR


async def allow(telegram_user_id: str, action: Action) -> bool:
    """Consume one unit of the hourly allowance. True when the caller may proceed."""
    limit = _limit_for(action)
    if limit <= 0:
        return True

    key = f"tg:rate:{action.value}:{telegram_user_id}"
    # redis-py ships no annotation for from_url; scoped ignore rather than relaxing
    # strict mode for the module.
    client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        # Fixed window: the TTL is set on every hit rather than only the first, so the
        # window slides with use. A sliding count is not worth a sorted set here.
        pipe.expire(key, _WINDOW_SECONDS)
        count, _ = await pipe.execute()
        return int(count) <= limit
    except Exception as exc:  # noqa: BLE001 - an abuse control must not break capture
        log.warning("telegram_rate_check_failed", error=type(exc).__name__)
        return True
    finally:
        await client.aclose()
