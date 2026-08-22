"""Shared Meta Graph API helpers.

Both Facebook Login (identity) and the Instagram connection talk to the same Graph
host with the same app credentials, so the URL builder and the appsecret proof live
here rather than being duplicated per caller.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from app.core.config import settings

GRAPH_TIMEOUT = 20.0


def graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}/{path.lstrip('/')}"


def appsecret_proof(access_token: str) -> str:
    """HMAC proving the call comes from the server holding the app secret.

    Without it, a token lifted from a client can be replayed against the Graph API from
    anywhere. With it, Meta rejects calls that do not carry the proof.
    """
    return hmac.new(
        settings.FACEBOOK_CLIENT_SECRET.encode(),
        access_token.encode(),
        hashlib.sha256,
    ).hexdigest()


def signed_params(access_token: str, **extra: Any) -> dict[str, Any]:
    """Query params for an authenticated Graph call, proof included."""
    return {
        "access_token": access_token,
        "appsecret_proof": appsecret_proof(access_token),
        **extra,
    }


class MetaGraphError(RuntimeError):
    """A Graph call failed. The message is safe to log, never to show a user verbatim."""


def graph_error_message(exc: httpx.HTTPStatusError) -> str:
    """Pull Meta's error message out of a failed response for the server log.

    Meta echoes the access token back in some error payloads, so only the `message`
    field is extracted -- never the whole body.
    """
    try:
        payload = exc.response.json()
        message = payload.get("error", {}).get("message")
        code = payload.get("error", {}).get("code")
        if message:
            return f"graph error {code}: {message}"
    except Exception:  # noqa: BLE001 - diagnostics must never mask the original failure
        pass
    return f"graph http {exc.response.status_code}"
