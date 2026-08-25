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
from app.core.config import settings
from app.core.security import create_session_token
from app.db.session import get_session
from app.main import create_app
from app.models.base import ContentType, ProcessingStatus, Visibility
from app.models.collection import Collection
from app.models.user import User
from app.models.vault import VaultItem

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://recall:recall@localhost:5432/recall_test",
)

# Order matters: children before parents so the cascade never fires mid-truncate.
_TABLES = [
    "collection_items",
    "vault_chunks",
    "vault_items",
    "collections",
    "subscriptions",
    "audit_log",
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


async def make_collection(
    session: AsyncSession,
    owner: User,
    name: str,
    visibility: Visibility = Visibility.private,
) -> Collection:
    collection = Collection(
        user_id=owner.id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        visibility=visibility,
    )
    session.add(collection)
    await session.commit()
    await session.refresh(collection)
    return collection


def authenticate(client: AsyncClient, user: User) -> None:
    """Attach a genuine signed session cookie, exercising the real auth path."""
    client.cookies.set(settings.SESSION_COOKIE_NAME, create_session_token(str(user.id)))


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
