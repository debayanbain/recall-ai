"""JWT session tokens and password-less identity helpers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings


def create_session_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    """Create a signed JWT for the given user id (subject)."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALG)


def decode_session_token(token: str) -> dict[str, Any]:
    """Decode and verify a session token. Raises jwt.PyJWTError on failure."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALG])
