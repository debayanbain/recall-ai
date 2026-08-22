"""Provider lookup by name. The single place that knows the concrete classes."""
from __future__ import annotations

from app.services.oauth.base import OAuthProvider
from app.services.oauth.facebook import FacebookOAuthProvider
from app.services.oauth.google import GoogleOAuthProvider
from app.services.oauth.instagram import InstagramOAuthProvider

# Order is the order the frontend renders the buttons in.
# X/Twitter is parked, not deleted: the working provider sits in
# `docs/parked/twitter_oauth.py` with re-enable steps in its docstring.
_PROVIDERS: dict[str, OAuthProvider] = {
    "google": GoogleOAuthProvider(),
    "facebook": FacebookOAuthProvider(),
    "instagram": InstagramOAuthProvider(),
}


def get_oauth_provider(name: str) -> OAuthProvider | None:
    """Return the provider, or None for an unknown or unconfigured name.

    Unconfigured is folded into None on purpose: the router turns both into a 404, so a
    probe cannot tell "provider does not exist" from "provider has no secret here".
    """
    provider = _PROVIDERS.get(name)
    if provider is None or not provider.is_configured():
        return None
    return provider


def configured_provider_names() -> list[str]:
    return [name for name, p in _PROVIDERS.items() if p.is_configured()]
