"""Cloudflare R2 (S3-compatible) object storage.

Files live in R2; Postgres stores only URLs/metadata. Client built lazily.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.core.config import settings


@lru_cache
def _client() -> Any:
    import boto3

    endpoint = f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


class R2Storage:
    """Thin wrapper around the S3 client for uploads + public URLs."""

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        _client().put_object(
            Bucket=settings.R2_BUCKET, Key=key, Body=data, ContentType=content_type
        )
        return self.public_url(key)

    def public_url(self, key: str) -> str:
        base = settings.R2_PUBLIC_URL.rstrip("/")
        return f"{base}/{key}"

    def presigned_get(self, key: str, expires: int = 3600) -> str:
        url: str = _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.R2_BUCKET, "Key": key},
            ExpiresIn=expires,
        )
        return url
