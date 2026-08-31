"""Backblaze B2 object storage over the S3-compatible API.

The bucket is **private**. Nothing is ever served from a public URL: a download is a
short-lived presigned GET, minted per request only after the caller's ownership of the
row has been checked in the database. That is what keeps one user's documents out of
another user's reach even though the object key is guessable-shaped.

boto3 is synchronous, so every call is pushed to a worker thread -- a blocking upload in
the event loop would stall every other request on the process.

Config lives in `settings.B2_*`; `get_storage()` in `app/storage/__init__.py` returns None
when the bucket is not configured, and the callers treat that as "file storage is off"
rather than crashing.
"""
from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any
from urllib.parse import quote

from app.core.config import settings
from app.core.logging import get_logger
from app.storage.base import StorageError

log = get_logger("storage.b2")


@lru_cache
def _client() -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.B2_ENDPOINT_URL,
        aws_access_key_id=settings.B2_KEY_ID,
        aws_secret_access_key=settings.B2_APPLICATION_KEY,
        region_name=settings.B2_REGION,
        # B2 speaks SigV4 only, and virtual-host addressing needs the bucket in the
        # hostname; path style is what its documented S3 endpoint expects.
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    )


def _content_disposition(filename: str) -> str:
    """Always `attachment`, never `inline`.

    The browser must not render anything out of this bucket: an uploaded file that the
    origin decides to display is stored XSS. Forcing a download makes the file's own
    content type irrelevant to the page that linked to it. The name is sent twice --
    a plain ASCII fallback and RFC 5987 percent-encoding for anything else -- and the
    quoted form is stripped of quotes and control characters so it cannot break out of
    the header.
    """
    ascii_name = "".join(c for c in filename if 32 <= ord(c) < 127 and c not in '"\\;')
    ascii_name = ascii_name.strip() or "download"
    if ascii_name.startswith("."):  # name was entirely non-ASCII but for its suffix
        ascii_name = f"download{ascii_name}"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class B2Storage:
    """`ObjectStorage` implementation for Backblaze B2."""

    def __init__(self) -> None:
        self.bucket = settings.B2_BUCKET

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        def _put() -> None:
            _client().put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                # Belt and braces: the disposition is set on the object as well as on
                # the presigned URL, so a link generated any other way still downloads.
                ContentDisposition="attachment",
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:  # noqa: BLE001 - provider errors carry live HTTP handles
            log.warning("b2_upload_failed", key=key, error=type(exc).__name__)
            raise StorageError("We couldn't store that file. Please try again.") from None

    async def download(self, key: str) -> bytes:
        """Read an object's bytes. Used by the worker, never on a request path."""

        def _get() -> bytes:
            response = _client().get_object(Bucket=self.bucket, Key=key)
            body: bytes = response["Body"].read()
            return body

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:  # noqa: BLE001 - provider errors carry live HTTP handles
            log.warning("b2_download_failed", key=key, error=type(exc).__name__)
            raise StorageError("We couldn't read that file back from storage.") from None

    async def presigned_get(
        self, key: str, *, filename: str, content_type: str, expires: int
    ) -> str:
        def _sign() -> str:
            url: str = _client().generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": key,
                    "ResponseContentDisposition": _content_disposition(filename),
                    "ResponseContentType": content_type,
                },
                ExpiresIn=expires,
            )
            return url

        try:
            return await asyncio.to_thread(_sign)
        except Exception as exc:  # noqa: BLE001
            log.warning("b2_presign_failed", key=key, error=type(exc).__name__)
            raise StorageError("We couldn't build a download link. Please try again.") from None

    async def delete(self, key: str) -> None:
        """Purge every version of an object, not just the newest one.

        A plain S3 DELETE against a B2 bucket whose lifecycle is "Keep all versions" only
        writes a *delete marker*: the bytes stay, they keep costing money, and a user who
        asked for their document to be removed still has it stored. Verified against the
        live bucket -- one delete left `live versions: 1`.

        So this lists the versions for exactly this key and removes each by id. That makes
        deletion mean deletion whatever the bucket's lifecycle is set to, rather than
        depending on a console setting that anyone can change back.
        """

        def _purge() -> int:
            client = _client()
            removed = 0
            key_marker: str | None = None
            version_marker: str | None = None
            # Bounded: one object has a handful of versions, and an unbounded loop against
            # a paginated API is how a delete turns into a hang.
            for _ in range(10):
                params: dict[str, Any] = {"Bucket": self.bucket, "Prefix": key}
                if key_marker:
                    params["KeyMarker"] = key_marker
                    params["VersionIdMarker"] = version_marker
                page = client.list_object_versions(**params)

                entries = page.get("Versions", []) + page.get("DeleteMarkers", [])
                # Prefix listing can return neighbours (`abc.png` matches `abc.png.bak`),
                # so only exact key matches are touched.
                for entry in entries:
                    if entry.get("Key") != key:
                        continue
                    client.delete_object(
                        Bucket=self.bucket, Key=key, VersionId=entry["VersionId"]
                    )
                    removed += 1

                if not page.get("IsTruncated"):
                    break
                key_marker = page.get("NextKeyMarker")
                version_marker = page.get("NextVersionIdMarker")

            if removed == 0:
                # Versioning disabled, or already gone: a plain delete is the right call.
                client.delete_object(Bucket=self.bucket, Key=key)
            return removed

        try:
            removed = await asyncio.to_thread(_purge)
            log.info("b2_object_deleted", key=key, versions_removed=removed)
        except Exception as exc:  # noqa: BLE001
            # Deliberately not raised: the row is already gone from the user's vault, and
            # failing their delete because the bucket hiccuped would be worse than an
            # orphaned object. Logged so it is findable.
            log.warning("b2_delete_failed", key=key, error=type(exc).__name__)
