"""Connect an Instagram Business account through Facebook Login.

This is the "Instagram side of Facebook": the same Meta app, a second consent round with
extra scopes. Instagram Basic Display was shut down in December 2024, so the only way to
read a user's Instagram media is via the Instagram Graph API, which requires:

  * an Instagram **Business or Creator** account,
  * linked to a **Facebook Page**,
  * whose Page access token authorises the reads.

The flow is therefore: consent -> short-lived user token -> long-lived user token ->
list the user's Pages -> keep the ones with a linked IG account, with each Page's own
token. Page tokens minted from a long-lived user token do not expire, which is what
makes background ingestion possible later.

Why this is separate from sign-in: `instagram_basic`, `pages_show_list` and
`business_management` all need Meta App Review, and asking for business-Page access on a
sign-in screen is both a conversion disaster and more authority than logging in needs.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.core.crypto import encrypt_token
from app.core.logging import get_logger
from app.models.instagram_account import InstagramAccount
from app.repositories.instagram_account import InstagramAccountRepository
from app.services.meta_graph import (
    GRAPH_TIMEOUT,
    graph_error_message,
    graph_url,
    signed_params,
)
from app.services.oauth.base import expiry_from_seconds

log = get_logger("instagram")


class InstagramConnectError(RuntimeError):
    """A connect attempt failed for a reason worth telling the user about.

    `code` is a short, fixed token the frontend maps to copy. It never carries text from
    Meta, so nothing attacker- or upstream-controlled reaches the browser.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class InstagramService:
    def __init__(self, repo: InstagramAccountRepository) -> None:
        self.repo = repo

    @staticmethod
    def is_configured() -> bool:
        # Rides the Facebook app, so it is configured exactly when Facebook is.
        return bool(settings.FACEBOOK_CLIENT_ID and settings.FACEBOOK_CLIENT_SECRET)

    @staticmethod
    def build_authorize_url(state: str) -> str:
        params = {
            "client_id": settings.FACEBOOK_CLIENT_ID,
            "redirect_uri": settings.INSTAGRAM_CONNECT_REDIRECT_URI,
            "response_type": "code",
            "scope": settings.INSTAGRAM_CONNECT_SCOPES,
            "state": state,
            # Someone who declined a permission last time is never re-asked by default;
            # without this the retry silently returns the same incomplete grant.
            "auth_type": "rerequest",
        }
        base = f"https://www.facebook.com/{settings.FACEBOOK_API_VERSION}/dialog/oauth"
        return f"{base}?{urlencode(params)}"

    async def connect(self, user_id: uuid.UUID, code: str) -> list[InstagramAccount]:
        """Complete the consent round and store every linked Instagram account."""
        async with httpx.AsyncClient(timeout=GRAPH_TIMEOUT) as client:
            token, expires_at = await self._long_lived_token(client, code)
            await self._assert_permissions(client, token)
            pages = await self._pages_with_instagram(client, token)

        if not pages:
            # Consent succeeded but there is nothing to connect -- almost always a
            # personal IG account, or one not linked to a Page.
            raise InstagramConnectError("no_instagram_account")

        stored = [
            await self._upsert(user_id, page, token, expires_at) for page in pages
        ]
        log.info("instagram_connected", user_id=str(user_id), accounts=len(stored))
        return stored

    async def _long_lived_token(
        self, client: httpx.AsyncClient, code: str
    ) -> tuple[str, datetime | None]:
        """Code -> short-lived user token -> ~60-day long-lived user token."""
        try:
            short = await client.get(
                graph_url("oauth/access_token"),
                params={
                    "client_id": settings.FACEBOOK_CLIENT_ID,
                    "client_secret": settings.FACEBOOK_CLIENT_SECRET,
                    "redirect_uri": settings.INSTAGRAM_CONNECT_REDIRECT_URI,
                    "code": code,
                },
            )
            short.raise_for_status()
            short_token = short.json()["access_token"]

            long = await client.get(
                graph_url("oauth/access_token"),
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.FACEBOOK_CLIENT_ID,
                    "client_secret": settings.FACEBOOK_CLIENT_SECRET,
                    "fb_exchange_token": short_token,
                },
            )
            long.raise_for_status()
            payload: dict[str, Any] = long.json()
        except httpx.HTTPStatusError as exc:
            log.warning("instagram_token_exchange_failed", error=graph_error_message(exc))
            raise InstagramConnectError("exchange_failed") from exc
        except (httpx.HTTPError, KeyError) as exc:
            log.warning("instagram_token_exchange_failed", error=type(exc).__name__)
            raise InstagramConnectError("exchange_failed") from exc

        return payload["access_token"], expiry_from_seconds(payload.get("expires_in"))

    async def _assert_permissions(self, client: httpx.AsyncClient, token: str) -> None:
        """Fail early and precisely when the user unticked a permission.

        Without this the next call just returns an empty list and the user is told
        "no Instagram account found", which sends them to fix the wrong thing.
        """
        try:
            resp = await client.get(graph_url("me/permissions"), params=signed_params(token))
            resp.raise_for_status()
            granted = {
                row["permission"]
                for row in resp.json().get("data", [])
                if row.get("status") == "granted"
            }
        except httpx.HTTPError as exc:
            log.warning("instagram_permissions_check_failed", error=type(exc).__name__)
            return  # Non-fatal: let the Pages call be the judge.

        required = {s.strip() for s in settings.INSTAGRAM_CONNECT_SCOPES.split(",") if s.strip()}
        missing = required - granted
        if missing:
            log.info("instagram_permissions_declined", missing=sorted(missing))
            raise InstagramConnectError("permissions_declined")

    async def _pages_with_instagram(
        self, client: httpx.AsyncClient, token: str
    ) -> list[dict[str, Any]]:
        """Pages the user manages that have an Instagram Business account attached."""
        try:
            resp = await client.get(
                graph_url("me/accounts"),
                params=signed_params(
                    token,
                    fields=(
                        "id,name,access_token,"
                        "instagram_business_account{id,username,name,profile_picture_url}"
                    ),
                    limit=100,
                ),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("instagram_pages_failed", error=graph_error_message(exc))
            raise InstagramConnectError("graph_failed") from exc
        except httpx.HTTPError as exc:
            log.warning("instagram_pages_failed", error=type(exc).__name__)
            raise InstagramConnectError("graph_failed") from exc

        return [p for p in resp.json().get("data", []) if p.get("instagram_business_account")]

    async def _upsert(
        self,
        user_id: uuid.UUID,
        page: dict[str, Any],
        user_token: str,
        expires_at: datetime | None,
    ) -> InstagramAccount:
        ig = page["instagram_business_account"]
        account = await self.repo.get_by_instagram_id(user_id, str(ig["id"]))
        if account is None:
            account = InstagramAccount(
                user_id=user_id,
                instagram_user_id=str(ig["id"]),
                page_id=str(page["id"]),
            )
        account.page_id = str(page["id"])
        account.page_name = page.get("name")
        account.username = ig.get("username")
        account.name = ig.get("name")
        account.profile_picture_url = ig.get("profile_picture_url")
        account.scopes = settings.INSTAGRAM_CONNECT_SCOPES
        account.token_expires_at = expires_at
        account.page_access_token_encrypted = encrypt_token(page.get("access_token"))
        account.user_access_token_encrypted = encrypt_token(user_token)
        account.updated_at = datetime.now(UTC)
        return await self.repo.add(account)

    async def list_for_user(self, user_id: uuid.UUID) -> list[InstagramAccount]:
        return await self.repo.list_for_user(user_id)

    async def disconnect(self, account_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Remove a connection. Scoped by user_id so one tenant cannot delete another's."""
        account = await self.repo.get(account_id, user_id)
        if account is None:
            return False
        await self.repo.delete(account)
        log.info("instagram_disconnected", user_id=str(user_id))
        return True
