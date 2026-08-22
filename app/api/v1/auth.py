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
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import AuthServiceDep, CurrentUser
from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import create_session_token
from app.schemas.auth import OAuthProviderInfo, ProvidersResponse, UserRead
from app.services.oauth import get_oauth_provider
from app.services.oauth.registry import configured_provider_names

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
        token = create_session_token(str(user.id))
    except Exception:
        # The provider half succeeded, so the failure is ours -- an unreachable database,
        # a missing migration, a bad encryption key. Stranding the user on a bare 500 at a
        # backend URL gives them no way back, so send them to sign-in with a coarse code
        # and keep the full traceback in the server log where it is actually useful.
        log.exception("oauth_persist_failed", provider=provider)
        return fail("server_error")

    redirect = RedirectResponse(f"{settings.FRONTEND_URL.rstrip('/')}{target}")
    redirect.set_cookie(
        settings.SESSION_COOKIE_NAME,
        token,
        max_age=settings.JWT_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
        domain=settings.SESSION_COOKIE_DOMAIN,
        path="/",
    )
    _clear_flow_cookies(redirect)
    log.info("oauth_login", provider=provider, user_id=str(user.id))
    return redirect


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    """Drop the session cookie. Attributes must mirror the ones used to set it."""
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        path="/",
        domain=settings.SESSION_COOKIE_DOMAIN,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.SESSION_COOKIE_SAMESITE,
    )
    return {"ok": True}


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser, auth: AuthServiceDep) -> UserRead:
    payload = UserRead.model_validate(user)
    payload.linked_providers = await auth.linked_providers(user)
    return payload
