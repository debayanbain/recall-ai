"""Callback failure paths: the user must always land back on the frontend.

The provider half of the flow can succeed while our half fails (database down, missing
migration, bad encryption key). Before this was handled the user got a bare 500 on a
backend URL with no way back, which is what these tests pin down.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_auth_service
from app.core.config import settings
from app.main import app
from app.services.oauth import registry
from app.services.oauth.base import OAuthIdentity

_STATE = "state-value-for-tests"


class _StubProvider:
    name = "stub"
    uses_pkce = False

    def is_configured(self) -> bool:
        return True

    def build_authorize_url(self, state: str, code_challenge: str | None = None) -> str:
        return f"https://stub.example/authorize?state={state}"

    async def exchange_code(
        self, code: str, code_verifier: str | None = None
    ) -> OAuthIdentity:
        return OAuthIdentity(
            provider="stub",
            account_id="stub-1",
            email="person@example.com",
            email_verified=True,
        )


class _ExplodingAuthService:
    """Stands in for a service whose database is unreachable."""

    async def login(self, identity: OAuthIdentity) -> Any:
        raise OSError("connection refused")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setitem(registry._PROVIDERS, "stub", _StubProvider())
    app.dependency_overrides[get_auth_service] = lambda: _ExplodingAuthService()
    with TestClient(app, follow_redirects=False) as c:
        c.cookies.set("oauth_state", _STATE)
        yield c
    app.dependency_overrides.clear()


def test_persistence_failure_redirects_to_sign_in(client: TestClient) -> None:
    response = client.get(
        "/api/v1/auth/stub/callback", params={"code": "abc", "state": _STATE}
    )
    assert response.status_code == 307
    location = response.headers["location"]
    assert location.startswith(settings.FRONTEND_URL)
    assert "/sign-in?error=server_error" in location


def test_persistence_failure_sets_no_session_cookie(client: TestClient) -> None:
    """A failed login must not leave a usable session behind."""
    response = client.get(
        "/api/v1/auth/stub/callback", params={"code": "abc", "state": _STATE}
    )
    assert settings.SESSION_COOKIE_NAME not in response.cookies


def test_persistence_failure_does_not_leak_the_exception(client: TestClient) -> None:
    """Only the coarse code travels to the browser -- never the exception text."""
    response = client.get(
        "/api/v1/auth/stub/callback", params={"code": "abc", "state": _STATE}
    )
    assert "connection refused" not in response.headers["location"]


# --- diagnosing a split-origin flow -------------------------------------------------


def test_missing_state_cookie_names_the_origin_split(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A cookie that never arrives because the flow spans two origins must say so.

    Without this the only signal is `invalid_state`, whose user-facing copy blames an
    expired link -- which sends people looking in entirely the wrong place.
    """
    from app.api.v1.auth import _origin_hint

    monkeypatch.setattr(
        settings, "GOOGLE_REDIRECT_URI", "https://tunnel.example/api/v1/auth/google/callback"
    )

    class _Req:
        headers = {"host": "localhost:8000"}

    hint = _origin_hint(_Req(), "google")  # type: ignore[arg-type]
    assert "hint" in hint
    assert "localhost:8000" in hint["hint"]
    assert "tunnel.example" in hint["hint"]


def test_no_hint_when_both_ends_share_an_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1.auth import _origin_hint

    monkeypatch.setattr(
        settings, "GOOGLE_REDIRECT_URI", "https://tunnel.example/api/v1/auth/google/callback"
    )

    class _Req:
        headers = {"host": "tunnel.example"}

    assert _origin_hint(_Req(), "google") == {}  # type: ignore[arg-type]
