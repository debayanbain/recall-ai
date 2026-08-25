"""Refresh-session data access.

Every lookup here is by token *digest* or by `user_id` -- never by a client-supplied row
id, so there is no object reference for a caller to tamper with.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, update
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.user_session import UserSession


class UserSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_hash(self, token_hash: str) -> UserSession | None:
        """Look a presented refresh token up by digest.

        Retired and revoked rows are returned too: the caller must be able to tell
        "replayed a rotated token" (an incident) from "no such token" (a stale browser).
        """
        result = await self.session.exec(
            select(UserSession).where(UserSession.token_hash == token_hash)
        )
        return result.first()

    async def add(self, row: UserSession) -> UserSession:
        self.session.add(row)
        await self.session.flush()
        await self.session.refresh(row)
        return row

    async def list_active_for_user(self, user_id: uuid.UUID) -> list[UserSession]:
        """Live sessions for one user, newest first. Scoped by `user_id` at the query."""
        now = datetime.now(UTC)
        result = await self.session.exec(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                col(UserSession.revoked_at).is_(None),
                UserSession.expires_at > now,
            )
            .order_by(col(UserSession.last_used_at).desc())
        )
        return list(result.all())

    async def revoke_family(self, family_id: uuid.UUID, reason: str) -> int:
        """Kill an entire rotation chain. Returns the number of live rows closed."""
        result = await self.session.execute(
            update(UserSession)
            .where(
                col(UserSession.family_id) == family_id,
                col(UserSession.revoked_at).is_(None),
            )
            .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
        )
        # execute() is typed as Result; a DML statement always yields a CursorResult.
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def revoke_all_for_user(self, user_id: uuid.UUID, reason: str) -> int:
        """Sign out everywhere. Also the correct response to a deactivated account."""
        result = await self.session.execute(
            update(UserSession)
            .where(
                col(UserSession.user_id) == user_id,
                col(UserSession.revoked_at).is_(None),
            )
            .values(revoked_at=datetime.now(UTC), revoked_reason=reason)
        )
        # execute() is typed as Result; a DML statement always yields a CursorResult.
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    async def delete_expired(self, before: datetime) -> int:
        """Drop rows that can no longer prove anything, keeping the table bounded."""
        result = await self.session.execute(
            delete(UserSession).where(col(UserSession.expires_at) < before)
        )
        # execute() is typed as Result; a DML statement always yields a CursorResult.
        return int(cast("CursorResult[Any]", result).rowcount or 0)
