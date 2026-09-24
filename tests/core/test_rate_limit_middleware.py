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


#: What the platform's probes arrive as on K3s: the cluster bridge, not the pod.
POD_NETWORK_CLIENT = "10.42.0.1"
#: An ordinary internet client, from the documentation range.
INTERNET_CLIENT = "203.0.113.9"


def _app(exempt_cidrs: list[str] | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, exempt_cidrs=exempt_cidrs)

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.post(f"{settings.API_V1_PREFIX}/webhooks/telegram/x")
    async def hook() -> dict[str, str]:
        return {"ok": "yes"}

    # The real ones are app/api/health.py; these stand in for them so this file
    # tests the middleware rather than the database behind /ready.
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    return app


async def _client(app: FastAPI, client_host: str = "127.0.0.1") -> AsyncClient:
    """`client_host` is the socket peer -- what `request.client.host` reads."""
    return AsyncClient(
        transport=ASGITransport(app=app, client=(client_host, 51234)),
        base_url="http://testserver",
    )


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


@pytest.mark.parametrize("path", ["/health", "/ready"])
async def test_health_probes_are_never_counted(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A 429 on /health is answered by the platform restarting the container.

    Every kubelet probe in the cluster arrives from one address, so they share a single
    limiter key and spend against the cap continuously -- and the symptom is not a
    throttled probe, it is a pod that keeps being killed and an app that looks like it
    keeps crashing. Raising RATE_LIMIT_PER_MINUTE was the first workaround, and it
    bought the probes headroom by handing the same headroom to every real client.
    """
    calls = _counting(monkeypatch)
    client = await _client(_app())

    response = await client.get(path)

    assert response.status_code == 200
    assert calls == [], "the limiter must not even be consulted"


async def test_a_client_inside_the_exempt_network_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second, independent half: the pod network, whatever it asks for.

    A probe reaching a pod on a path nobody foresaw still must not restart it.
    """
    calls = _counting(monkeypatch)
    client = await _client(_app(["10.42.0.0/16"]), client_host=POD_NETWORK_CLIENT)

    assert (await client.get("/thing")).status_code == 200
    assert calls == []


async def test_a_client_outside_the_exempt_network_still_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The branch that is meant to stay out of the way has to be tested too."""
    calls = _counting(monkeypatch)
    client = await _client(_app(["10.42.0.0/16"]), client_host=INTERNET_CLIENT)

    assert (await client.get("/thing")).status_code == 200
    assert [call["identity"] for call in calls] == [INTERNET_CLIENT]


async def test_a_spoofed_forwarded_for_header_does_not_exempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption reads the socket peer, never a header.

    X-Forwarded-For is written by whoever is calling. uvicorn rewrites `client` from it
    only for peers already inside --forwarded-allow-ips, and then takes the right-most
    entry that is not itself trusted -- so the internet cannot claim to be the pod
    network. This pins the half that lives in this repository: nothing here reads the
    header, so the claim is ignored whatever the deployment does with proxy headers.
    """
    calls = _counting(monkeypatch)
    client = await _client(_app(["10.42.0.0/16"]), client_host=INTERNET_CLIENT)

    response = await client.get(
        "/thing", headers={"X-Forwarded-For": f"{POD_NETWORK_CLIENT}, 10.42.0.7"}
    )

    assert response.status_code == 200
    assert [call["identity"] for call in calls] == [INTERNET_CLIENT]


async def test_an_empty_exempt_list_counts_the_pod_network_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The setting can be turned off, and turning it off means off.

    The probe *paths* stay exempt regardless -- they are two of this app's own routes,
    not a deployment's opinion.
    """
    calls = _counting(monkeypatch)
    client = await _client(_app([]), client_host=POD_NETWORK_CLIENT)

    assert (await client.get("/thing")).status_code == 200
    assert [call["identity"] for call in calls] == [POD_NETWORK_CLIENT]

    assert (await client.get("/health")).status_code == 200
    assert len(calls) == 1, "the path exemption does not depend on the CIDR list"
