"""Outbound-fetch safety for user-supplied URLs (SSRF guard).

The worker fetches whatever a user pastes. Without a guard that is a server-side request
forgery primitive: pasting `http://169.254.169.254/latest/meta-data/` makes the worker
read the cloud instance's IAM credentials and store them as vault content, which the user
then reads back through the API. Internal-only services (databases, admin panels, the
app's own API) are reachable the same way.

The rules here are deliberately deny-by-default:

* http/https only -- no file://, gopher://, data: and friends.
* The hostname is resolved and *every* resulting address must be publicly routable.
  Checking the literal string is not enough: `localtest.me` and countless other names
  resolve to 127.0.0.1.
* Redirects are not followed automatically; each hop is re-validated, because an
  external URL is free to redirect to an internal one.

Residual risk: DNS rebinding. The name is resolved for validation and resolved again by
the HTTP client when it connects, so a hostile resolver can answer differently the second
time. Closing that needs connection-level IP pinning; the practical mitigations are to
run the worker with egress restrictions and to keep this list current.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Link-local addresses used by cloud providers to expose instance credentials.
BLOCKED_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS / GCP / Azure / DigitalOcean IMDS
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)

MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class UnsafeUrlError(ValueError):
    """The URL points somewhere the server must not fetch."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Publicly routable only. Rejects loopback, private, link-local, reserved, multicast."""
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return False
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return False
    # ::ffff:127.0.0.1 and friends smuggle an IPv4 address inside IPv6.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and not _is_public(mapped):
        return False
    return True


def assert_safe_url(url: str) -> None:
    """Raise `UnsafeUrlError` unless `url` resolves entirely to public addresses."""
    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeUrlError(f"scheme {parts.scheme!r} is not fetchable")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeUrlError("URL has no host")
    if host in BLOCKED_HOSTS:
        raise UnsafeUrlError("host is a cloud metadata endpoint")

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        raise UnsafeUrlError(f"host does not resolve ({type(exc).__name__})") from exc

    # sockaddr[0] is the address string for both AF_INET and AF_INET6.
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        raise UnsafeUrlError("host resolved to no addresses")

    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])  # strip IPv6 zone id
        except ValueError as exc:
            raise UnsafeUrlError(f"unparseable address {raw!r}") from exc
        # One bad answer is enough: a round-robin name with a single internal record
        # must not be fetchable at all.
        if not _is_public(ip):
            raise UnsafeUrlError("host resolves to a non-public address")
