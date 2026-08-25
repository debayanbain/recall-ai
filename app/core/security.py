"""Session credentials: short-lived access JWTs and opaque refresh tokens.

Two very different things live here, and the split is the whole point of the design:

* **Access token** -- a signed JWT in the `recall_session` cookie, minutes long. It is
  never checked against the database, which is what keeps every authenticated request a
  pure signature verification. The price is that revocation is not instant: a revoked
  session can still be used until its access token expires, which is why the lifetime is
  measured in minutes rather than days.
* **Refresh token** -- 32+ bytes of CSPRNG output, opaque, stored only as a SHA-256
  digest in `user_sessions`. It is a *database-backed* credential, so revoking it is
  immediate, and rotation on every use makes theft detectable.

The refresh token is deliberately not a JWT: a self-contained long-lived bearer token
cannot be revoked, and revocation is the entire reason the refresh half exists.

SHA-256 (not bcrypt/argon2) is the right digest here precisely because the input is not
a password: it is full-entropy random, so there is no dictionary to run and no work
factor to buy. A fast digest also lets the lookup be a single indexed equality query
instead of a scan-and-verify over every row.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.config import settings

ACCESS_TOKEN_TYPE = "access"
# 48 bytes -> 64 url-safe chars. Far past any brute-force reach, and short enough to sit
# in a cookie without crowding the 4 KB budget.
_REFRESH_TOKEN_BYTES = 48


def create_access_token(
    subject: str, session_id: str, extra: dict[str, Any] | None = None
) -> str:
    """Sign a short-lived access JWT bound to one server-side session row.

    `sid` ties the token to the `user_sessions` row it was minted from, so a token that
    outlives its session can be recognised as stale (and so an audit trail can follow a
    request back to the device that started the session).
    """
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "sid": session_id,
        "typ": ACCESS_TOKEN_TYPE,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALG)


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify an access token. Raises `jwt.PyJWTError` on anything unacceptable.

    The algorithm is pinned to a single value from configuration: passing the token's own
    `alg` header back to `jwt.decode` is the classic `alg: none` / RS256->HS256 confusion
    bypass. `require` makes a token missing `exp` or `sub` a rejection rather than a
    token that never expires.
    """
    payload: dict[str, Any] = jwt.decode(
        token,
        settings.SECRET_KEY,
        algorithms=[settings.JWT_ALG],
        options={"require": ["exp", "iat", "sub"]},
    )
    # A refresh credential must never be usable as an access credential. Refresh tokens
    # are not JWTs today, so this only guards against a future token type being replayed
    # at the wrong door -- which is exactly the kind of change that ships without anyone
    # re-reading this function.
    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def new_refresh_token() -> str:
    """A fresh opaque refresh token. Returned to the browser once, never stored raw."""
    return secrets.token_urlsafe(_REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """Digest stored in `user_sessions.token_hash`.

    A database dump therefore yields no usable session credential -- the same reason
    passwords are never stored raw.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
