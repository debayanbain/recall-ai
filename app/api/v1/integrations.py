"""Connected third-party accounts. Instagram today; the shape generalises.

Unlike `/auth`, every route here requires an authenticated user: a connection attaches a
resource grant to an *existing* account rather than establishing identity.

Threat model:

* **Connection CSRF / grant injection** -- the callback is a plain GET that Meta drives,
  so without protection an attacker could complete their own consent, then lure a victim
  to the callback URL and graft their Instagram onto the victim's account. The state
  cookie is compared in constant time and a missing cookie is a rejection; the cookie is
  additionally bound to the user id that started the flow, so a session change mid-flow
  fails instead of connecting to the wrong account.
* **Token exposure** -- Page access tokens never appear in a response body; see
  `schemas/integrations.py`.
* **IDOR on disconnect** -- the delete path is scoped by `user_id` in the repository, so
  a guessed account id resolves to 404 rather than someone else's connection.
"""
from __future__ import annotations

import secrets
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from app.api.deps import CurrentUser, InstagramServiceDep
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.integrations import InstagramAccountRead, InstagramConnectionsResponse
from app.services.instagram_service import InstagramConnectError, InstagramService

router = APIRouter(prefix="/integrations", tags=["integrations"])
log = get_logger("integrations")

_STATE_COOKIE = "instagram_state"
_STATE_MAX_AGE = 600  # 10 minutes
_SETTINGS_PATH = "/settings"


def _frontend(**params: str) -> str:
    base = f"{settings.FRONTEND_URL.rstrip('/')}{_SETTINGS_PATH}"
    return f"{base}?{urlencode(params)}" if params else base


@router.get("/instagram", response_model=InstagramConnectionsResponse)
async def list_instagram(
    user: CurrentUser, service: InstagramServiceDep
) -> InstagramConnectionsResponse:
    accounts = await service.list_for_user(user.id)
    return InstagramConnectionsResponse(
        available=InstagramService.is_configured(),
        accounts=[InstagramAccountRead.model_validate(a) for a in accounts],
    )


@router.get("/instagram/start")
async def start_instagram(user: CurrentUser) -> RedirectResponse:
    """Send the user to Facebook for the Instagram permission round."""
    if not InstagramService.is_configured():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instagram is not configured")

    nonce = secrets.token_urlsafe(32)
    redirect = RedirectResponse(
        InstagramService.build_authorize_url(nonce),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )
    # Value binds the nonce to whoever started the flow; the callback checks both.
    redirect.set_cookie(
        _STATE_COOKIE,
        f"{user.id}.{nonce}",
        max_age=_STATE_MAX_AGE,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        # lax, not strict: Meta returns the user via a top-level GET, which a strict
        # cookie would not be attached to.
        samesite="lax",
        path="/",
    )
    return redirect


@router.get("/instagram/callback")
async def instagram_callback(
    user: CurrentUser,
    service: InstagramServiceDep,
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
) -> RedirectResponse:
    def finish(**params: str) -> RedirectResponse:
        response = RedirectResponse(_frontend(**params))
        response.delete_cookie(
            _STATE_COOKIE,
            path="/",
            httponly=True,
            secure=settings.COOKIE_SECURE,
            samesite="lax",
        )
        return response

    if error or not code:
        log.info("instagram_consent_denied")
        return finish(instagram="error", reason="access_denied")

    cookie = request.cookies.get(_STATE_COOKIE)
    expected_user, _, expected_nonce = (cookie or "").partition(".")
    # Fail closed on every branch: no cookie, no state, a mismatched nonce, or a nonce
    # minted for a different user than the one now signed in.
    if (
        not expected_nonce
        or not state
        or not secrets.compare_digest(expected_nonce, state)
        or expected_user != str(user.id)
    ):
        log.warning("instagram_state_mismatch")
        return finish(instagram="error", reason="invalid_state")

    try:
        accounts = await service.connect(user.id, code)
    except InstagramConnectError as exc:
        return finish(instagram="error", reason=exc.code)
    except Exception:
        log.exception("instagram_connect_failed")
        return finish(instagram="error", reason="server_error")

    return finish(instagram="connected", count=str(len(accounts)))


@router.delete("/instagram/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_instagram(
    account_id: uuid.UUID, user: CurrentUser, service: InstagramServiceDep
) -> None:
    removed = await service.disconnect(account_id, user.id)
    if not removed:
        # 404, not 403: a caller must not be able to probe which ids exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
