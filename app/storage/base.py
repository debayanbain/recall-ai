"""Object storage contract.

Business code depends on this Protocol, never on a concrete provider -- same rule as
`ai/`. Today the only implementation is Backblaze B2 (`b2.py`), but every method here is
plain S3, so R2/S3/MinIO drop in without touching a service.
"""
from __future__ import annotations

from typing import Protocol


class StorageError(RuntimeError):
    """The object store rejected an operation. Message is safe to show the user."""


class ObjectStorage(Protocol):
    """Private-bucket object storage. There are no public URLs by design."""

    async def upload(self, key: str, data: bytes, content_type: str) -> None: ...

    async def presigned_get(
        self, key: str, *, filename: str, content_type: str, expires: int
    ) -> str: ...

    async def delete(self, key: str) -> None: ...
