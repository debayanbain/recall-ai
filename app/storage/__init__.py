"""Object storage. Import `get_storage()`, never a provider class."""
from __future__ import annotations

from app.core.config import settings
from app.storage.base import ObjectStorage, StorageError


def get_storage() -> ObjectStorage | None:
    """The configured bucket, or None when file storage is off.

    None is a first-class answer: the vault works without a bucket (URLs, notes and PDF
    *text* need no storage), so an unconfigured deployment must degrade to "uploads are
    unavailable" instead of failing to boot.
    """
    if not settings.storage_enabled:
        return None
    from app.storage.b2 import B2Storage

    return B2Storage()


__all__ = ["ObjectStorage", "StorageError", "get_storage"]
