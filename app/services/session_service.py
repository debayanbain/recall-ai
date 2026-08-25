"""Persistent login sessions: issue, rotate, revoke.

The rules this service exists to enforce:

1. **Rotation on every use.** A refresh token is single-use. Presenting it mints a new
   one and retires the old, so a token captured from a log, a proxy or a backup is only
   valuable until the legitimate client next refreshes.
2. **Replay is an incident, not an error.** A token that was already rotated away is
   evidence that two parties hold it. There is no way to tell the thief from the victim,
   so the entire chain is revoked and both are sent back through the provider -- the
   standard answer (OAuth 2.0 BCP, §4.13.2) and the reason retired rows are kept.
3. **Sliding, but bounded.** Each rotation extends the window by
   `REFRESH_TOKEN_EXPIRE_DAYS`, so an active user never sees a login screen; the chain
   still dies at `REFRESH_TOKEN_ABSOLUTE_DAYS` no matter how active it is.
4. **Fail closed.** Every unexpected condition -- unknown token, revoked row, expired
   row, vanished or soft-deleted user -- ends in `SessionError`, never in a token.

Nothing here reads a request or writes a cookie; the router owns that. Nothing here
commits either -- `get_session` commits at the request boundary.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    hash_refresh_token,
    new_refresh_token,
)
from app.models.user import User
from app.models.user_session import UserSession
from app.repositories.user import UserRepository
from app.repositories.user_session import UserSessionRepository

log = get_logger("session")

# A User-Agent is unbounded attacker-controlled text; it is only ever shown back to its
# own owner, but there is no reason to store a megabyte of it.
_MAX_USER_AGENT = 256


class SessionError(Exception):
    """A refresh attempt that must end in 401 and a cleared cookie.

    `reason` is for logs and metrics only. The router deliberately collapses every value
    into one opaque client-facing message: telling a caller *why* a token was rejected
    tells a token thief whether the token ever existed.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class IssuedSession:
    """What the router needs to set two cookies. `refresh_token` is raw, and this is the
    only moment it exists outside the browser -- only its digest is persisted."""

    access_token: str
    refresh_token: str
    session_id: uuid.UUID
    access_expires_in: int
    refresh_expires_at: datetime


def _aware(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    Columns are `timestamptz` so this should never fire, but a comparison between naive
    and aware datetimes raises `TypeError` -- and an exception inside the refresh path
    would log every user out.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SessionService:
    def __init__(self, sessions: UserSessionRepository, users: UserRepository) -> None:
        self.sessions = sessions
        self.users = users

    # --- issuing -------------------------------------------------------------

    async def start(
        self,
        user: User,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedSession:
        """Open a new session after a successful provider login. Starts a new family."""
        now = datetime.now(UTC)
        family_id = uuid.uuid4()
        issued = await self._issue(
            user_id=user.id,
            family_id=family_id,
            family_started_at=now,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        log.info("session_started", user_id=str(user.id), session_id=str(issued.session_id))
        return issued

    async def _issue(
        self,
        *,
        user_id: uuid.UUID,
        family_id: uuid.UUID,
        family_started_at: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> IssuedSession:
        now = datetime.now(UTC)
        raw = new_refresh_token()
        expires_at = now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        row = await self.sessions.add(
            UserSession(
                user_id=user_id,
                family_id=family_id,
                token_hash=hash_refresh_token(raw),
                expires_at=expires_at,
                family_started_at=family_started_at,
                user_agent=user_agent[:_MAX_USER_AGENT] if user_agent else None,
                ip_address=ip_address,
                last_used_at=now,
                created_at=now,
            )
        )
        return IssuedSession(
            access_token=create_access_token(str(user_id), str(row.id)),
            refresh_token=raw,
            session_id=row.id,
            access_expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            refresh_expires_at=expires_at,
        )

    # --- rotating ------------------------------------------------------------

    async def refresh(
        self,
        raw_token: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedSession:
        """Trade a refresh token for a new pair. Single-use: the old token dies here."""
        row = await self.sessions.get_by_hash(hash_refresh_token(raw_token))
        if row is None:
            # Either never issued, or already pruned. Nothing to revoke.
            raise SessionError("unknown_token")

        if row.revoked_at is not None:
            # A retired token came back. If it was retired by rotation, someone is
            # holding a copy -- kill the chain, both halves of it.
            if row.revoked_reason == "rotated":
                revoked = await self.sessions.revoke_family(row.family_id, "reuse_detected")
                log.warning(
                    "session_reuse_detected",
                    user_id=str(row.user_id),
                    family_id=str(row.family_id),
                    revoked=revoked,
                )
            raise SessionError("revoked")

        now = datetime.now(UTC)
        if _aware(row.expires_at) <= now:
            raise SessionError("expired")

        absolute_deadline = _aware(row.family_started_at) + timedelta(
            days=settings.REFRESH_TOKEN_ABSOLUTE_DAYS
        )
        if now >= absolute_deadline:
            await self.sessions.revoke_family(row.family_id, "absolute_expiry")
            raise SessionError("absolute_expiry")

        user = await self.users.get(row.user_id)
        if user is None or user.deleted_at is not None:
            # A deleted account must not be resurrected by a token minted before it went.
            await self.sessions.revoke_family(row.family_id, "user_gone")
            raise SessionError("user_gone")

        issued = await self._issue(
            user_id=row.user_id,
            family_id=row.family_id,
            family_started_at=_aware(row.family_started_at),
            # Carried forward so the device list keeps naming the device, not the
            # refresh call. A changed User-Agent is not treated as suspicious: browsers
            # rewrite it on every update.
            user_agent=row.user_agent,
            ip_address=ip_address or row.ip_address,
        )
        row.revoked_at = now
        row.revoked_reason = "rotated"
        row.replaced_by_id = issued.session_id
        row.last_used_at = now
        await self.sessions.add(row)
        return issued

    # --- revoking ------------------------------------------------------------

    async def revoke(self, raw_token: str | None, reason: str = "logout") -> None:
        """Sign out one device. Silent on an unknown token -- logout must not 401."""
        if not raw_token:
            return
        row = await self.sessions.get_by_hash(hash_refresh_token(raw_token))
        if row is None or row.revoked_at is not None:
            return
        # The whole family, not just this row: the retired rows in the chain are exactly
        # what a replay would use, and a logged-out session should have none of them left.
        await self.sessions.revoke_family(row.family_id, reason)
        log.info("session_revoked", user_id=str(row.user_id), reason=reason)

    async def revoke_all(self, user_id: uuid.UUID, reason: str = "logout_all") -> int:
        count = await self.sessions.revoke_all_for_user(user_id, reason)
        log.info("sessions_revoked_all", user_id=str(user_id), count=count, reason=reason)
        return count

    async def revoke_one(self, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
        """Revoke a session named by the *owner* from their device list.

        The ownership check is here rather than in the router, and the miss is reported
        as a plain False so the router can answer 404 for both "not yours" and "not
        there" -- a caller must not be able to probe for other users' session ids.
        """
        rows = await self.sessions.list_active_for_user(user_id)
        target = next((r for r in rows if r.id == session_id), None)
        if target is None:
            return False
        await self.sessions.revoke_family(target.family_id, "revoked_by_user")
        return True

    async def list_sessions(self, user_id: uuid.UUID) -> list[UserSession]:
        return await self.sessions.list_active_for_user(user_id)
