"""Facebook reels — two extractors for one URL family.

`FacebookReelExtractor` (default) reads the reel's Open Graph tags directly and costs
nothing. Facebook answers a *non-browser* User-Agent with the full share-preview markup:
`og:title` carries the entire caption, `og:url` the canonical reel URL and the page slug.
A browser-looking User-Agent gets HTTP 400 instead, which is why the header block below is
deliberately not disguised as Chrome. A `share/r/<code>` shortlink is resolved by the
normal redirect loop, so nothing has to special-case it.

`FacebookReelApifyExtractor` is the paid fallback, kept because the OG path cannot see
comments and depends on Facebook keeping unauthenticated previews open. It only claims a
URL when `FACEBOOK_USE_APIFY` is on, so the two never compete in the registry. Note that
Apify's first-party `facebook-reels-scraper` documents `startUrls` as *page* URLs — it
walks a page's reels rather than resolving one reel by link — so turning the flag on also
means pointing `APIFY_FACEBOOK_ACTOR` at a store actor that accepts a single reel.
"""
from __future__ import annotations

import base64
import json
import re
from html import unescape as _unescape
from html.parser import HTMLParser
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.core.net import MAX_REDIRECTS, MAX_RESPONSE_BYTES, assert_safe_url
from app.extractors.base import (
    DeferredExtractor,
    ExtractedContent,
    Extractor,
    PermanentExtractionError,
)
from app.models.base import ContentType

log = get_logger("extractor.facebook")

# facebook.com/reel/123, fb.watch/xyz, and the share/r/ shortlink form.
_FB_REEL_RE = re.compile(
    r"(?:facebook\.com/(?:[A-Za-z0-9_.]+/)?(?:reel|videos)/[A-Za-z0-9_-]+"
    r"|facebook\.com/share/r/[A-Za-z0-9_-]+"
    r"|fb\.watch/[A-Za-z0-9_-]+)",
    re.IGNORECASE,
)

_APIFY_BASE = "https://api.apify.com/v2/acts"
MAX_CONTENT_CHARS = 8000

# Facebook serves the share preview to crawlers and a 400 to anything that looks like a
# browser, so this identifies the bot honestly. Accept-Language pins the engagement line
# to English -- without it the counts come back in the exit node's locale.
_HEADERS = {
    "User-Agent": "RecallAIBot/1.0 (+https://recall.ai)",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

#: Facebook pads captions with zero-width joiners; they survive into the AI prompt and the
#: title otherwise.
_ZERO_WIDTH = re.compile(r"[​‌‍﻿]")
#: "3.7K views · 1.4K reactions" — the prefix Facebook prepends to og:title.
_STATS_RE = re.compile(
    r"([\d][\d.,]*)\s*([KMB])?\s*(views?|reactions?|comments?|shares?)", re.IGNORECASE
)
_SUFFIX = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
_PERMANENT_STATUSES = frozenset({400, 401, 403, 404, 410})


class _MetaHarvester(HTMLParser):
    """Collects `<meta property|name=...>` pairs. Stops feeding once `</head>` is seen."""

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        a = dict(attrs)
        key = a.get("property") or a.get("name")
        content = a.get("content")
        # First value wins: Facebook repeats og:image for each rendition.
        if key and content and key not in self.meta:
            self.meta[key] = content


def _clean(text: str) -> str:
    return _ZERO_WIDTH.sub("", text).strip()


def _looks_like_stats(chunk: str) -> bool:
    """True for the "3.7K views · 1.4K reactions" segment, not for a caption line."""
    return (
        "\n" not in chunk
        and len(chunk) <= 80
        and any(c.isdigit() for c in chunk)
        and bool(_STATS_RE.search(chunk))
    )


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _parse_stats(chunk: str) -> dict[str, int]:
    """"3.7K views · 1.4K reactions" -> {"views": 3700, "reactions": 1400}."""
    out: dict[str, int] = {}
    for value, suffix, label in _STATS_RE.findall(chunk):
        try:
            number = float(value.replace(",", ""))
        except ValueError:
            continue
        out[label.lower().rstrip("s") + "s"] = int(number * _SUFFIX.get(suffix.upper(), 1))
    return out



#: Facebook's own player URLs, carried in the inline JSON the page ships with. There is no
#: `og:video` on a reel -- verified against a live one -- so this is the only free route
#: to the actual video, and without it a Facebook memory is its caption and nothing else.
#:
#: SD is preferred over HD deliberately: the frames are downscaled to
#: VIDEO_FRAME_MAX_EDGE before they reach the model, and the audio is stream-copied rather
#: than re-encoded, so HD buys nothing and costs several times the download.
_PLAYER_URL_RE = re.compile(
    r'"(browser_native_sd_url|browser_native_hd_url)"\s*:\s*"((?:[^"\\]|\\.)*)"'
)


def _video_url(page: str) -> str | None:
    r"""The reel's mp4, or None.

    Undocumented internal JSON, so treat a miss as normal rather than as a fault: the key
    names can change without notice, and when they do a Facebook capture must quietly go
    back to being caption-only. `ProcessingService._read_video` already no-ops on a
    missing `video_url`, so returning None here is the whole degradation path.

    The value is decoded twice because it arrives escaped twice -- JSON (`\/`, `\uXXXX`)
    inside HTML (`&amp;`). The scheme is then checked here as well as in `fetch_video`:
    this string is scraped from a page we do not control and ends up in `item_metadata`,
    which *is* serialized to the browser, so a `javascript:` value must not get that far
    even though nothing renders it as a link today.
    """
    found: dict[str, str] = {}
    for match in _PLAYER_URL_RE.finditer(page):
        raw = match.group(2)
        if not raw or raw == "null":
            continue
        try:
            url = _unescape(json.loads(f'"{raw}"'))
        except (ValueError, json.JSONDecodeError):
            continue
        if url.startswith(("http://", "https://")):
            found.setdefault(match.group(1), url)

    return found.get("browser_native_sd_url") or found.get("browser_native_hd_url")


class FacebookReelExtractor(Extractor):
    """Free path: the reel's own Open Graph tags."""

    content_type = ContentType.facebook

    def can_handle(self, url: str) -> bool:
        if settings.FACEBOOK_USE_APIFY and settings.APIFY_TOKEN:
            return False  # the Apify extractor sits ahead of this one anyway
        return bool(_FB_REEL_RE.search(url))

    async def extract(self, url: str) -> ExtractedContent:
        html, final_url = await self._fetch(url)
        harvester = _MetaHarvester()
        harvester.feed(html)
        meta = harvester.meta

        raw_title = _clean(meta.get("og:title") or "")
        canonical = meta.get("og:url") or final_url
        if not raw_title and not meta.get("og:description"):
            # Either Facebook served the login wall or the reel is not public. Retrying
            # cannot change either, and the article fallback would store a blank page.
            raise PermanentExtractionError(
                "Facebook returned no preview for that link — the reel may be private, "
                "deleted, or restricted. Set FACEBOOK_USE_APIFY=true to scrape it instead."
            )

        stats, caption, owner = self._split(raw_title, canonical)
        if not caption:
            caption = _clean(meta.get("og:description") or "")

        parts: list[str] = []
        if owner:
            parts.append(f"Facebook reel by {owner}")
        if caption:
            parts.append(caption)
        content: str | None = "\n\n".join(parts).strip() or None
        if content and len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS].rstrip() + "…"

        title = caption.splitlines()[0].strip() if caption else ""
        if len(title) > 120:
            title = title[:117].rstrip() + "…"
        if not title:
            title = f"Facebook reel by {owner}" if owner else "Facebook reel"

        metadata: dict[str, Any] = {
            "owner": owner,
            "is_video": True,
            "canonical_url": canonical,
            "source": "opengraph",
            **_parse_stats(stats),
        }
        # What turns a Facebook capture from a caption into a read video. Set only when it
        # was really found: the key is what `ProcessingService._read_video` branches on,
        # so an absent one is the honest way to say "there is no video to read".
        video_url = _video_url(html)
        if video_url:
            metadata["video_url"] = video_url
        log.info(
            "facebook_og_extracted",
            url=canonical[:120],
            chars=len(content or ""),
            has_video_url=bool(video_url),
        )
        return ExtractedContent(
            type=self.content_type,
            title=title,
            content=content,
            thumbnail_url=meta.get("og:image"),
            metadata=metadata,
        )

    @staticmethod
    def _split(raw_title: str, canonical: str) -> tuple[str, str, str | None]:
        """Peel "<stats> | <caption> | <page name>" apart.

        Both wrappers are optional and a caption may itself contain " | ", so each end is
        only trimmed when it actually looks like the thing being trimmed.
        """
        slug_match = re.search(r"facebook\.com/([A-Za-z0-9_.]+)/", canonical)
        slug = slug_match.group(1) if slug_match else None

        stats = ""
        head, sep, rest = raw_title.partition(" | ")
        if sep and _looks_like_stats(head):
            stats, raw_title = head, rest

        owner: str | None = None
        body, sep, tail = raw_title.rpartition(" | ")
        if sep and "\n" not in tail and len(tail) <= 60 and slug:
            # Only trust the trailing chunk as a page name when it matches the slug the
            # canonical URL already proved; otherwise it is part of the caption.
            if _slugify(tail) == _slugify(slug):
                owner, raw_title = tail.strip(), body
        if owner is None and slug and slug.lower() not in {"reel", "video", "videos", "share"}:
            owner = slug

        return stats, _clean(raw_title), owner

    @staticmethod
    async def _fetch(url: str) -> tuple[str, str]:
        """GET the reel page, re-validating every redirect hop and capping the body.

        Redirects are followed by hand for the same reason `ArticleExtractor` does it: an
        allowed public URL is free to redirect to an internal one, so each hop goes back
        through `assert_safe_url` instead of being trusted by the client.
        """
        async with httpx.AsyncClient(
            timeout=20.0, follow_redirects=False, headers=_HEADERS
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                assert_safe_url(url)
                async with client.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            break
                        url = str(httpx.URL(url).join(location))
                        continue
                    if resp.status_code in _PERMANENT_STATUSES:
                        raise PermanentExtractionError(
                            f"Facebook refused that link (HTTP {resp.status_code}) — it is "
                            "probably private, deleted, or not a public reel."
                        )
                    resp.raise_for_status()
                    return await _read_capped(resp), url
        raise PermanentExtractionError("Facebook redirected too many times for that link.")


async def _read_capped(resp: httpx.Response) -> str:
    """Read at most MAX_RESPONSE_BYTES. Truncating is safe: the og tags are in <head>."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(resp.charset_encoding or "utf-8", errors="replace")


class FacebookReelApifyExtractor(DeferredExtractor):
    """Paid fallback, off unless `FACEBOOK_USE_APIFY` is set."""

    content_type = ContentType.facebook
    deferred = True

    def can_handle(self, url: str) -> bool:
        if not settings.FACEBOOK_USE_APIFY:
            return False
        return bool(_FB_REEL_RE.search(url))

    def _require_token(self) -> None:
        if not settings.APIFY_TOKEN:
            raise PermanentExtractionError(
                "FACEBOOK_USE_APIFY is on but APIFY_TOKEN is unset."
            )

    async def start(self, url: str) -> str:
        self._require_token()
        endpoint = f"{_APIFY_BASE}/{settings.APIFY_FACEBOOK_ACTOR}/runs"
        params: dict[str, Any] = {}
        webhook = self._webhook_config()
        if webhook:
            params["webhooks"] = webhook

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                endpoint,
                json={"startUrls": [{"url": url}], "resultsLimit": 1},
                params=params,
                headers={"Authorization": f"Bearer {settings.APIFY_TOKEN}"},
            )
        self._raise_for_status(resp)
        run_id = (resp.json().get("data") or {}).get("id")
        if not run_id:
            raise RuntimeError("Apify did not return a run id")
        log.info("apify_fb_run_started", run_id=str(run_id), url=url[:80])
        return str(run_id)

    @staticmethod
    def _webhook_config() -> str | None:
        if not (settings.PUBLIC_BASE_URL and settings.APIFY_WEBHOOK_SECRET):
            return None
        payload = [
            {
                "eventTypes": ["ACTOR.RUN.SUCCEEDED", "ACTOR.RUN.FAILED",
                               "ACTOR.RUN.ABORTED", "ACTOR.RUN.TIMED_OUT"],
                "requestUrl": (
                    f"{settings.PUBLIC_BASE_URL.rstrip('/')}"
                    f"{settings.API_V1_PREFIX}/webhooks/apify/{settings.APIFY_WEBHOOK_SECRET}"
                ),
            }
        ]
        return base64.b64encode(json.dumps(payload).encode()).decode()

    async def fetch_dataset(self, dataset_id: str) -> list[dict[str, Any]]:
        self._require_token()
        async with httpx.AsyncClient(timeout=settings.APIFY_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                f"https://api.apify.com/v2/datasets/{dataset_id}/items",
                params={"clean": "true", "limit": 50},
                headers={"Authorization": f"Bearer {settings.APIFY_TOKEN}"},
            )
        self._raise_for_status(resp)
        data = resp.json()
        if not isinstance(data, list):
            raise RuntimeError("Apify returned an unexpected dataset shape")
        return [row for row in data if isinstance(row, dict)]

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code == 402:
            raise PermanentExtractionError("Apify account is out of credits.")
        if resp.status_code in (401, 403):
            raise PermanentExtractionError("Apify rejected the token (check APIFY_TOKEN).")
        resp.raise_for_status()

    def build(self, items: list[dict[str, Any]]) -> ExtractedContent:
        """Map the actor payload. Field names vary by actor, so each is probed leniently."""
        if not items:
            raise PermanentExtractionError(
                "Facebook returned nothing for that link — the reel may be private, "
                "deleted, or the URL may not point at a reel."
            )
        post = items[0]
        if post.get("error"):
            raise PermanentExtractionError(f"Facebook scrape failed: {str(post['error'])[:120]}")

        def pick(*keys: str) -> Any:
            for k in keys:
                if post.get(k) not in (None, "", []):
                    return post[k]
            return None

        caption = str(pick("text", "caption", "title", "message") or "").strip()
        owner = pick("pageName", "authorName", "ownerName", "user")
        if isinstance(owner, dict):
            owner = owner.get("name")

        parts: list[str] = []
        if owner:
            parts.append(f"Facebook reel by {owner}")
        if caption:
            parts.append(caption)
        comments = [
            c.get("text")
            for c in (pick("comments", "latestComments") or [])
            if isinstance(c, dict) and c.get("text")
        ]
        if comments:
            parts.append("Top comments: " + " | ".join(str(c) for c in comments[:5]))

        content = "\n\n".join(parts).strip() or None
        if content and len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS].rstrip() + "…"

        title = caption.splitlines()[0].strip() if caption else None
        if title and len(title) > 120:
            title = title[:117].rstrip() + "…"
        if not title:
            title = f"Facebook reel by {owner}" if owner else "Facebook reel"

        return ExtractedContent(
            type=self.content_type,
            title=title,
            content=content,
            thumbnail_url=pick("thumbnailUrl", "thumbnail", "previewImage"),
            metadata={
                "owner": owner,
                "is_video": True,
                "video_url": pick("videoUrl", "video", "mediaUrl"),
                "views": pick("viewsCount", "views", "playCount"),
                "likes": pick("likesCount", "likes", "reactionsCount"),
                "comments": pick("commentsCount"),
                "posted_at": pick("time", "timestamp", "publishedAt"),
                "source": "apify",
            },
        )
