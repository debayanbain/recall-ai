"""Connected-account DTOs.

Deliberately token-free: `InstagramAccount` holds a Page access token that can read the
user's Instagram content, and no shape here has a field for it. Adding one would leak a
long-lived credential into the browser.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class InstagramAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    instagram_user_id: str
    username: str | None
    name: str | None
    profile_picture_url: str | None
    page_name: str | None
    token_expires_at: datetime | None
    created_at: datetime


class InstagramConnectionsResponse(BaseModel):
    #: False when the deployment has no Meta app configured, so the UI can explain
    #: rather than offer a button that cannot work.
    available: bool
    accounts: list[InstagramAccountRead]
