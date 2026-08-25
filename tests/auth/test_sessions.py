"""Persistent login: refresh rotation, replay detection, logout, device list.

These pin the properties the two-cookie scheme exists for. A user who returns three days
later must be signed in without touching the provider; a stolen refresh token must stop
working the moment the real client uses its own; and "log out" must kill the credential
server-side rather than merely clearing a cookie the thief never had anyway.

Needs a real PostgreSQL (see conftest) -- these skip without one.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.security import create_access_token, hash_refresh_token
from app.models.user import User
from app.models.user_session import UserSession
from app.repositories.user import UserRepository
from app.repositories.user_session import UserSessionRepository
from app.services.session_service import IssuedSession, SessionService
from tests.conftest import authenticate

REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"


def _service(session: AsyncSession) -> SessionService:
    return SessionService(UserSessionRepository(session), UserRepository(session))


async def _open_session(session: AsyncSession, user: User) -> IssuedSession:
    """Start a real server-side session, exactly as the OAuth callback does."""
    issued = await _service(session).start(
        user, user_agent="pytest-agent/1.0", ip_address="203.0.113.7"
    )
    await session.commit()
    return issued


def _with_refresh(client: AsyncClient, token: str) -> None:
    client.cookies.set(settings.REFRESH_COOKIE_NAME, token)


async def _row_for(session: AsyncSession, token: str) -> UserSession | None:
    result = await session.exec(
        select(UserSession).where(UserSession.token_hash == hash_refresh_token(token))
    )
    return result.first()


@pytest_asyncio.fixture(loop_scope="session")
async def alice_session(session: AsyncSession, alice: User) -> IssuedSession:
    return await _open_session(session, alice)


async def test_refresh_returns_a_new_access_and_refresh_pair(
    client: AsyncClient, alice_session: IssuedSession
) -> None:
    """The whole point: a valid refresh cookie alone re-authenticates, no provider."""
    _with_refresh(client, alice_session.refresh_token)

    response = await client.post(REFRESH_URL)

    assert response.status_code == 200
    assert response.cookies[settings.SESSION_COOKIE_NAME]
    rotated = response.cookies[settings.REFRESH_COOKIE_NAME]
    assert rotated != alice_session.refresh_token


async def test_refreshed_access_token_authenticates(
    client: AsyncClient, alice: User, alice_session: IssuedSession
) -> None:
    """A user back after three days: refresh, then /me answers as them."""
    _with_refresh(client, alice_session.refresh_token)
    await client.post(REFRESH_URL)

    me = await client.get("/api/v1/auth/me")

    assert me.status_code == 200
    assert me.json()["id"] == str(alice.id)


async def test_refresh_cookie_is_scoped_to_the_auth_routes(
    client: AsyncClient, alice_session: IssuedSession
) -> None:
    """Path=/api/v1/auth keeps the long-lived credential off every other endpoint."""
    _with_refresh(client, alice_session.refresh_token)

    response = await client.post(REFRESH_URL)

    header = next(
        value
        for key, value in response.headers.multi_items()
        if key == "set-cookie" and value.startswith(settings.REFRESH_COOKIE_NAME)
    )
    assert "Path=/api/v1/auth" in header
    assert "HttpOnly" in header


async def test_rotation_extends_the_window(
    client: AsyncClient, session: AsyncSession, alice_session: IssuedSession
) -> None:
    """Sliding expiry: each visit buys another full REFRESH_TOKEN_EXPIRE_DAYS."""
    _with_refresh(client, alice_session.refresh_token)

    response = await client.post(REFRESH_URL)

    new_row = await _row_for(session, response.cookies[settings.REFRESH_COOKIE_NAME])
    assert new_row is not None
    assert new_row.expires_at > alice_session.refresh_expires_at


async def test_rotation_retires_the_presented_token(
    client: AsyncClient, session: AsyncSession, alice_session: IssuedSession
) -> None:
    """Single use: the token that was just spent is dead in the database."""
    _with_refresh(client, alice_session.refresh_token)
    await client.post(REFRESH_URL)

    old = await _row_for(session, alice_session.refresh_token)
    assert old is not None
    assert old.revoked_at is not None
    assert old.revoked_reason == "rotated"
    assert old.replaced_by_id is not None


async def test_replaying_a_rotated_token_is_rejected(
    client: AsyncClient, alice_session: IssuedSession
) -> None:
    _with_refresh(client, alice_session.refresh_token)
    await client.post(REFRESH_URL)

    _with_refresh(client, alice_session.refresh_token)
    replay = await client.post(REFRESH_URL)

    assert replay.status_code == 401


async def test_replay_revokes_the_whole_chain(
    client: AsyncClient, session: AsyncSession, alice_session: IssuedSession
) -> None:
    """The thief and the victim cannot be told apart, so both are signed out.

    Without this, a stolen refresh token stays useful for as long as its holder keeps
    rotating it -- the legitimate client's own refresh would never disturb it.
    """
    _with_refresh(client, alice_session.refresh_token)
    live = (await client.post(REFRESH_URL)).cookies[settings.REFRESH_COOKIE_NAME]

    _with_refresh(client, alice_session.refresh_token)  # the stolen copy
    await client.post(REFRESH_URL)

    _with_refresh(client, live)  # the honest client's own, still unused
    assert (await client.post(REFRESH_URL)).status_code == 401

    session.expire_all()
    row = await _row_for(session, live)
    assert row is not None
    assert row.revoked_reason == "reuse_detected"


async def test_unknown_refresh_token_is_rejected_and_cookie_cleared(
    client: AsyncClient,
) -> None:
    _with_refresh(client, "not-a-real-token")

    response = await client.post(REFRESH_URL)

    assert response.status_code == 401
    # The dead cookie is cleared, or the SPA retries this endpoint on every navigation.
    assert response.cookies.get(settings.REFRESH_COOKIE_NAME) in (None, "")


async def test_refresh_without_a_cookie_is_rejected(client: AsyncClient) -> None:
    assert (await client.post(REFRESH_URL)).status_code == 401


async def test_expired_refresh_token_is_rejected(
    client: AsyncClient, session: AsyncSession, alice_session: IssuedSession
) -> None:
    """Eight days away is a re-login. Seven days minus a minute is not."""
    row = await _row_for(session, alice_session.refresh_token)
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    session.add(row)
    await session.commit()

    _with_refresh(client, alice_session.refresh_token)
    assert (await client.post(REFRESH_URL)).status_code == 401


async def test_absolute_lifetime_caps_an_endlessly_rotated_chain(
    client: AsyncClient, session: AsyncSession, alice_session: IssuedSession
) -> None:
    """Activity slides the window, but not past REFRESH_TOKEN_ABSOLUTE_DAYS."""
    row = await _row_for(session, alice_session.refresh_token)
    assert row is not None
    row.family_started_at = datetime.now(UTC) - timedelta(
        days=settings.REFRESH_TOKEN_ABSOLUTE_DAYS + 1
    )
    session.add(row)
    await session.commit()

    _with_refresh(client, alice_session.refresh_token)
    assert (await client.post(REFRESH_URL)).status_code == 401


async def test_refresh_for_a_deleted_user_is_rejected(
    client: AsyncClient, session: AsyncSession, alice: User, alice_session: IssuedSession
) -> None:
    """A soft-deleted account must not be brought back by a token minted before it went."""
    alice.deleted_at = datetime.now(UTC)
    session.add(alice)
    await session.commit()

    _with_refresh(client, alice_session.refresh_token)
    assert (await client.post(REFRESH_URL)).status_code == 401


async def test_logout_revokes_the_token_server_side(
    client: AsyncClient, alice_session: IssuedSession
) -> None:
    """Clearing cookies is not logging out: the row has to die too."""
    _with_refresh(client, alice_session.refresh_token)
    assert (await client.post(LOGOUT_URL)).status_code == 200

    _with_refresh(client, alice_session.refresh_token)
    assert (await client.post(REFRESH_URL)).status_code == 401


async def test_logout_with_no_session_still_succeeds(client: AsyncClient) -> None:
    """A client that cannot read its own HttpOnly cookies must not be stranded on a 401."""
    assert (await client.post(LOGOUT_URL)).status_code == 200


async def test_logout_all_kills_every_device(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    phone = await _open_session(session, alice)
    laptop = await _open_session(session, alice)
    authenticate(client, alice)

    response = await client.post("/api/v1/auth/logout-all")

    assert response.status_code == 200
    assert response.json()["revoked"] >= 2
    for token in (phone.refresh_token, laptop.refresh_token):
        _with_refresh(client, token)
        assert (await client.post(REFRESH_URL)).status_code == 401


async def test_session_list_shows_only_the_callers_own_devices(
    client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """Tenant scoping: bob's session id must never appear in alice's list."""
    await _open_session(session, alice)
    bob_session = await _open_session(session, bob)
    authenticate(client, alice)

    listed = (await client.get("/api/v1/auth/sessions")).json()

    ids = {row["id"] for row in listed}
    assert ids
    assert str(bob_session.session_id) not in ids


async def test_session_list_never_leaks_a_token(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    issued = await _open_session(session, alice)
    authenticate(client, alice)

    body = (await client.get("/api/v1/auth/sessions")).text

    assert issued.refresh_token not in body
    assert hash_refresh_token(issued.refresh_token) not in body


async def test_revoking_another_users_session_is_a_404(
    client: AsyncClient, session: AsyncSession, alice: User, bob: User
) -> None:
    """404, not 403: a distinguishing answer is a probe for other users' session ids."""
    bob_session = await _open_session(session, bob)
    authenticate(client, alice)

    response = await client.delete(f"/api/v1/auth/sessions/{bob_session.session_id}")

    assert response.status_code == 404

    session.expire_all()
    result = await session.exec(
        select(UserSession).where(col(UserSession.id) == bob_session.session_id)
    )
    still_live = result.first()
    assert still_live is not None
    assert still_live.revoked_at is None


async def test_user_can_revoke_one_of_their_own_sessions(
    client: AsyncClient, session: AsyncSession, alice: User
) -> None:
    issued = await _open_session(session, alice)
    authenticate(client, alice)

    response = await client.delete(f"/api/v1/auth/sessions/{issued.session_id}")

    assert response.status_code == 204
    _with_refresh(client, issued.refresh_token)
    assert (await client.post(REFRESH_URL)).status_code == 401


async def test_cross_origin_refresh_is_rejected(
    client: AsyncClient, alice_session: IssuedSession
) -> None:
    """CSRF: SameSite covers the default deployment, the Origin allowlist covers the
    cross-site one where the cookie has to be SameSite=None."""
    _with_refresh(client, alice_session.refresh_token)

    response = await client.post(REFRESH_URL, headers={"origin": "https://evil.example"})

    assert response.status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"typ": "refresh"}, id="refresh-token-replayed-as-access"),
        pytest.param({"typ": None}, id="no-type-claim"),
    ],
)
async def test_access_cookie_must_be_a_typed_access_token(
    client: AsyncClient, alice: User, payload: dict[str, str | None]
) -> None:
    """A credential minted for one door must not open another."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(alice.id),
        "sid": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    if payload["typ"] is not None:
        claims["typ"] = payload["typ"]
    token = jwt.encode(claims, settings.SECRET_KEY, algorithm=settings.JWT_ALG)
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)

    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_expired_access_token_says_so(client: AsyncClient, alice: User) -> None:
    """The SPA needs to tell "refresh me" from "you are forged" -- same status, different
    message, so a 401 handler can decide whether to try /auth/refresh."""
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(alice.id),
            "sid": str(uuid.uuid4()),
            "typ": "access",
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
        },
        settings.SECRET_KEY,
        algorithm=settings.JWT_ALG,
    )
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Session expired"


async def test_access_token_is_short_lived(alice: User) -> None:
    """A stateless token is unrevocable, so its lifetime is the revocation window."""
    claims = jwt.decode(
        create_access_token(str(alice.id), str(uuid.uuid4())),
        settings.SECRET_KEY,
        algorithms=[settings.JWT_ALG],
    )
    lifetime_minutes = (claims["exp"] - claims["iat"]) / 60
    assert lifetime_minutes <= 60
