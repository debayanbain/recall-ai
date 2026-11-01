"""`/ready` reaches the database until it succeeds once, and then never again.

It used to run `SELECT 1` on every probe -- every 10 seconds, per pod, forever -- which
kept a Neon compute from ever suspending and spent the free plan's monthly compute in
about half a month, taking production down. The route is also public and exempt from
rate limiting, so every anonymous hit was a query.

What has to survive is the rollout guarantee: a pod that cannot reach its database never
becomes Ready, so a deploy with a bad URL keeps the old pod serving.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api import health
from app.db import session as db_session


class _FakeEngine:
    """Counts connections and fails on demand; stands in for the module-level engine."""

    def __init__(self) -> None:
        self.connects = 0
        self.down = False

    def connect(self) -> Any:
        self.connects += 1
        engine = self

        @asynccontextmanager
        async def _conn() -> AsyncIterator[Any]:
            if engine.down:
                raise ConnectionRefusedError("db.internal.example:5432 refused")

            class _Conn:
                async def execute(self, _statement: object) -> None:
                    return None

            yield _Conn()

        return _conn()


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    fake = _FakeEngine()
    monkeypatch.setattr(db_session, "engine", fake)
    monkeypatch.setattr(db_session, "_database_reached", False)
    return fake


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(health.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_the_first_probe_queries_and_the_rest_do_not(
    engine: _FakeEngine, client: httpx.AsyncClient
) -> None:
    for _ in range(5):
        response = await client.get("/ready")
        assert response.status_code == 200
    assert engine.connects == 1


async def test_a_pod_that_cannot_reach_the_database_is_not_ready(
    engine: _FakeEngine, client: httpx.AsyncClient
) -> None:
    """The rollout guarantee: a bad URL must never replace a working pod."""
    engine.down = True
    response = await client.get("/ready")
    assert response.status_code == 503
    assert not db_session.database_reached()


async def test_the_503_does_not_echo_the_drivers_message(
    engine: _FakeEngine, client: httpx.AsyncClient
) -> None:
    """The route is public; a driver error can name the host."""
    engine.down = True
    response = await client.get("/ready")
    assert "db.internal.example" not in response.text


async def test_a_pod_that_booted_during_an_outage_becomes_ready_when_it_ends(
    engine: _FakeEngine, client: httpx.AsyncClient
) -> None:
    engine.down = True
    assert (await client.get("/ready")).status_code == 503
    engine.down = False
    assert (await client.get("/ready")).status_code == 200
    assert engine.connects == 2


async def test_a_later_outage_does_not_pull_the_pod_out_of_rotation(
    engine: _FakeEngine, client: httpx.AsyncClient
) -> None:
    """Every pod shares the database: flipping them all NotReady is a total outage."""
    assert (await client.get("/ready")).status_code == 200
    engine.down = True
    assert (await client.get("/ready")).status_code == 200
    assert engine.connects == 1


async def test_a_warmed_pool_counts_as_reaching_the_database(
    engine: _FakeEngine, client: httpx.AsyncClient
) -> None:
    assert await db_session.warm_pool(2) == 2
    before = engine.connects
    assert (await client.get("/ready")).status_code == 200
    assert engine.connects == before


async def test_a_failed_warm_up_leaves_readiness_to_check_for_itself(
    engine: _FakeEngine, client: httpx.AsyncClient
) -> None:
    engine.down = True
    assert await db_session.warm_pool(2) == 0
    assert not db_session.database_reached()
    engine.down = False
    assert (await client.get("/ready")).status_code == 200
