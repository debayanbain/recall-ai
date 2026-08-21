"""Google OAuth login flow + user provisioning."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.models.user import User
from app.repositories.user import UserRepository

_GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint URL
_GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"


class AuthService:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    @staticmethod
    def build_authorize_url(state: str) -> str:
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{_GOOGLE_AUTH}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Swap auth code for an access token, return Google userinfo."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_resp = await client.post(
                _GOOGLE_TOKEN,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            access_token = token_resp.json()["access_token"]

            info_resp = await client.get(
                _GOOGLE_USERINFO,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            info_resp.raise_for_status()
            userinfo: dict[str, Any] = info_resp.json()
            return userinfo

    async def upsert_user(self, userinfo: dict[str, Any]) -> User:
        provider = "google"
        account_id = userinfo["sub"]

        user = await self.repo.get_by_provider(provider, account_id)
        if user is None:
            user = await self.repo.get_by_email(userinfo["email"])
        if user is None:
            user = await self.repo.create(
                User(
                    email=userinfo["email"],
                    name=userinfo.get("name"),
                    avatar_url=userinfo.get("picture"),
                    auth_provider=provider,
                    provider_account_id=account_id,
                )
            )
        else:
            user.auth_provider = provider
            user.provider_account_id = account_id
            user.name = user.name or userinfo.get("name")
            user.avatar_url = userinfo.get("picture") or user.avatar_url
            await self.repo.create(user)
        return user
