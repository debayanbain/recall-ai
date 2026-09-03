"""One update, processed once.

Telegram redelivers an update on **any** non-2xx, and it redelivers the same bytes; the
webhook answers 202 to everything precisely so that never becomes an infinite loop. That
leaves a second delivery path nothing covers: the queue. `handle_telegram_update` runs
with `acks_late=True` and `task_reject_on_worker_lost=True`, so a worker killed after the
model call but before the ack returns the message to the broker and the whole turn runs
again -- a second embedding, a second answer, a second bill, and for a capture a second
row. The webhook cannot see that happen, which is why the check lives here in the worker
and not in the route.

`update_id` is the natural key: unique per bot, already assigned by Telegram, and already
what the log's `request_id` is built from. A day is far longer than any redelivery window
and short enough that the keyspace stays trivial.

**Fails open**, for the same reason `core/rate_limit` does: Redis being unreachable is
already a queue outage, and turning it into "no message is ever answered" widens the
blast radius to guard against a duplicate.
"""
from __future__ import annotations

import redis.asyncio as redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("telegram")


async def claim(update_id: object) -> bool:
    """True when this update is ours to process. False when it has already been seen.

    An update with no `update_id` -- a shape Telegram does not send -- is claimed rather
    than dropped: refusing to answer a message because it lacks a field we only use for
    bookkeeping would be the bookkeeping deciding what gets a reply.
    """
    if not isinstance(update_id, int):
        return True

    # redis-py ships no annotation for from_url; scoped ignore rather than relaxing
    # strict mode for the module.
    client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
    try:
        # SET NX is the whole mechanism: the first caller to reach Redis wins, and two
        # workers racing on the same redelivery cannot both win it.
        first = await client.set(
            f"tg:update:{update_id}",
            "1",
            nx=True,
            ex=settings.TELEGRAM_UPDATE_DEDUPE_TTL_SECONDS,
        )
        return bool(first)
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        log.warning("telegram_dedupe_failed", error=type(exc).__name__)
        return True
    finally:
        await client.aclose()
