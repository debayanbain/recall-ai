"""Instagram connect flow: authorize URL shape and the grant-injection defences.

The callback is a plain GET that Meta drives, so the state check is the only thing
standing between an attacker's consent and a victim's account. These tests pin every
branch of it closed.
"""
from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, get_instagram_service
from app.core.config import settings
from app.main import app
from app.models.user import User
from app.services.instagram_service import InstagramConnectError, InstagramService

_USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_OTHER_USER_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
_NONCE = "nonce-value-used-across-these-tests"
_COOKIE = "instagram_state"


class _RecordingService:
    """Stands in for the real service and records whether connect() ran at all."""

    def __init__(self) -> None:
        self.connect_calls: list[tuple[uuid.UUID, str]] = []
        self.raises: InstagramConnectError | None = None

    async def connect(self, user_id: uuid.UUID, code: str) -> list[Any]:
        self.connect_calls.append((user_id, code))
        if self.raises is not None:
            raise self.raises
        return [object()]

    async def list_for_user(self, user_id: uuid.UUID) -> list[Any]:
        return []

    async def disconnect(self, account_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return False


@pytest.fixture
def service() -> _RecordingService:
    return _RecordingService()


@pytest.fixture
def client(service: _RecordingService) -> Any:
    user = User(id=_USER_ID, email="person@example.com", auth_provider="google")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_instagram_service] = lambda: service
    with TestClient(app, follow_redirects=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def anon_client() -> Any:
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _callback(client: TestClient, **params: str) -> Any:
    return client.get("/api/v1/integrations/instagram/callback", params=params)


# --- authorize URL -----------------------------------------------------------------


def test_authorize_url_requests_instagram_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FACEBOOK_CLIENT_ID", "app-id")
    query = parse_qs(urlsplit(InstagramService.build_authorize_url("abc")).query)

    assert set(query["scope"][0].split(",")) == {
        "instagram_basic",
        "pages_show_list",
        "business_management",
    }
    # Without rerequest, a user who declined once is silently never re-asked.
    assert query["auth_type"] == ["rerequest"]
    assert query["redirect_uri"] == [settings.INSTAGRAM_CONNECT_REDIRECT_URI]


def test_sign_in_scopes_stay_free_of_instagram_permissions() -> None:
    """Business-Page access must not ride along on the login consent screen."""
    assert "instagram" not in settings.FACEBOOK_SCOPES
    assert "pages" not in settings.FACEBOOK_SCOPES


# --- auth required -----------------------------------------------------------------


@pytest.mark.parametrize("path", ["", "/start", "/callback"])
def test_routes_require_authentication(anon_client: TestClient, path: str) -> None:
    assert anon_client.get(f"/api/v1/integrations/instagram{path}").status_code == 401


# --- state / grant injection -------------------------------------------------------


def test_callback_without_state_cookie_is_rejected(
    client: TestClient, service: _RecordingService
) -> None:
    """The grant-injection case: attacker's code, victim's browser, no cookie."""
    response = _callback(client, code="attacker-code", state=_NONCE)
    assert "reason=invalid_state" in response.headers["location"]
    assert service.connect_calls == []


def test_callback_with_mismatched_nonce_is_rejected(
    client: TestClient, service: _RecordingService
) -> None:
    client.cookies.set(_COOKIE, f"{_USER_ID}.{_NONCE}")
    response = _callback(client, code="c", state="a-different-nonce")
    assert "reason=invalid_state" in response.headers["location"]
    assert service.connect_calls == []


def test_callback_bound_to_another_user_is_rejected(
    client: TestClient, service: _RecordingService
) -> None:
    """Flow started as one user, finished as another -- must not connect."""
    client.cookies.set(_COOKIE, f"{_OTHER_USER_ID}.{_NONCE}")
    response = _callback(client, code="c", state=_NONCE)
    assert "reason=invalid_state" in response.headers["location"]
    assert service.connect_calls == []


def test_valid_state_connects_for_the_current_user(
    client: TestClient, service: _RecordingService
) -> None:
    client.cookies.set(_COOKIE, f"{_USER_ID}.{_NONCE}")
    response = _callback(client, code="good-code", state=_NONCE)

    assert service.connect_calls == [(_USER_ID, "good-code")]
    location = response.headers["location"]
    assert location.startswith(settings.FRONTEND_URL)
    assert "instagram=connected" in location


def test_denied_consent_never_reaches_the_service(
    client: TestClient, service: _RecordingService
) -> None:
    client.cookies.set(_COOKIE, f"{_USER_ID}.{_NONCE}")
    response = _callback(client, error="access_denied", state=_NONCE)
    assert "reason=access_denied" in response.headers["location"]
    assert service.connect_calls == []


def test_connect_error_maps_to_its_code(
    client: TestClient, service: _RecordingService
) -> None:
    service.raises = InstagramConnectError("no_instagram_account")
    client.cookies.set(_COOKIE, f"{_USER_ID}.{_NONCE}")
    response = _callback(client, code="c", state=_NONCE)
    assert "reason=no_instagram_account" in response.headers["location"]


def test_disconnecting_an_unowned_account_is_404(client: TestClient) -> None:
    """404 rather than 403, so ids cannot be probed for existence."""
    response = client.delete(f"/api/v1/integrations/instagram/{uuid.uuid4()}")
    assert response.status_code == 404
