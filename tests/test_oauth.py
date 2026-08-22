"""Offline tests for the OAuth plumbing: redirect safety, PKCE, provider gating."""
from __future__ import annotations

import base64
import hashlib

import pytest

from app.api.v1.auth import _DEFAULT_NEXT, _pkce_pair, _safe_next
from app.services.auth_service import is_placeholder_email, placeholder_email
from app.services.oauth import OAuthIdentity, get_oauth_provider
from app.services.oauth.registry import configured_provider_names


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        "",
        "https://evil.com",
        "//evil.com",
        "/\\evil.com",
        "/%2fevil.com",
        "/%5Cevil.com",
        "javascript:alert(1)",
        "vault",
        "/vault\nLocation: https://evil.com",
        "data:text/html,<script>alert(1)</script>",
    ],
)
def test_safe_next_rejects_offsite_targets(candidate: str | None) -> None:
    assert _safe_next(candidate) == _DEFAULT_NEXT


@pytest.mark.parametrize("candidate", ["/vault", "/spaces/abc", "/vault?tab=all#top"])
def test_safe_next_keeps_relative_paths(candidate: str) -> None:
    assert _safe_next(candidate) == candidate


def test_pkce_pair_is_valid_s256() -> None:
    verifier, challenge = _pkce_pair()
    # RFC 7636: 43..128 chars, unreserved alphabet.
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    assert challenge == expected.rstrip(b"=").decode()
    assert "=" not in challenge


def test_pkce_pair_is_not_reused() -> None:
    assert _pkce_pair()[0] != _pkce_pair()[0]


def test_unconfigured_provider_is_indistinguishable_from_unknown() -> None:
    """Both must be None so the router can 404 without leaking which one it was."""
    assert get_oauth_provider("myspace") is None
    for name in ("google", "facebook"):
        if name not in configured_provider_names():
            assert get_oauth_provider(name) is None


def test_identity_drops_verified_flag_without_an_email() -> None:
    # A phone-only Facebook account releases no address; the flag must not survive.
    identity = OAuthIdentity(provider="facebook", account_id="1", email=None, email_verified=True)
    assert identity.email is None
    assert identity.email_verified is False


def test_placeholder_email_is_non_routable_and_recognised() -> None:
    identity = OAuthIdentity(provider="facebook", account_id="99")
    email = placeholder_email(identity)
    assert email == "facebook-99@users.noreply.recall.invalid"
    assert is_placeholder_email(email)
    assert not is_placeholder_email("real@example.com")


# --- Instagram Login (sign-in provider, not the Facebook connection) ---------------


def test_instagram_is_unconfigured_without_a_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three values are required: Instagram rejects a redirect_uri mismatch."""
    from app.core.config import settings
    from app.services.oauth.instagram import InstagramOAuthProvider

    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "ig-app")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "ig-secret")
    monkeypatch.setattr(settings, "INSTAGRAM_LOGIN_REDIRECT_URI", "")
    assert InstagramOAuthProvider().is_configured() is False

    monkeypatch.setattr(
        settings, "INSTAGRAM_LOGIN_REDIRECT_URI", "https://example.com/cb"
    )
    assert InstagramOAuthProvider().is_configured() is True


def test_instagram_authorize_url_targets_instagram_not_facebook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.parse import parse_qs, urlsplit

    from app.core.config import settings
    from app.services.oauth.instagram import InstagramOAuthProvider

    monkeypatch.setattr(settings, "INSTAGRAM_APP_ID", "ig-app")
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", "ig-secret")
    monkeypatch.setattr(settings, "INSTAGRAM_LOGIN_REDIRECT_URI", "https://example.com/cb")

    url = InstagramOAuthProvider().build_authorize_url("state-123")
    parts = urlsplit(url)
    assert parts.netloc == "www.instagram.com"
    query = parse_qs(parts.query)
    assert query["client_id"] == ["ig-app"]
    assert query["scope"] == ["instagram_business_basic"]
    assert query["state"] == ["state-123"]


def test_instagram_sign_in_credentials_are_separate_from_facebook() -> None:
    """Sign-in uses the Instagram App ID; the connection uses the Facebook one.

    Wiring them to the same value is the most likely mistake here, and it fails at Meta
    with an unhelpful error rather than in our code.
    """
    from app.core.config import Settings

    fields = set(Settings.model_fields)
    assert {"INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET", "INSTAGRAM_LOGIN_REDIRECT_URI"} <= fields
    assert {"FACEBOOK_CLIENT_ID", "INSTAGRAM_CONNECT_REDIRECT_URI"} <= fields
