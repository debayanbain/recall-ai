"""Per-Telegram-user hourly caps.

The API's own rate limiter keys on client IP, which is useless here: every update arrives
from Telegram's infrastructure, so one key would cover every user of the bot. This one
keys on the Telegram sender, which is the identity that actually costs money -- a recall
is an embedding plus two model calls.

Counting lives in Redis because the worker is prefork: an in-process dict would give each
child its own allowance, so the real limit would be the cap times the concurrency. That
part is `core/rate_limit`, shared with the web surface; what is left here is which
identity this surface counts and what its caps are called.

**Fails open.** Redis being unreachable must not stop a user saving a link; the queue is
already down in that scenario and the failure is logged.
"""
from __future__ import annotations

from enum import StrEnum

from app.core import rate_limit
from app.core.config import settings


class Action(StrEnum):
    capture = "capture"
    recall = "recall"


def _limit_for(action: Action) -> int:
    if action is Action.recall:
        return settings.TELEGRAM_RECALLS_PER_HOUR
    return settings.TELEGRAM_CAPTURES_PER_HOUR


async def allow(telegram_user_id: str, action: Action) -> bool:
    """Consume one unit of the hourly allowance. True when the caller may proceed.

    The counting itself is `core/rate_limit.consume` -- shared with the web surface,
    which has the same problem with a different identity. What stays here is the part
    that is actually about this surface: which identity is counted, and what the caps
    are called.
    """
    return await rate_limit.consume(
        f"tg:{action.value}", telegram_user_id, _limit_for(action)
    )
