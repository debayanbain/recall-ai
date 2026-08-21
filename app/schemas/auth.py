"""Auth/user DTOs."""
from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, EmailStr

from app.models.base import Plan


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    name: str | None
    avatar_url: str | None
    plan: Plan
