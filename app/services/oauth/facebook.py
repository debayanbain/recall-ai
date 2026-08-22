"""Facebook Login (OAuth 2.0).

Also the entry point for Instagram: once the Instagram product is added to the same
Meta app, the very token stored here gains `instagram_basic` / `pages_show_list` and
can read the user's Instagram content -- which is why tokens are persisted at all.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services.meta_graph import GRAPH_TIMEOUT as _TIMEOUT
from app.services.meta_graph import graph_url as _graph
from app.services.meta_graph import signed_params
from app.services.oauth.base import OAuthIdentity, expiry_from_seconds


class FacebookOAuthProvider:
    name = "facebook"
    uses_pkce = False

    def is_configured(self) -> bool:
        return bool(settings.FACEBOOK_CLIENT_ID and settings.FACEBOOK_CLIENT_SECRET)

    def build_authorize_url(self, state: str, code_challenge: str | None = None) -> str:
        params = {
            "client_id": settings.FACEBOOK_CLIENT_ID,
            "redirect_uri": settings.FACEBOOK_REDIRECT_URI,
            "response_type": "code",
            "scope": settings.FACEBOOK_SCOPES,
            "state": state,
        }
        return f"https://www.facebook.com/{settings.FACEBOOK_API_VERSION}/dialog/oauth?{urlencode(params)}"

    async def exchange_code(
        self, code: str, code_verifier: str | None = None
    ) -> OAuthIdentity:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            token_resp = await client.get(
                _graph("oauth/access_token"),
                params={
                    "client_id": settings.FACEBOOK_CLIENT_ID,
                    "client_secret": settings.FACEBOOK_CLIENT_SECRET,
                    "redirect_uri": settings.FACEBOOK_REDIRECT_URI,
                    "code": code,
                },
            )
            token_resp.raise_for_status()
            token: dict[str, Any] = token_resp.json()
            access_token = token["access_token"]

            info_resp = await client.get(
                _graph("me"),
                params=signed_params(
                    access_token, fields="id,name,email,picture.type(large)"
                ),
            )
            info_resp.raise_for_status()
            info: dict[str, Any] = info_resp.json()

        picture = info.get("picture", {}).get("data", {}).get("url")
        email = info.get("email")
        return OAuthIdentity(
            provider=self.name,
            account_id=str(info["id"]),
            # Meta only ever returns an address it has verified; absent means the account
            # is phone-only, and the caller falls back to a synthetic address.
            email=email,
            email_verified=bool(email),
            name=info.get("name"),
            avatar_url=picture,
            access_token=access_token,
            # Facebook issues no refresh token; long-lived exchange is a separate call.
            refresh_token=None,
            expires_at=expiry_from_seconds(token.get("expires_in")),
            scopes=settings.FACEBOOK_SCOPES,
        )
