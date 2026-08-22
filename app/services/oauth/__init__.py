"""OAuth identity providers. Add a provider here, never a route."""
from app.services.oauth.base import OAuthIdentity, OAuthProvider
from app.services.oauth.registry import configured_provider_names, get_oauth_provider

__all__ = [
    "OAuthIdentity",
    "OAuthProvider",
    "get_oauth_provider",
    "configured_provider_names",
]
