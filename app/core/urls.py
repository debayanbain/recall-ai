"""Canonical form for a saved URL.

Duplicate detection is only as good as the key it compares. Instagram appends `?igsi=…`
to every share, Facebook adds `fbclid`, and campaign links carry `utm_*` — so the same
reel shared twice arrives as two different strings and, without normalising, becomes two
cards, two AI calls and two paid Apify runs.

Deliberately conservative: only parameters that are known tracking noise are dropped.
Stripping unknown query parameters would break links whose identity lives in the query
(`youtube.com/watch?v=…` is the obvious one).
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Analytics and share-attribution parameters. None of these change what is at the URL.
TRACKING_PARAMS = frozenset(
    {
        "igsi", "igshid", "img_index",           # Instagram
        "fbclid", "mibextid",                    # Facebook
        "gclid", "dclid", "msclkid",             # ad networks
        "si", "feature", "app",                  # YouTube share links
        "ref", "ref_src", "ref_url", "source",
        "mc_cid", "mc_eid", "_hsenc", "_hsmi",
        "yclid", "twclid", "ttclid", "s",
    }
)


def canonical_url(raw: str) -> str:
    """Return a comparable form of `raw`, or `raw` unchanged if it cannot be parsed."""
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return raw.strip()
    if not parts.scheme or not parts.netloc:
        return raw.strip()

    # Host is case-insensitive; the path is not.
    netloc = parts.netloc.lower()
    # Drop a default port so :443 and the bare host do not diverge.
    for scheme, port in (("https", ":443"), ("http", ":80")):
        if parts.scheme.lower() == scheme and netloc.endswith(port):
            netloc = netloc[: -len(port)]

    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
        ]
    )

    path = parts.path
    # A trailing slash never identifies a different document, except at the root.
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    # The fragment is client-side only and never reaches the server.
    return urlunsplit((parts.scheme.lower(), netloc, path, query, ""))
