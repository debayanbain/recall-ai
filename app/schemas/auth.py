"""Auth/user DTOs."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.base import Plan


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    # False for accounts created through a provider that never released a verified
    # address (a phone-only Facebook account) -- their `email` is a synthetic
    # placeholder on the non-routable users.noreply.recall.invalid domain.
    email_verified: bool = False
    name: str | None
    avatar_url: str | None
    plan: Plan
    linked_providers: list[str] = Field(default_factory=list)


class OAuthProviderInfo(BaseModel):
    id: str
    label: str
    login_url: str


class ProvidersResponse(BaseModel):
    providers: list[OAuthProviderInfo]


class RefreshResponse(BaseModel):
    """Answer to POST /auth/refresh. The tokens themselves travel as HttpOnly cookies and
    deliberately never appear in a body -- a body is readable by script, a cookie is not.

    `expires_in` lets the SPA schedule a refresh slightly before the access token dies
    instead of discovering it through a failed request.
    """

    ok: bool = True
    expires_in: int
    refresh_expires_at: datetime


class SessionRead(BaseModel):
    """One live device in the user's own session list.

    Carries no credential: `id` is a row id whose only power is `DELETE /auth/sessions/
    {id}`, and that is ownership-checked.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    current: bool = False
    user_agent: str | None = None
    ip_address: str | None = None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
