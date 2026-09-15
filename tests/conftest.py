"""Async database + API test harness.

These fixtures need a real PostgreSQL. The models depend on pgvector's ``Vector``,
JSONB, ``PGUUID`` and GIN/HNSW indexes, none of which SQLite can host, so an in-memory
substitute is not an option. When no database is reachable every fixture below skips,
which keeps ``pytest -q`` green on a machine with nothing running.

Bring one up with::

    # any Postgres with pgvector -- a Neon branch, or a second local database
    export TEST_DATABASE_URL=postgresql+asyncpg://USER:PASS@HOST/recall_test?ssl=require

Override the target with ``TEST_DATABASE_URL``.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import app.models  # noqa: F401  -- populates SQLModel.metadata
from app.api.deps import clear_user_cache
from app.core.config import settings
from app.core.security import create_access_token
from app.db.session import get_session
from app.main import create_app
from app.models.base import (
    ConnectionOrigin,
    ConnectionStatus,
    ContentType,
    ProcessingStatus,
    Relation,
    SpaceRole,
    Visibility,
)
from app.models.connection import MemoryConnection
from app.models.space import Space, SpaceMember
from app.models.user import User
from app.models.vault import VaultItem

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://recall:recall@localhost:5432/recall_test",
)

# Order matters: children before parents so the cascade never fires mid-truncate.
_TABLES = [
    "space_invites",
    "space_members",
    "space_items",
    "vault_chunks",
    "vault_items",
    "spaces",
    "subscriptions",
    "audit_log",
    "user_sessions",
    "telegram_link_tokens",
    "telegram_accounts",
    "users",
]

def _assert_not_the_live_database() -> None:
    """Refuse to run against the database the app itself uses.

    The engine fixture below runs `drop_all` and every test truncates. That was harmless
    when the default target was a throwaway local container, but the project now points at
    a hosted Postgres, where a copy-pasted URL would destroy real data.

    This raises rather than skipping: a silent skip is exactly how someone concludes "the
    tests just don't run here" and later points the variable at production to fix it.
    """
    from app.core.config import settings

    def identity(url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        return (parts.hostname or "", parts.path)

    if identity(TEST_DATABASE_URL) == identity(settings.database_url_str):
        raise RuntimeError(
            "TEST_DATABASE_URL points at the same host and database as DATABASE_URL. "
            "The test suite drops every table -- point it at a separate database "
            "(a Neon branch, or a second database on the same instance)."
        )


_assert_not_the_live_database()

_REQUIRED_EXTENSIONS = ("vector", "pg_trgm", "pgcrypto", "citext")


@pytest.fixture(autouse=True)
def _no_startup_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the app's boot self-check off the network.

    The lifespan reconciles the Telegram webhook registration and pings for a live Celery
    worker. Both are real outbound calls, and every `TestClient(app)` runs the lifespan --
    so left on, the suite talks to api.telegram.org once per test and waits out a
    broadcast timeout on top of it. The self-check has its own tests, calls stubbed.

    Pool warm-up is off for the same reason: it opens connections to the *configured*
    database, which in a test run is not the one the fixtures are talking to.

    The current-user cache is cleared per test as well. It is keyed by access-token
    digest, and tests reuse tokens across users and mutate rows behind the dependency --
    exactly the two things the cache is allowed to be blind to for 30 seconds in
    production and must not be for a moment in a test.
    """
    monkeypatch.setattr(settings, "STARTUP_SELF_CHECK", False)
    monkeypatch.setattr(settings, "DB_POOL_WARMUP", 0)
    clear_user_cache()


@pytest.fixture(autouse=True)
def _no_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach a chat provider, even by accident.

    `.env` carries a real key on a developer machine, so a code path that reaches
    `get_chat_model()` without a test having stubbed it does not fail -- it *works*,
    slowly, over the network, and charges for the privilege. That happened: a new lane
    called the model directly and a suite of tests written to be offline started billing
    per run while still passing.

    Every outbound AI capability is closed here, not just the chat model: each one has
    its own client and its own switch, so each is its own way out to the network.
    Both the factory's own name and every module that imported it directly are patched.
    A single patch of `factory.get_chat_model` covers the chains that reach it through
    `resilient`, and misses any module holding its own reference from
    `from ... import get_chat_model` -- so both are done rather than reasoned about.
    A test that wants a model stubs its own over the top of this.
    """

    def _refuse(*args: object, **kwargs: object) -> object:
        raise RuntimeError("tests must not call a chat provider")

    monkeypatch.setattr("app.ai.chat.factory.get_chat_model", _refuse)
    monkeypatch.setattr("app.ai.chat.factory.build_chat_model", _refuse)
    monkeypatch.setattr("app.ai.chat.factory.get_agent_model", _refuse)
    monkeypatch.setattr("app.ai.chat.factory.fallback_models", tuple)
    monkeypatch.setattr("app.ai.chat.tools.get_chat_model", _refuse)
    # The agent loop holds its own reference, imported by name. Patching the factory's
    # copy alone would leave this one pointing at the real thing -- the same trap the
    # comment above describes, and the reason both are always done rather than reasoned
    # about.
    monkeypatch.setattr("app.ai.chat.harness.graph.get_agent_model", _refuse)

    # Embeddings are an outbound provider call too, and until now the only thing keeping
    # them off the network was that every test happened to stub `MemoryRetriever.recall`
    # one level above. A test that reaches retrieval without doing so does not fail --
    # it works, over the network, and bills per run. Closed here for the same reason as
    # the rest: the failure is invisible except as a slower suite.
    monkeypatch.setattr("app.services.chat_engine.retrieval.get_ai_provider", _refuse)

    # Combined enrichment is its own client and its own switch, so it is its own way out
    # to the network. Turned off *and* stubbed: off is what the pipeline's own tests
    # want (they assert on the four-call path through their fake provider), and the stub
    # is what catches a future caller that reaches past the switch.
    monkeypatch.setattr(settings, "ENRICHMENT_COMBINED", False)
    monkeypatch.setattr("app.ai.enrichment._call_provider", _refuse)

    # Relation typing, the same way and for the same two reasons. Off is what the
    # connection tests want -- they assert that an edge keeps the honest `related_to` a
    # distance can actually support -- and the stub is what catches a future caller that
    # reaches past the switch.
    monkeypatch.setattr(settings, "CONNECTION_TYPING_ENABLED", False)
    monkeypatch.setattr("app.ai.connections._call_provider", _refuse)

    # The connection *judge* -- which decides which recalled memories are really
    # connected -- is a fourth way out. It differs from relation typing in one way that
    # matters here: it ships ON, so a derivation test that did not know about it would
    # reach a provider on a machine with a key rather than merely failing to. Off is what
    # the existing tests assert against (the cosine floor writing `related_to`), and the
    # stub is what catches a caller that reaches past the switch. Fourth time this rule
    # has been paid for; a new outbound AI capability belongs here in the same commit.
    monkeypatch.setattr(settings, "CONNECTION_JUDGE_ENABLED", False)
    monkeypatch.setattr("app.ai.connection_judge._call_provider", _refuse)


@pytest.fixture(autouse=True)
def _no_shared_redis_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may depend on what a *previous* run left in Redis.

    A developer machine has a live broker, so anything the code stores there outlives the
    process. Update dedupe is the first such thing: it is a `SET NX` keyed on Telegram's
    `update_id`, and two tests that both dispatch `update_id: 1` are fine on a clean Redis
    and order-dependent on a used one -- the second run of the suite fails a test that the
    first one passed, which reads as flakiness rather than as state.

    Neutralised here rather than per test, for the same reason `_no_provider_calls` is:
    the failure it prevents is one nobody would think to guard against until they had
    spent an afternoon on it. A test of the dedupe itself stubs the Redis client instead.
    """
    async def _always_first(update_id: object) -> bool:
        return True

    monkeypatch.setattr("app.services.telegram.dedupe.claim", _always_first)

    # The per-IP request cap counts in Redis now, which is the point of it -- a count per
    # process was a count per replica. It also means the count survives the test that
    # made it: every request in the suite comes from the same client address, so after
    # sixty of them the limiter starts answering 429 to assertions about 401s. Allowed
    # here for the same reason the dedupe is stubbed, and a test *of* the limiter puts
    # the real function back over the top.
    async def _allow(
        namespace: str, identity: str, limit: int, window: int = 3600
    ) -> bool:
        return True

    monkeypatch.setattr("app.core.rate_limit.consume", _allow)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    """Session-wide engine with the schema built once. Skips if no database answers."""
    eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            for ext in _REQUIRED_EXTENSIONS:
                await conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
            await conn.run_sync(SQLModel.metadata.drop_all)
            await conn.run_sync(SQLModel.metadata.create_all)
    except Exception as exc:  # noqa: BLE001 - any connection/permission failure means skip
        await eng.dispose()
        pytest.skip(f"no test database at {TEST_DATABASE_URL} ({type(exc).__name__}: {exc})")

    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """A committing session. State is cleared by truncation after each test.

    Deliberately not wrapped in a rollback-only transaction: the API under test commits
    at its own request boundary, and faking that away would hide real commit behaviour.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture(loop_scope="session")
async def client(session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Unauthenticated HTTP client wired to the test session.

    ASGITransport does not run the lifespan, so no Redis pool is opened. Tests therefore
    seed rows directly rather than through capture endpoints, which enqueue jobs.
    """
    application = create_app()

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    application.dependency_overrides[get_session] = _override_session
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    application.dependency_overrides.clear()


async def make_user(session: AsyncSession, email: str) -> User:
    user = User(email=email, name=email.split("@")[0], provider_account_id=email)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def make_item(session: AsyncSession, owner: User, title: str) -> VaultItem:
    item = VaultItem(
        user_id=owner.id,
        type=ContentType.note,
        title=title,
        content=f"body of {title}",
        summary=f"summary of {title}",
        processing_status=ProcessingStatus.completed,
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def make_connection(
    session: AsyncSession,
    owner: User,
    source: VaultItem,
    target: VaultItem,
    *,
    relation: Relation = Relation.related_to,
    status: ConnectionStatus = ConnectionStatus.confirmed,
    origin: ConnectionOrigin = ConnectionOrigin.user,
    score: float | None = None,
) -> MemoryConnection:
    """Seed an edge directly.

    Suggested edges have no API that creates them -- the worker does, and the worker needs
    Redis and an embedding -- so every test about confirming, dismissing or reading one
    would otherwise have to stand up the whole derivation first.

    `pair_low` / `pair_high` are GENERATED columns and are deliberately not passed:
    SQLAlchemy omits them from the INSERT, and Postgres fills them.
    """
    connection = MemoryConnection(
        user_id=owner.id,
        source_item_id=source.id,
        target_item_id=target.id,
        relation=relation.value,
        status=status.value,
        origin=origin.value,
        score=score,
    )
    session.add(connection)
    await session.commit()
    await session.refresh(connection)
    return connection


async def make_space(
    session: AsyncSession,
    owner: User,
    name: str,
    visibility: Visibility = Visibility.private,
) -> Space:
    space = Space(
        user_id=owner.id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        visibility=visibility,
    )
    session.add(space)
    await session.commit()
    await session.refresh(space)
    return space


async def make_member(
    session: AsyncSession, space: Space, user: User, role: SpaceRole
) -> SpaceMember:
    """Put someone in a Space directly, bypassing the invite round trip.

    The invite flow has its own tests; every *other* membership test would otherwise have
    to mint and spend a token before it could assert anything about roles.
    """
    member = SpaceMember(space_id=space.id, user_id=user.id, role=role.value)
    session.add(member)
    await session.commit()
    await session.refresh(member)
    return member


def authenticate(client: AsyncClient, user: User) -> None:
    """Attach a genuine signed access cookie, exercising the real auth path.

    The `sid` claim points at no real `user_sessions` row: access tokens are verified by
    signature alone, so nothing in the request path looks it up. Tests that care about
    the server-side session (refresh, logout, the device list) create one for real.
    """
    client.cookies.set(
        settings.SESSION_COOKIE_NAME,
        create_access_token(str(user.id), str(uuid.uuid4())),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def alice(session: AsyncSession) -> User:
    return await make_user(session, "alice@example.com")


@pytest_asyncio.fixture(loop_scope="session")
async def bob(session: AsyncSession) -> User:
    return await make_user(session, "bob@example.com")


@pytest_asyncio.fixture(loop_scope="session")
async def alice_client(client: AsyncClient, alice: User) -> AsyncClient:
    authenticate(client, alice)
    return client


@pytest_asyncio.fixture(loop_scope="session")
async def bob_client(client: AsyncClient, bob: User) -> AsyncClient:
    authenticate(client, bob)
    return client
