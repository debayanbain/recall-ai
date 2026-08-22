"""Symmetric encryption for provider OAuth tokens stored in the database.

Access and refresh tokens are bearer credentials for a *third-party* account, so a
dump of `oauth_accounts` must not be enough to act as the user on Google/Facebook/X.
They are therefore Fernet-encrypted (AES-128-CBC + HMAC-SHA256) with a key that lives
only in the environment, never in the database.

`TOKEN_ENCRYPTION_KEY` is optional in dev: with no key set, `encrypt_token` returns
``None`` so the token is simply never written. `validate_deployment_config` makes the
key mandatory outside dev whenever an OAuth provider is configured.
"""
from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class TokenEncryptionError(RuntimeError):
    """Raised when a stored token cannot be decrypted (wrong or rotated key)."""


@lru_cache(maxsize=1)
def _fernet() -> Fernet | None:
    """Build the Fernet box once. Returns None when no key is configured (dev only)."""
    key = settings.TOKEN_ENCRYPTION_KEY
    if not key:
        return None
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        # Never echo the key itself -- this message reaches crash logs.
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not a valid Fernet key (expect urlsafe-base64, "
            "32 bytes). Generate one with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\""
        ) from exc


def encryption_enabled() -> bool:
    return _fernet() is not None


def encrypt_token(plaintext: str | None) -> str | None:
    """Encrypt a provider token. Returns None if there is nothing to store."""
    if not plaintext:
        return None
    box = _fernet()
    if box is None:
        # No key configured (dev). Drop the token rather than persist it in the clear.
        return None
    return box.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str | None) -> str | None:
    """Decrypt a stored provider token. Returns None if nothing was stored."""
    if not ciphertext:
        return None
    box = _fernet()
    if box is None:
        raise TokenEncryptionError("TOKEN_ENCRYPTION_KEY is not configured")
    try:
        return box.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TokenEncryptionError("Stored token could not be decrypted") from exc
