"""Telegram connection payloads.

Deliberately identifier-free. `telegram_chat_id` is the address the bot sends replies to,
so it never crosses into the browser: the same rule that keeps Page tokens out of
`schemas/integrations.py` and `storage_key` out of `schemas/vault.py`. `telegram_user_id`
is exposed because the settings page has nothing else to show for an account with no
username, and it is not an authorisation token on its own.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TelegramAccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    telegram_user_id: str
    username: str | None
    first_name: str | None
    linked_at: datetime


class TelegramConnectionResponse(BaseModel):
    """`available` mirrors InstagramConnectionsResponse: the server may have no bot."""

    available: bool
    bot_username: str | None
    account: TelegramAccountRead | None


class TelegramLinkResponse(BaseModel):
    """A freshly minted, single-use deep link. Shown once and never stored client-side."""

    deep_link: str
    expires_in: int
    expires_at: datetime
