"""Is anything actually going to run the work we just queued?

The webhook's job is to accept an update and answer fast, so it queues and returns. That
is right until nothing is consuming the queue: the API still answers 202, Telegram is
satisfied, and the person who sent the message waits for a reply no process is going to
write. Nothing in the system notices, because from every component's point of view it
worked.

This is the cheap check that turns that silence into a sentence. Two signals, in cost
order:

* **Queue depth** -- one `LLEN`, about a millisecond. With a worker running this is
  almost always zero, because a task is consumed as fast as it is pushed. So the happy
  path pays for one Redis call and stops.
* **A control-plane ping** -- a broadcast with a timeout, hundreds of milliseconds when
  nobody answers. Only reached once depth is non-zero, and cached, so a stalled queue
  costs one ping per `_PING_TTL` rather than one per message.

Every function here fails *open*: if the check itself cannot run we report "not
degraded". A broken health probe must never invent an outage.
"""
from __future__ import annotations

import asyncio
import time

import redis.asyncio as redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("queue.health")

#: How long a worker-presence answer stays good. Short, because the interesting moment is
#: recovery: a user who was told "starting up" should not keep being told it once a
#: worker is back.
_PING_TTL = 10.0
_PING_TIMEOUT = 1.0

_cached: tuple[float, bool] | None = None


async def backlog_depth() -> int:
    """How many tasks are waiting. -1 when the broker itself cannot be reached."""
    client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
    try:
        from app.queue.celery_app import celery_app

        queue = celery_app.conf.task_default_queue or "celery"
        return int(await client.llen(queue))
    except Exception as exc:  # noqa: BLE001 - an unreachable broker is its own answer
        log.warning("queue_depth_unavailable", error=type(exc).__name__)
        return -1
    finally:
        await client.aclose()


async def workers_available() -> bool:
    """Whether any worker answers a ping. Cached for `_PING_TTL`."""
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _PING_TTL:
        return _cached[1]

    from app.queue.celery_app import celery_app

    try:
        replies = await asyncio.to_thread(
            celery_app.control.ping, timeout=_PING_TIMEOUT
        )
        available = bool(replies)
    except Exception as exc:  # noqa: BLE001 - fail open, never invent an outage
        log.warning("celery_ping_failed", error=type(exc).__name__)
        available = True

    _cached = (now, available)
    return available


async def is_stalled() -> bool:
    """True when work is piling up and nothing is consuming it.

    Deliberately conservative. A depth of zero is never stalled however the ping goes,
    and a ping that cannot be performed is never treated as an outage -- the cost of a
    false positive is telling a working bot's user that it is broken.
    """
    depth = await backlog_depth()
    if depth <= 0:
        return False
    if await workers_available():
        return False
    log.warning("queue_stalled", depth=depth)
    return True


def reset_cache() -> None:
    """Forget the cached ping. For tests, and for a boot that just started a worker."""
    global _cached
    _cached = None
