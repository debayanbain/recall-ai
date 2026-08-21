"""Shared FastAPI dependencies: DB session, current user, service wiring."""
from __future__ import annotations

import uuid
from typing import Annotated

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.security import decode_session_token
from app.db.session import get_session
from app.models.user import User
from app.repositories.collection import CollectionRepository
from app.repositories.user import UserRepository
from app.repositories.vault import VaultRepository
from app.services.auth_service import AuthService
from app.services.collection_service import CollectionService
from app.services.vault_service import VaultService

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_current_user(
    session: SessionDep,
    token: Annotated[str | None, Cookie(alias=settings.SESSION_COOKIE_NAME)] = None,
) -> User:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        payload = decode_session_token(token)
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session") from exc

    user = await UserRepository(session).get(user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_vault_service(session: SessionDep) -> VaultService:
    return VaultService(VaultRepository(session))


def get_collection_service(session: SessionDep) -> CollectionService:
    return CollectionService(CollectionRepository(session))


def get_auth_service(session: SessionDep) -> AuthService:
    return AuthService(UserRepository(session))


VaultServiceDep = Annotated[VaultService, Depends(get_vault_service)]
CollectionServiceDep = Annotated[CollectionService, Depends(get_collection_service)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
