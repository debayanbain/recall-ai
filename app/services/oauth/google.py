"""Google OAuth 2.0 (OpenID Connect)."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services.oauth.base import OAuthIdentity, expiry_from_seconds

_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105 - public endpoint URL
_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
_TIMEOUT = 15.0


class GoogleOAuthProvider:
    name = "google"
    uses_pkce = False

    def is_configured(self) -> bool:
        return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)

    def build_authorize_url(self, state: str, code_challenge: str | None = None) -> str:
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, code_verifier: str | None = None
    ) -> OAuthIdentity:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            token_resp = await client.post(
                _TOKEN,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            token_resp.raise_for_status()
            token: dict[str, Any] = token_resp.json()

            info_resp = await client.get(
                _USERINFO, headers={"Authorization": f"Bearer {token['access_token']}"}
            )
            info_resp.raise_for_status()
            info: dict[str, Any] = info_resp.json()

        return OAuthIdentity(
            provider=self.name,
            account_id=str(info["sub"]),
            email=info.get("email"),
            # Google states verification explicitly; treat a missing flag as unverified.
            email_verified=bool(info.get("email_verified")),
            name=info.get("name"),
            avatar_url=info.get("picture"),
            access_token=token.get("access_token"),
            refresh_token=token.get("refresh_token"),
            expires_at=expiry_from_seconds(token.get("expires_in")),
            scopes=token.get("scope"),
        )
