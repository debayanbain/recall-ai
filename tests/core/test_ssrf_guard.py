"""The worker fetches user-supplied URLs, so the SSRF guard is load-bearing."""
from __future__ import annotations

import pytest

from app.core.net import UnsafeUrlError, assert_safe_url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",   # AWS/GCP/Azure IMDS
        "http://metadata.google.internal/",
        "http://127.0.0.1:8000/api/v1/auth/providers",
        "http://localhost:8000/",
        "http://[::1]:8000/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://0.0.0.0/",
        "http://[::ffff:127.0.0.1]/",                # IPv4 smuggled inside IPv6
    ],
)
def test_internal_targets_are_refused(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "data:text/html,<script>alert(1)</script>",
        "javascript:alert(1)",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_safe_url(url)


def test_a_hostname_that_resolves_to_loopback_is_refused() -> None:
    """Checking the literal string is not enough — plenty of names point inward."""
    with pytest.raises(UnsafeUrlError):
        assert_safe_url("http://localhost./")


def test_missing_host_is_refused() -> None:
    with pytest.raises(UnsafeUrlError):
        assert_safe_url("http:///nohost")


def test_ordinary_public_urls_still_pass() -> None:
    assert_safe_url("https://en.wikipedia.org/wiki/Zettelkasten")
    assert_safe_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
