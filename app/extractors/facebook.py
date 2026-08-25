"""Facebook reels via an Apify actor.

Same two-phase shape as Instagram reels: `start` fires the actor and returns, the webhook
(or the sweeper) calls `build` with the payload.

The actor is configurable because Apify's first-party `facebook-reels-scraper` documents
its `startUrls` as *page* URLs — it walks a page's reels rather than resolving one reel by
link. If that turns out not to accept a single reel URL, point `APIFY_FACEBOOK_ACTOR` at
a store actor that does; nothing else here changes.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.extractors.base import (
    DeferredExtractor,
    ExtractedContent,
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


class FacebookReelExtractor(DeferredExtractor):
    content_type = ContentType.facebook
    deferred = True

    def can_handle(self, url: str) -> bool:
        return bool(_FB_REEL_RE.search(url))

    def _require_token(self) -> None:
        if not settings.APIFY_TOKEN:
            raise PermanentExtractionError(
                "Facebook reels need APIFY_TOKEN — Facebook blocks server-side fetches."
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
