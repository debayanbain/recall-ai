"""Shared FastAPI dependencies: DB session, current user, service wiring."""
from __future__ import annotations

import uuid
from typing import Annotated

import jwt
import structlog
from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.user import User
from app.repositories.collection import CollectionRepository
from app.repositories.instagram_account import InstagramAccountRepository
from app.repositories.oauth_account import OAuthAccountRepository
from app.repositories.telegram import (
    TelegramAccountRepository,
    TelegramLinkTokenRepository,
)
from app.repositories.user import UserRepository
from app.repositories.user_session import UserSessionRepository
from app.repositories.vault import VaultRepository
from app.services.auth_service import AuthService
from app.services.collection_service import CollectionService
from app.services.instagram_service import InstagramService
from app.services.session_service import SessionService
from app.services.telegram.linking import TelegramLinkService
from app.services.vault_service import VaultService
from app.storage import get_storage

log = get_logger("deps")

SessionDep = Annotated[AsyncSession, Depends(get_session)]


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

    user = await UserRepository(session).get(user_id)
    # A soft-deleted account keeps its row, so the deleted_at check has to happen here or
    # a token minted before deletion would keep working until it expired.
    if user is None or user.deleted_at is not None:
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


def get_collection_service(session: SessionDep) -> CollectionService:
    return CollectionService(CollectionRepository(session), VaultRepository(session))


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
CollectionServiceDep = Annotated[CollectionService, Depends(get_collection_service)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
InstagramServiceDep = Annotated[InstagramService, Depends(get_instagram_service)]
TelegramLinkServiceDep = Annotated[
    TelegramLinkService, Depends(get_telegram_link_service)
]
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
