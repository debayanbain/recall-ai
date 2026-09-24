"""Per-identity hourly caps in Redis, for the things that cost money to answer.

The API's own middleware limiter keys on client IP, which is the wrong identity for
anything a model answers: behind one office NAT it throttles a whole company, and behind
a provider's webhook infrastructure it collapses every user onto one key. What costs
money is a *person* asking a question -- an embedding, a retrieval and one or more model
calls -- so that is what is counted.

Redis rather than a process dict because both consumers are multi-process: the API runs
replicas and the Celery worker is prefork, so an in-memory count would give each child
its own allowance and the real limit would be the cap times the concurrency.

**Fails open, on purpose.** Redis being unreachable is already an outage of the queue;
turning it into "nobody may ask anything" doubles the blast radius of a broker problem to
protect against a cost that is bounded by the outage itself. The failure is logged.
"""
from __future__ import annotations

import redis.asyncio as redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("core.rate_limit")

WINDOW_SECONDS = 3600


async def consume(
    namespace: str, identity: str, limit: int, window: int = WINDOW_SECONDS
) -> bool:
    """Take one unit of `identity`'s hourly allowance. True when the caller may proceed.

    `namespace` keeps one surface's counters away from another's -- the same person
    asking through the bot and through the web are two allowances, because they are two
    costs. A limit of zero or less means unlimited, which is how a deployment turns a cap
    off without a second setting to mean "off".

    `window` is a parameter because not every cap is hourly: the per-IP request limiter
    is per *minute*, and a second implementation of "count things in a window" is a second
    thing to get the fail-open behaviour wrong in.
    """
    if limit <= 0:
        return True

    key = f"rl:{namespace}:{identity}"
    # redis-py ships no annotation for from_url; scoped ignore rather than relaxing
    # strict mode for the module.
    client = redis.from_url(settings.redis_url_str)  # type: ignore[no-untyped-call]
    try:
        pipe = client.pipeline()
        pipe.incr(key)
        # NX: set the expiry only when the key has none, which is what makes this a
        # FIXED window. Re-setting the TTL on every hit -- what this did until a
        # production incident -- means a key under continuous traffic never expires:
        # the count is then cumulative since the last full `window` of silence, it
        # crosses the limit once and stays there, and everything from that identity is
        # refused permanently rather than for a minute. The traffic that never goes
        # quiet is the platform's own health probes, so the first thing it took down was
        # the liveness check, which reads as an app that keeps crashing.
        #
        # A true sliding count needs a sorted set per identity and is still not worth it
        # here; the cost of a fixed window is that a burst on a boundary can spend two
        # windows' worth, which is bounded and self-correcting.
        pipe.expire(key, window, nx=True)
        count, _ = await pipe.execute()
        return int(count) <= limit
    except Exception as exc:  # noqa: BLE001 - an abuse control must not become an outage
        log.warning("rate_check_failed", namespace=namespace, error=type(exc).__name__)
        return True
    finally:
        await client.aclose()
