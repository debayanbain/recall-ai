"""X (Twitter) OAuth 2.0 with PKCE. PARKED -- NOT part of the running app.

Lives outside `app/` on purpose: it references TWITTER_* settings that were removed
from `core/config.py`, so inside the package it would fail `mypy app`. Kept verbatim
so re-enabling is a copy-back rather than a rewrite.

To re-enable:
  1. move this file to `app/services/oauth/twitter.py`
  2. restore TWITTER_CLIENT_ID / _SECRET / _REDIRECT_URI / _SCOPES in `core/config.py`
     and add the pair to `enabled_oauth_providers` + the `any_oauth` boot guard
  3. add the import and the `"twitter"` entry to `services/oauth/registry.py`
  4. add `"twitter": "X (Twitter)"` to `_PROVIDER_LABELS` in `api/v1/auth.py`
  5. frontend: re-add `twitter: XMark` to MARKS in `components/oauth-buttons.tsx`
     and the label in `lib/session.ts`

X requires a Confidential client (HTTP Basic on the token endpoint) and never
returns an email, so its users land on a synthetic placeholder address.


X requires PKCE even for confidential clients, and the token endpoint expects the
client id/secret as HTTP Basic auth. It never returns an email -- `OAuthIdentity.email`
is always None here and `AuthService` synthesises a non-routable placeholder.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.services.oauth.base import OAuthIdentity, expiry_from_seconds

_AUTHORIZE = "https://x.com/i/oauth2/authorize"
_TOKEN = "https://api.x.com/2/oauth2/token"  # noqa: S105 - public endpoint URL
_ME = "https://api.x.com/2/users/me"
_TIMEOUT = 15.0


class TwitterOAuthProvider:
    name = "twitter"
    uses_pkce = True

    def is_configured(self) -> bool:
        return bool(settings.TWITTER_CLIENT_ID and settings.TWITTER_CLIENT_SECRET)

    def build_authorize_url(self, state: str, code_challenge: str | None = None) -> str:
        if not code_challenge:
            raise ValueError("Twitter OAuth requires a PKCE code challenge")
        params = {
            "client_id": settings.TWITTER_CLIENT_ID,
            "redirect_uri": settings.TWITTER_REDIRECT_URI,
            "response_type": "code",
            "scope": settings.TWITTER_SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{_AUTHORIZE}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, code_verifier: str | None = None
    ) -> OAuthIdentity:
        if not code_verifier:
            raise ValueError("Twitter OAuth requires the PKCE code verifier")
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            token_resp = await client.post(
                _TOKEN,
                data={
                    "code": code,
                    "grant_type": "authorization_code",
                    "client_id": settings.TWITTER_CLIENT_ID,
                    "redirect_uri": settings.TWITTER_REDIRECT_URI,
                    "code_verifier": code_verifier,
                },
                auth=(settings.TWITTER_CLIENT_ID, settings.TWITTER_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            token_resp.raise_for_status()
            token: dict[str, Any] = token_resp.json()

            info_resp = await client.get(
                _ME,
                params={"user.fields": "profile_image_url,username,name"},
                headers={"Authorization": f"Bearer {token['access_token']}"},
            )
            info_resp.raise_for_status()
            info: dict[str, Any] = info_resp.json().get("data", {})

        avatar = info.get("profile_image_url")
        if avatar:
            # X returns the 48px "_normal" crop; the 400px variant is the same URL.
            avatar = avatar.replace("_normal.", "_400x400.")

        return OAuthIdentity(
            provider=self.name,
            account_id=str(info["id"]),
            email=None,
            email_verified=False,
            name=info.get("name"),
            avatar_url=avatar,
            username=info.get("username"),
            access_token=token.get("access_token"),
            refresh_token=token.get("refresh_token"),
            expires_at=expiry_from_seconds(token.get("expires_in")),
            scopes=token.get("scope") or settings.TWITTER_SCOPES,
        )
