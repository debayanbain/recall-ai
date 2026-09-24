"""The window has to actually end.

`consume` re-set the key's TTL on every hit, which reads like a sliding window and is
not one: a key under continuous traffic never expires, so the count is cumulative since
the last full `window` of silence. It crosses the limit once and stays there, and the
identity is refused *permanently* rather than for a minute.

The traffic that never goes quiet is the platform's own health probes -- one key for all
of them, a hit every ten seconds, forever -- so the first thing this took down was the
liveness check, and a rate limiter presented as an application that kept crashing. The
per-user hourly caps had the same shape: an active user locked out for good.

These tests drive the real `consume` against a Redis that implements INCR, EXPIRE and
TTL faithfully enough to tell the two behaviours apart. `test_the_window_ends_under_
continuous_traffic` is the regression: it fails against `expire(key, window)`.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core import rate_limit

#: Bound at import: the autouse `_no_shared_redis_state` fixture replaces `consume` for
#: the whole suite, which would otherwise make the tests *of* it pass without running a
#: line. The same reason tests/core/test_rate_limit_middleware.py keeps this handle.
_REAL_CONSUME = rate_limit.consume


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePipeline:
    """Queues commands like redis-py's, applies them on execute()."""

    def __init__(self, client: FakeRedis) -> None:
        self._client = client
        self._queued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def incr(self, key: str) -> None:
        self._queued.append(("incr", (key,), {}))

    def expire(self, key: str, seconds: int, **flags: bool) -> None:
        self._queued.append(("expire", (key, seconds), flags))

    async def execute(self) -> list[Any]:
        return [
            getattr(self._client, name)(*args, **kwargs)
            for name, args, kwargs in self._queued
        ]


class FakeRedis:
    """INCR / EXPIRE / TTL with real expiry semantics, driven by a fake clock."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.values: dict[str, int] = {}
        self.expires_at: dict[str, float] = {}

    def _sweep(self, key: str) -> None:
        deadline = self.expires_at.get(key)
        if deadline is not None and self.clock.now >= deadline:
            self.values.pop(key, None)
            self.expires_at.pop(key, None)

    def incr(self, key: str) -> int:
        self._sweep(key)
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key: str, seconds: int, nx: bool = False, **_: bool) -> bool:
        self._sweep(key)
        if key not in self.values:
            return False
        if nx and key in self.expires_at:
            return False
        self.expires_at[key] = self.clock.now + seconds
        return True

    def ttl(self, key: str) -> int:
        self._sweep(key)
        if key not in self.expires_at:
            return -1
        return int(self.expires_at[key] - self.clock.now)

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    clock = FakeClock()
    client = FakeRedis(clock)
    monkeypatch.setattr(rate_limit, "consume", _REAL_CONSUME)
    monkeypatch.setattr(rate_limit.redis, "from_url", lambda *a, **k: client)
    return client


async def test_it_refuses_past_the_limit_inside_one_window(fake_redis: FakeRedis) -> None:
    allowed = [await rate_limit.consume("ip", "10.0.0.1", 3, window=60) for _ in range(5)]

    assert allowed == [True, True, True, False, False]


async def test_the_window_ends_under_continuous_traffic(fake_redis: FakeRedis) -> None:
    """The regression. Traffic that never pauses must still get a fresh window.

    Health probes are exactly this: one shared key, a request every ten seconds, no gap
    ever. With the TTL refreshed on each hit the key outlives every window it is given
    and the count only ever grows.
    """
    # Spend the whole allowance, then keep knocking every 10s for two more minutes.
    for _ in range(3):
        assert await rate_limit.consume("ip", "10.42.0.1", 3, window=60) is True
    assert await rate_limit.consume("ip", "10.42.0.1", 3, window=60) is False

    verdicts = []
    for _ in range(12):
        fake_redis.clock.advance(10)
        verdicts.append(await rate_limit.consume("ip", "10.42.0.1", 3, window=60))

    assert True in verdicts, "the window never reopened: the counter is cumulative"
    # The timeline, with the first window opened at t=0 by the four calls above:
    #   t=10..50   still inside it, already spent          -> False x5
    #   t=60       it expired; a fresh window, 3 allowed   -> True x3 (60, 70, 80)
    #   t=90..110  spent again                             -> False x3
    #   t=120      the second window expired               -> True
    assert verdicts[:5] == [False] * 5
    assert verdicts[5:8] == [True] * 3
    assert verdicts[8:11] == [False] * 3
    assert verdicts[11] is True


async def test_the_ttl_is_set_once_and_then_left_alone(fake_redis: FakeRedis) -> None:
    """What `nx=True` buys, asserted directly rather than through behaviour."""
    await rate_limit.consume("ip", "198.51.100.4", 100, window=60)
    assert fake_redis.ttl("rl:ip:198.51.100.4") == 60

    fake_redis.clock.advance(30)
    await rate_limit.consume("ip", "198.51.100.4", 100, window=60)

    assert fake_redis.ttl("rl:ip:198.51.100.4") == 30, "the hit must not extend the window"


async def test_a_quiet_identity_starts_over(fake_redis: FakeRedis) -> None:
    for _ in range(3):
        await rate_limit.consume("ask", "user-1", 3, window=3600)
    assert await rate_limit.consume("ask", "user-1", 3, window=3600) is False

    fake_redis.clock.advance(3600)

    assert await rate_limit.consume("ask", "user-1", 3, window=3600) is True


async def test_a_limit_of_zero_is_unlimited_and_never_touches_redis(
    fake_redis: FakeRedis,
) -> None:
    assert await rate_limit.consume("ip", "10.0.0.9", 0) is True
    assert fake_redis.values == {}
