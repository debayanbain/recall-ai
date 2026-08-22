"""Instagram Login — sign in with an Instagram account directly.

This is **not** the Facebook-based Instagram connection in `services/instagram_service.py`.
That one links a Business account to an already-signed-in user; this one establishes
identity. They use different credentials: this needs the *Instagram* App ID/Secret from
the Meta app's Instagram product ("Instagram API setup with Instagram login"), not the
Facebook App ID.

Instagram Basic Display was shut down on 2024-12-04. Its replacement, Instagram Login,
differs in three ways worth remembering:

* Three hosts, not one -- `instagram.com` authorizes, `api.instagram.com` mints the
  short-lived token, `graph.instagram.com` exchanges it and answers `/me`.
* The returned `code` carries a literal `#_` suffix that must be stripped, or the token
  exchange fails with an opaque error.
* No email, ever. `AuthService` gives these users a synthetic placeholder address.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services.oauth.base import OAuthIdentity, expiry_from_seconds

_AUTHORIZE = "https://www.instagram.com/oauth/authorize"
_TOKEN = "https://api.instagram.com/oauth/access_token"  # noqa: S105 - public endpoint URL
_EXCHANGE = "https://graph.instagram.com/access_token"  # noqa: S105 - public endpoint URL
_ME = "https://graph.instagram.com/v21.0/me"
_TIMEOUT = 15.0


class InstagramOAuthProvider:
    name = "instagram"
    uses_pkce = False

    def is_configured(self) -> bool:
        # The redirect URI is part of the contract: Instagram rejects a mismatch, and it
        # has no usable default because it must be https.
        return bool(
            settings.INSTAGRAM_APP_ID
            and settings.INSTAGRAM_APP_SECRET
            and settings.INSTAGRAM_LOGIN_REDIRECT_URI
        )

    def build_authorize_url(self, state: str, code_challenge: str | None = None) -> str:
        params = {
            "client_id": settings.INSTAGRAM_APP_ID,
            "redirect_uri": settings.INSTAGRAM_LOGIN_REDIRECT_URI,
            "response_type": "code",
            "scope": settings.INSTAGRAM_LOGIN_SCOPES,
            "state": state,
        }
        return f"{_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, code_verifier: str | None = None
    ) -> OAuthIdentity:
        # Instagram appends "#_" to the code on redirect. Left in place, the exchange
        # fails with a generic "Invalid authorization code".
        code = code.removesuffix("#_")

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            token_resp = await client.post(
                _TOKEN,
                data={
                    "client_id": settings.INSTAGRAM_APP_ID,
                    "client_secret": settings.INSTAGRAM_APP_SECRET,
                    "grant_type": "authorization_code",
                    "redirect_uri": settings.INSTAGRAM_LOGIN_REDIRECT_URI,
                    "code": code,
                },
            )
            token_resp.raise_for_status()
            short: dict[str, Any] = token_resp.json()
            short_token = short["access_token"]

            # Short-lived tokens last ~1 hour, which is useless for anything later. Trade
            # up to the 60-day token before storing.
            long_resp = await client.get(
                _EXCHANGE,
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": settings.INSTAGRAM_APP_SECRET,
                    "access_token": short_token,
                },
            )
            long_resp.raise_for_status()
            long: dict[str, Any] = long_resp.json()
            access_token = long.get("access_token", short_token)

            info_resp = await client.get(
                _ME,
                params={
                    "fields": "user_id,username,name,profile_picture_url,account_type",
                    "access_token": access_token,
                },
            )
            info_resp.raise_for_status()
            info: dict[str, Any] = info_resp.json()

        # `user_id` is the stable app-scoped id; `id` is legacy and not always present.
        account_id = str(info.get("user_id") or info["id"])
        return OAuthIdentity(
            provider=self.name,
            account_id=account_id,
            # Instagram Login never returns an email under any scope.
            email=None,
            email_verified=False,
            name=info.get("name") or info.get("username"),
            avatar_url=info.get("profile_picture_url"),
            username=info.get("username"),
            access_token=access_token,
            refresh_token=None,
            expires_at=expiry_from_seconds(long.get("expires_in")),
            scopes=settings.INSTAGRAM_LOGIN_SCOPES,
        )
