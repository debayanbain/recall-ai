"""Provider-agnostic OAuth contract.

Every identity provider reduces to the same two steps -- build an authorize URL, then
swap the returned code for an `OAuthIdentity`. Business code (`AuthService`, the auth
router) only ever sees this interface, so adding a provider means adding a module here
and one entry in `registry.py` -- never a new route or a new branch in the service.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


@dataclass(slots=True)
class OAuthIdentity:
    """Normalised result of a completed OAuth exchange."""

    provider: str
    account_id: str
    email: str | None = None
    email_verified: bool = False
    name: str | None = None
    avatar_url: str | None = None
    username: str | None = None
    access_token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None
    scopes: str | None = None

    def __post_init__(self) -> None:
        # An unverified address must never be treated as proof of identity downstream.
        if not self.email:
            self.email = None
            self.email_verified = False


def expiry_from_seconds(expires_in: Any) -> datetime | None:
    """Turn a provider's `expires_in` (seconds, often a string) into an absolute time."""
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return datetime.now(UTC) + timedelta(seconds=seconds)


class OAuthProvider(Protocol):
    """What the auth router needs from a provider. Implementations are stateless."""

    name: str
    #: True when this provider requires RFC 7636 PKCE. No registered provider sets
    #: this today (the parked X/Twitter one does), but the router still honours it.
    uses_pkce: bool

    def is_configured(self) -> bool:
        """False when client id/secret are missing, so login can 404 instead of redirect."""
        ...

    def build_authorize_url(self, state: str, code_challenge: str | None = None) -> str:
        ...

    async def exchange_code(
        self, code: str, code_verifier: str | None = None
    ) -> OAuthIdentity:
        ...
