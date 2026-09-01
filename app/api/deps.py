"""Shared FastAPI dependencies: DB session, current user, service wiring."""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Annotated, Any

import jwt
import structlog
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.user import User
from app.repositories.instagram_account import InstagramAccountRepository
from app.repositories.oauth_account import OAuthAccountRepository
from app.repositories.space import SpaceRepository
from app.repositories.telegram import (
    TelegramAccountRepository,
    TelegramLinkTokenRepository,
)
from app.repositories.user import UserRepository
from app.repositories.user_session import UserSessionRepository
from app.repositories.vault import VaultRepository
from app.services.auth_service import AuthService
from app.services.instagram_service import InstagramService
from app.services.session_service import SessionService
from app.services.space_service import SpaceService
from app.services.telegram.linking import TelegramLinkService
from app.services.vault_service import VaultService
from app.storage import get_storage

log = get_logger("deps")

SessionDep = Annotated[AsyncSession, Depends(get_session)]

#: Verified `users` rows, keyed by the *digest* of the access token that resolved them.
#:
#: Every authenticated request re-read the same row, and against a database in another
#: region that read is a full round trip -- ~290ms measured, which was the entire cost of
#: endpoints like `/vault/uploads/limits` that otherwise touch nothing. The row is
#: immutable for the life of an access token in every path that reads it (nothing under
#: `get_current_user` writes to the user; the OAuth callback that does mutate a user does
#: not go through this dependency), so re-reading it bought nothing but latency.
#:
#: Three properties make this safe rather than a revocation hole:
#:  * It is keyed by token digest, not by user id -- a different token, including one
#:    minted after a password-equivalent change, never reads another token's entry.
#:  * The TTL is `AUTH_USER_CACHE_SECONDS`, capped at a quarter of the access token
#:    lifetime by the boot guard. Nothing already consults the database while an access
#:    token is valid, so deletion and `logout-all` already take up to
#:    ACCESS_TOKEN_EXPIRE_MINUTES; this cannot extend that window, only sit inside it.
#:  * Deletion is invalidated explicitly through `forget_cached_user`, so the account
#:    routes that soft-delete a user do not have to wait for the TTL.
#:
#: The stored value is a plain snapshot, not the ORM instance: an object bound to a closed
#: session would raise on attribute access from the next request, and handing the same
#: identity-mapped object to two concurrent requests is a data race.
_user_cache: dict[str, tuple[float, dict[str, Any]]] = {}

#: Bound on the cache so an attacker replaying many forged-but-valid-looking tokens
#: cannot grow it without limit. Only *successfully verified* tokens are ever inserted,
#: so reaching this means real concurrent sessions; the whole map is dropped rather than
#: evicted one by one, because the cost of a miss is a single query.
_USER_CACHE_MAX = 2048

#: Columns copied into the snapshot. An explicit list rather than `__dict__`, so a column
#: added later is absent (and re-read from the database) instead of silently stale.
_USER_CACHE_FIELDS = (
    "id",
    "email",
    "name",
    "avatar_url",
    "auth_provider",
    "provider_account_id",
    "email_verified",
    "plan",
    "created_at",
    "updated_at",
    "deleted_at",
)


def _cache_key(token: str) -> str:
    """Digest, never the raw token: this dict is reachable from a stack trace or a heap
    dump, and a raw session token there is a credential at rest."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cached_user(key: str) -> User | None:
    entry = _user_cache.get(key)
    if entry is None:
        return None
    expires_at, snapshot = entry
    if expires_at <= time.monotonic():
        _user_cache.pop(key, None)
        return None
    # Rebuilt per request and never added to a session: this instance is a read-only view
    # of the row, and `model_construct` skips validation because the values came out of
    # the database, not off the wire.
    return User.model_construct(**snapshot)


def _remember_user(key: str, user: User) -> None:
    ttl = settings.AUTH_USER_CACHE_SECONDS
    if ttl <= 0:
        return
    if len(_user_cache) >= _USER_CACHE_MAX:
        _user_cache.clear()
    snapshot = {
        name: getattr(user, name) for name in _USER_CACHE_FIELDS if hasattr(user, name)
    }
    _user_cache[key] = (time.monotonic() + ttl, snapshot)


def forget_cached_user(user_id: uuid.UUID) -> None:
    """Drop every cached entry for one account.

    Called when a user is soft-deleted so the change is visible on the next request
    rather than after the TTL. Linear over a map bounded at `_USER_CACHE_MAX`, and it
    runs on account deletion only.
    """
    for key, (_, snapshot) in list(_user_cache.items()):
        if snapshot.get("id") == user_id:
            _user_cache.pop(key, None)


def clear_user_cache() -> None:
    """Drop the whole cache. For tests, which change users behind the dependency."""
    _user_cache.clear()


async def get_current_user(
    session: SessionDep,
    token: Annotated[str | None, Cookie(alias=settings.SESSION_COOKIE_NAME)] = None,
) -> User:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except jwt.ExpiredSignatureError as exc:
        # Distinguished from every other failure on purpose: this is the one case where
        # the client should silently POST /auth/refresh and retry, and it is not a
        # security signal (the signature was valid). Any *other* JWT failure means a
        # forged or malformed token and gets the flat message below.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Session expired",
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from exc
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session") from exc

    # The signature is verified above on every request; only the *row lookup* is cached,
    # and only for a token that has already proved itself. A forged or expired token never
    # reaches this line.
    key = _cache_key(token)
    user = _cached_user(key)
    if user is None:
        user = await UserRepository(session).get(user_id)
        # A soft-deleted account keeps its row, so the deleted_at check has to happen here
        # or a token minted before deletion would keep working until it expired. Only a
        # live user is ever cached, so a cache hit cannot skip this check.
        if user is None or user.deleted_at is not None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session")
        # Belt and braces: the entry is keyed by token digest, and the token's own `sub`
        # is what produced this row -- but a mismatch would be a cross-account read, so it
        # is asserted rather than assumed.
        if user.id != user_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session")
        _remember_user(key, user)
    elif user.id != user_id:
        # Cannot happen (the key is the digest of this very token); a stale or colliding
        # entry must fail closed rather than answer with another account's row.
        _user_cache.pop(key, None)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session")
    # Every log line emitted for the rest of this request carries the user id, which is
    # what makes the centralized store answerable to "what did this account do?".
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]



def assert_same_site(request: Request) -> None:
    """Reject a cross-origin state change on a cookie-authenticated endpoint.

    SameSite already blocks this for the default `lax` cookie, but a deployment whose SPA
    and API are genuinely cross-site must set `SESSION_COOKIE_SAMESITE=none`, and that
    hands the browser back to the attacker's page. An Origin allowlist is the check that
    survives both settings. A request with no Origin at all (curl, a native app) is
    allowed: no browser omits Origin on a cross-site POST, so absence is not the attack.

    Lives here rather than in one router because it guards a property of the session
    cookie, not of any single feature.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = {*settings.CORS_ORIGINS, settings.FRONTEND_URL.rstrip("/")}
    if origin.rstrip("/") not in {a.rstrip("/") for a in allowed}:
        log.warning("session_cross_origin_rejected", origin=origin, path=request.url.path)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request rejected")


def get_vault_service(session: SessionDep) -> VaultService:
    # `get_storage()` returns None when no bucket is configured; the service degrades to
    # text-only uploads rather than the API failing to start.
    return VaultService(VaultRepository(session), get_storage())


def get_space_service(session: SessionDep) -> SpaceService:
    # The vault repository comes in beside the space one because adding a memory to a
    # Space has to verify the *memory's* owner, not just the Space's. Owning the
    # container has never granted access to someone else's content.
    return SpaceService(SpaceRepository(session), VaultRepository(session))


def get_instagram_service(session: SessionDep) -> InstagramService:
    return InstagramService(InstagramAccountRepository(session))


def get_telegram_link_service(session: SessionDep) -> TelegramLinkService:
    return TelegramLinkService(
        TelegramAccountRepository(session), TelegramLinkTokenRepository(session)
    )


def get_auth_service(session: SessionDep) -> AuthService:
    return AuthService(UserRepository(session), OAuthAccountRepository(session))


def get_session_service(session: SessionDep) -> SessionService:
    return SessionService(UserSessionRepository(session), UserRepository(session))


VaultServiceDep = Annotated[VaultService, Depends(get_vault_service)]
SpaceServiceDep = Annotated[SpaceService, Depends(get_space_service)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
InstagramServiceDep = Annotated[InstagramService, Depends(get_instagram_service)]
TelegramLinkServiceDep = Annotated[
    TelegramLinkService, Depends(get_telegram_link_service)
]
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
