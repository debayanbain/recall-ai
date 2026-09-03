"""The per-IP request cap: shared, bounded, and never an outage of its own.

It used to count in a process dict, which was wrong twice. Once in the way the docstring
admitted -- a count per process is a count per replica, so the real limit was the
configured one times the concurrency. And once in a way nobody had written down: the dict
was never pruned, so every distinct client address became a permanent entry and the
limiter was a slow memory leak reachable from the internet by rotating IPs.

Both are gone because the counting moved to `core/rate_limit`, which the per-user caps
already used. What is pinned here is that it actually counts, that it fails open, and
that provider callbacks still bypass it.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import middleware as middleware_module
from app.core import rate_limit
from app.core.config import settings
from app.core.middleware import RateLimitMiddleware

#: Bound at import, before any fixture runs. The autouse `_no_shared_redis_state` fixture
#: replaces `rate_limit.consume` for the whole suite -- which is what keeps a live Redis
#: from making every other test order-dependent, and which would otherwise make the tests
#: *of* the limiter pass without running a line of it.
_REAL_CONSUME = rate_limit.consume


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware)

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post(f"{settings.API_V1_PREFIX}/webhooks/telegram/x")
    async def hook() -> dict[str, str]:
        return {"ok": "yes"}

    return app


async def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


def _counting(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record what the middleware asks the shared limiter for."""
    calls: list[dict[str, Any]] = []

    async def _consume(
        namespace: str, identity: str, limit: int, window: int = 3600
    ) -> bool:
        calls.append(
            {"namespace": namespace, "identity": identity, "limit": limit, "window": window}
        )
        return len(calls) <= 2

    monkeypatch.setattr(middleware_module.rate_limit, "consume", _consume)
    return calls


async def test_it_counts_per_minute_against_the_shared_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One implementation of "count things in a window", not two."""
    calls = _counting(monkeypatch)
    client = await _client(_app())

    assert (await client.get("/thing")).status_code == 200

    assert calls[0]["namespace"] == "ip"
    assert calls[0]["window"] == 60
    assert calls[0]["limit"] == settings.RATE_LIMIT_PER_MINUTE


async def test_past_the_cap_it_answers_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _counting(monkeypatch)
    client = await _client(_app())

    await client.get("/thing")
    await client.get("/thing")
    refused = await client.get("/thing")

    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "60"


async def test_provider_callbacks_are_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every user's traffic collapses onto one provider address.

    A 429 there is a delivery failure both Telegram and Apify retry, which turns the cap
    into a self-amplifying backlog. Those routes are gated by a shared secret instead.
    """
    calls = _counting(monkeypatch)
    client = await _client(_app())

    response = await client.post(f"{settings.API_V1_PREFIX}/webhooks/telegram/x")

    assert response.status_code == 200
    assert calls == [], "the limiter must not even be consulted"


async def test_an_unreachable_redis_does_not_refuse_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails open, like the per-user caps.

    Redis being down is already an outage of the queue; turning it into "nobody may make
    a request" doubles the blast radius to guard against a cost the outage already bounds.
    """

    class Broken:
        # `pipeline()` is synchronous on the real client, so this one is too -- an async
        # stub raises before it is awaited and leaves a warning about the test rather
        # than exercising the path.
        def pipeline(self) -> Any:
            raise ConnectionError("no route to host")

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(rate_limit, "consume", _REAL_CONSUME)
    monkeypatch.setattr(rate_limit.redis, "from_url", lambda *a, **k: Broken())
    client = await _client(_app())

    assert (await client.get("/thing")).status_code == 200
