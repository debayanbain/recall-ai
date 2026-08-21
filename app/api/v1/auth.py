"""Google OAuth routes + session cookie management."""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.api.deps import AuthServiceDep, CurrentUser
from app.core.config import settings
from app.core.security import create_session_token
from app.schemas.auth import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])

_STATE_COOKIE = "oauth_state"


@router.get("/google/login")
async def google_login() -> RedirectResponse:
    """Kick off Google OAuth. Stores CSRF state in a short-lived cookie."""
    state = secrets.token_urlsafe(24)
    from app.services.auth_service import AuthService

    redirect = RedirectResponse(AuthService.build_authorize_url(state))
    redirect.set_cookie(
        _STATE_COOKIE, state, max_age=600, httponly=True,
        secure=settings.COOKIE_SECURE, samesite="lax",
    )
    return redirect


@router.get("/google/callback")
async def google_callback(
    auth: AuthServiceDep,
    request: Request,
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    """Handle Google's redirect: verify state, provision user, set session."""
    expected = request.cookies.get(_STATE_COOKIE)
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid OAuth state")

    userinfo = await auth.exchange_code(code)
    user = await auth.upsert_user(userinfo)
    token = create_session_token(str(user.id))

    redirect = RedirectResponse(f"{settings.FRONTEND_URL}/vault")
    redirect.set_cookie(
        settings.SESSION_COOKIE_NAME,
        token,
        max_age=settings.JWT_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
    )
    redirect.delete_cookie(_STATE_COOKIE)
    return redirect


@router.post("/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(settings.SESSION_COOKIE_NAME)
    return {"ok": True}


@router.get("/me", response_model=UserRead)
async def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)
