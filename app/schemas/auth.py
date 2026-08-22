"""Auth/user DTOs."""
from __future__ import annotations

import uuid

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
