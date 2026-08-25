"""OAuth login routes (Google / Facebook / X) and session cookie management.

One pair of routes serves every provider: `app.services.oauth.registry` resolves the
`{provider}` path segment, so adding a provider never touches this module.

Threat model notes for anyone editing this file:

* **CSRF on the callback** -- `state` is a 32-byte random value echoed by the provider and
  compared, in constant time, against a short-lived HttpOnly cookie. A callback without a
  matching cookie is rejected outright; a missing cookie is not treated as "skip the check".
* **Authorization-code interception** -- PKCE (S256) is used for every provider that
  supports it; the verifier lives in its own HttpOnly cookie and never in a URL.
* **Open redirect** -- the post-login `next` target is accepted only as a same-origin
  relative path (`/vault`), never as an absolute URL, and is re-validated on the way out.
* **Error leakage** -- provider failures redirect to the sign-in page with a coarse error
  code. Upstream response bodies (which can contain tokens) are never echoed to the client.

Sessions themselves are two cookies, not one (see `app.services.session_service`):

* `recall_session` -- a minutes-long access JWT, `Path=/`, sent with every API call.
* `recall_refresh` -- a 7-day opaque token, `Path=/api/v1/auth`, so it is never attached
  to `/vault`, `/search` or anything else. It rotates on every use; replaying a rotated
  one revokes the whole chain.

The point of the pair: coming back three days later, the browser still holds a valid
refresh token, `POST /auth/refresh` mints a new access token, and the user never sees the
provider again. Only a 7-day gap -- or an explicit sign-out -- sends them back to OAuth.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import AuthServiceDep, CurrentUser, SessionServiceDep
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.schemas.auth import (
    OAuthProviderInfo,
    ProvidersResponse,
    RefreshResponse,
    SessionRead,
    UserRead,
)
from app.services.oauth import get_oauth_provider
from app.services.oauth.registry import configured_provider_names
from app.services.session_service import IssuedSession, SessionError

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger("auth")

_STATE_COOKIE = "oauth_state"
_VERIFIER_COOKIE = "oauth_verifier"
_NEXT_COOKIE = "oauth_next"
_FLOW_COOKIE_MAX_AGE = 600  # 10 minutes: long enough to consent, short enough to expire

_DEFAULT_NEXT = "/vault"
_SIGN_IN_PATH = "/sign-in"

_PROVIDER_LABELS = {"google": "Google", "facebook": "Facebook", "instagram": "Instagram"}


def _safe_next(candidate: str | None) -> str:
    """Reduce a caller-supplied redirect target to a same-origin relative path.

    Anything absolute, protocol-relative (`//evil.com`), backslash-smuggled
    (`/\\evil.com`) or control-character bearing collapses to the default. Only a path
    is ever concatenated onto FRONTEND_URL, so an open redirect has no way in.
    """
    if not candidate or not candidate.startswith("/"):
        return _DEFAULT_NEXT
    if candidate.startswith(("//", "/\\", "/%2f", "/%2F", "/%5c", "/%5C")):
        return _DEFAULT_NEXT
    # Control characters would let a crafted value split the Location header.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return _DEFAULT_NEXT
    return candidate


def _frontend(path: str, **params: str) -> str:
    url = f"{settings.FRONTEND_URL.rstrip('/')}{path}"
    return f"{url}?{urlencode(params)}" if params else url


def _set_flow_cookie(response: Response, name: str, value: str) -> None:
    """Short-lived HttpOnly cookie carrying one leg of the OAuth handshake.

    SameSite=lax (not strict) on purpose: the provider sends the user back via a
    top-level GET, and a strict cookie would not be attached to it.
    """
    response.set_cookie(
        name,
        value,
        max_age=_FLOW_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_flow_cookies(response: Response) -> None:
    for name in (_STATE_COOKIE, _VERIFIER_COOKIE, _NEXT_COOKIE):
        response.delete_cookie(name, path="/", httponly=True, secure=settings.COOKIE_SECURE,
                               samesite="lax")


# The refresh cookie is scoped to the auth routes and nowhere else. A cookie the browser
# only attaches to four endpoints cannot be leaked by any other endpoint's logging,
# caching or CORS mistake -- and no XSS payload can read it either way (HttpOnly).
_REFRESH_COOKIE_PATH = f"{settings.API_V1_PREFIX}/auth"


def _set_session_cookies(response: Response, issued: IssuedSession) -> None:
    """Attach the access + refresh pair. Both HttpOnly; neither is readable from JS."""
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        issued.access_token,
        max_age=issued.access_expires_in,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        domain=settings.SESSION_COOKIE_DOMAIN,
        path="/",
    )
    response.set_cookie(
        settings.REFRESH_COOKIE_NAME,
        issued.refresh_token,
        # Max-Age matches the token's own expiry, so a browser that keeps the cookie
        # longer than the server keeps the row is impossible.
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        domain=settings.SESSION_COOKIE_DOMAIN,
        path=_REFRESH_COOKIE_PATH,
    )


def _clear_session_cookies(response: Response) -> None:
    """Drop both cookies. Path/domain must mirror the set exactly or the browser keeps
    the old cookie and the user appears to be signed in until it expires on its own."""
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        path="/",
        domain=settings.SESSION_COOKIE_DOMAIN,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
    )
    response.delete_cookie(
        settings.REFRESH_COOKIE_NAME,
        path=_REFRESH_COOKIE_PATH,
        domain=settings.SESSION_COOKIE_DOMAIN,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
    )


def _client_ip(request: Request) -> str | None:
    """The peer address, and deliberately *not* X-Forwarded-For.

    XFF is caller-supplied text; trusting it lets anyone write whatever they like into
    the device list a user is meant to audit. Behind a proxy this records the proxy --
    fix that with `--proxy-headers` and a trusted-hosts list at the ASGI server, which is
    the only layer that knows which hop is actually ours.
    """
    return request.client.host if request.client else None


def _assert_same_site(request: Request) -> None:
    """Reject a cross-origin state change on the session endpoints.

    SameSite already blocks this for the default `lax` cookie, but a deployment whose SPA
    and API are genuinely cross-site must set `SESSION_COOKIE_SAMESITE=none`, and that
    hands the browser back to the attacker's page. An Origin allowlist is the check that
    survives both settings. A request with no Origin at all (curl, a native app) is
    allowed: no browser omits Origin on a cross-site POST, so absence is not the attack.
    """
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = {*settings.CORS_ORIGINS, settings.FRONTEND_URL.rstrip("/")}
    if origin.rstrip("/") not in {a.rstrip("/") for a in allowed}:
        log.warning("session_cross_origin_rejected", origin=origin, path=request.url.path)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-origin request rejected")


def _pkce_pair() -> tuple[str, str]:
    """RFC 7636 S256 verifier/challenge."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _origin_hint(request: Request, provider: str) -> dict[str, str]:
    """Explain a missing state cookie when the flow spans two origins.

    The cookie is set on whatever host started the login and only comes back to that same
    host. If the browser begins at one origin (say http://localhost:8000, because the SPA
    points there) but the provider is configured to return to another (an https tunnel),
    the cookie is simply not sent and every login dies as `invalid_state` with nothing in
    the logs to say why. This turns that into a one-line diagnosis.
    """
    impl_uri = {
        "google": settings.GOOGLE_REDIRECT_URI,
        "facebook": settings.FACEBOOK_REDIRECT_URI,
        "instagram": settings.INSTAGRAM_LOGIN_REDIRECT_URI,
    }.get(provider, "")
    callback_host = urlsplit(impl_uri).netloc if impl_uri else ""
    request_host = request.headers.get("host", "")
    if not callback_host or not request_host or callback_host == request_host:
        return {}
    return {
        "hint": (
            f"login began on a different origin than the callback ({request_host} vs "
            f"{callback_host}); the state cookie cannot travel between them. Point the "
            f"frontend's NEXT_PUBLIC_API_URL and every *_REDIRECT_URI at one origin "
            f"(see `make dev-tunnel`)."
        )
    }


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    """Providers this deployment can actually complete a login with.

    The frontend renders exactly these buttons, so a provider missing its secret is
    never offered rather than failing after the user clicks.
    """
    return ProvidersResponse(
        providers=[
            OAuthProviderInfo(
                id=name,
                label=_PROVIDER_LABELS.get(name, name.title()),
                login_url=f"{settings.API_V1_PREFIX}/auth/{name}/login",
            )
            for name in configured_provider_names()
        ]
    )


@router.get("/{provider}/login")
async def oauth_login(
    provider: str,
    next: str = Query(_DEFAULT_NEXT, max_length=512),
) -> RedirectResponse:
    """Start the OAuth dance: mint CSRF state (+ PKCE) and bounce to the provider."""
    impl = get_oauth_provider(provider)
    if impl is None:
        # Same 404 for "unknown" and "not configured" -- no provider enumeration.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown auth provider")

    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair() if impl.uses_pkce else ("", None)

    redirect = RedirectResponse(
        impl.build_authorize_url(state, challenge), status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
    _set_flow_cookie(redirect, _STATE_COOKIE, state)
    _set_flow_cookie(redirect, _NEXT_COOKIE, _safe_next(next))
    if verifier:
        _set_flow_cookie(redirect, _VERIFIER_COOKIE, verifier)
    return redirect


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    auth: AuthServiceDep,
    sessions: SessionServiceDep,
    request: Request,
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
) -> RedirectResponse:
    """Provider redirect target: verify state, exchange the code, set the session."""
    impl = get_oauth_provider(provider)
    if impl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown auth provider")

    target = _safe_next(request.cookies.get(_NEXT_COOKIE))

    def fail(reason: str) -> RedirectResponse:
        # Coarse, non-reflective error code. Nothing from the provider -- or from our own
        # exception text -- is echoed back to the browser.
        response = RedirectResponse(_frontend(_SIGN_IN_PATH, error=reason, provider=provider))
        _clear_flow_cookies(response)
        return response

    if error or not code:
        # User denied consent, or the provider bailed out.
        log.info("oauth_denied", provider=provider)
        return fail("access_denied")

    expected_state = request.cookies.get(_STATE_COOKIE)
    # Absent cookie must fail closed: treating "no state to compare" as a pass is the
    # classic way CSRF protection ends up being decorative.
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        log.warning(
            "oauth_state_mismatch",
            provider=provider,
            # A *missing* cookie and a *wrong* cookie have completely different causes,
            # and collapsing them into one message sends people hunting the wrong bug.
            reason="cookie_absent" if not expected_state else "value_mismatch",
            **_origin_hint(request, provider),
        )
        return fail("invalid_state")

    verifier = request.cookies.get(_VERIFIER_COOKIE) if impl.uses_pkce else None
    if impl.uses_pkce and not verifier:
        log.warning("oauth_missing_verifier", provider=provider)
        return fail("invalid_state")

    try:
        identity = await impl.exchange_code(code, verifier)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        # exc may carry the upstream body (tokens included) -- log the type, not the text.
        log.warning("oauth_exchange_failed", provider=provider, error=type(exc).__name__)
        return fail("exchange_failed")

    try:
        user = await auth.login(identity)
        issued = await sessions.start(
            user,
            user_agent=request.headers.get("user-agent"),
            ip_address=_client_ip(request),
        )
    except Exception:
        # The provider half succeeded, so the failure is ours -- an unreachable database,
        # a missing migration, a bad encryption key. Stranding the user on a bare 500 at a
        # backend URL gives them no way back, so send them to sign-in with a coarse code
        # and keep the full traceback in the server log where it is actually useful.
        log.exception("oauth_persist_failed", provider=provider)
        return fail("server_error")

    redirect = RedirectResponse(f"{settings.FRONTEND_URL.rstrip('/')}{target}")
    _set_session_cookies(redirect, issued)
    _clear_flow_cookies(redirect)
    log.info("oauth_login", provider=provider, user_id=str(user.id))
    return redirect


def _current_session_id(request: Request) -> uuid.UUID | None:
    """Which session row the caller's access token was minted from, if it says.

    Only used to flag "this device" in the session list, so a token that predates the
    `sid` claim (or fails to parse) degrades to "none of them are current" rather than
    to an error. The signature is still verified -- `decode_access_token` does that --
    but the caller has already been authenticated by the time this runs.
    """
    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        return None
    try:
        return uuid.UUID(decode_access_token(token)["sid"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        return None


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_session(
    request: Request,
    response: Response,
    sessions: SessionServiceDep,
) -> RefreshResponse:
    """Trade the refresh cookie for a fresh access token, and rotate the refresh token.

    This is the endpoint that makes a login survive three days away: the browser still
    holds the refresh cookie, the SPA calls this on boot (or after any 401), and the user
    never sees the provider again.

    Every rejection is the same 401 with the same message. The *reason* -- unknown token,
    revoked, expired, deleted user -- is logged and never returned: a distinguishing
    error would tell whoever holds a stolen token whether it was ever real.
    """
    _assert_same_site(request)
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    try:
        issued = await sessions.refresh(
            raw,
            user_agent=request.headers.get("user-agent"),
            ip_address=_client_ip(request),
        )
    except SessionError as exc:
        log.info("session_refresh_rejected", reason=exc.reason)
        # Clear both cookies on the way out. Leaving a dead refresh cookie in place makes
        # the SPA retry this endpoint on every navigation forever.
        _clear_session_cookies(response)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated") from exc

    _set_session_cookies(response, issued)
    return RefreshResponse(
        expires_in=issued.access_expires_in,
        refresh_expires_at=issued.refresh_expires_at,
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    sessions: SessionServiceDep,
) -> dict[str, bool]:
    """Revoke this device's session server-side, then drop both cookies.

    Clearing cookies alone would leave a live refresh token in the database that anyone
    holding a copy could still redeem, which is exactly what "log out" is supposed to
    prevent. Always answers 200: an unknown or already-dead token still ends with the
    caller signed out, and a 401 here would only strand a client that cannot clear its
    own HttpOnly cookies.
    """
    _assert_same_site(request)
    await sessions.revoke(request.cookies.get(settings.REFRESH_COOKIE_NAME))
    _clear_session_cookies(response)
    return {"ok": True}


@router.post("/logout-all")
async def logout_all(
    request: Request,
    response: Response,
    user: CurrentUser,
    sessions: SessionServiceDep,
) -> dict[str, bool | int]:
    """Sign out every device for the current user -- the "someone has my account" button.

    Access tokens already minted stay valid until they expire (they are never checked
    against the database), so ACCESS_TOKEN_EXPIRE_MINUTES is the true bound on how long
    an attacker keeps read access after this. That is why it is capped at an hour.
    """
    _assert_same_site(request)
    count = await sessions.revoke_all(user.id)
    _clear_session_cookies(response)
    return {"ok": True, "revoked": count}


@router.get("/sessions", response_model=list[SessionRead])
async def list_sessions(
    request: Request,
    user: CurrentUser,
    sessions: SessionServiceDep,
) -> list[SessionRead]:
    """The current user's live sessions -- their own devices and nobody else's.

    Scoped by `user.id` in the repository query, so there is no id a caller could
    substitute. The refresh token is never part of this payload; only its row id is.
    """
    current_sid = _current_session_id(request)
    return [
        SessionRead(
            id=row.id,
            current=row.id == current_sid,
            user_agent=row.user_agent,
            ip_address=row.ip_address,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
        )
        for row in await sessions.list_sessions(user.id)
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    user: CurrentUser,
    sessions: SessionServiceDep,
) -> Response:
    """Revoke one of the caller's own sessions by id.

    Ownership is enforced in the service; a session belonging to someone else is
    reported as 404 rather than 403, so this cannot be used to test whether a given
    session id exists.
    """
    _assert_same_site(request)
    if not await sessions.revoke_one(user.id, session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    if session_id == _current_session_id(request):
        _clear_session_cookies(response)
    return response


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser, auth: AuthServiceDep) -> UserRead:
    payload = UserRead.model_validate(user)
    payload.linked_providers = await auth.linked_providers(user)
    return payload
