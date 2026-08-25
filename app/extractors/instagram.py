"""Instagram extractor backed by an Apify actor.

Instagram serves a login wall to server-side fetches, so the generic `ArticleExtractor`
returns a page titled "Instagram" with zero characters — nothing for the AI pipeline to
summarize or tag. Apify runs a real browser and hands back the caption, hashtags,
mentions, media URLs and engagement counts.

This extractor's only job is to turn that payload into one text blob plus structured
metadata. The summary, category and *memory tags* are still produced downstream by
`ProcessingService` through the configured `AIProvider` — keeping platform logic here and
AI logic there is the whole point of the extractor boundary.

Note this is a different Instagram integration from `services/instagram_service.py`. That
one links a user's *own* Business account via Facebook to read their own media. This one
reads any public post someone pastes, and needs no user connection.
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
    Extractor,
    PermanentExtractionError,
)
from app.models.base import ContentType

log = get_logger("extractor.instagram")

# Reels and IGTV only — the paid path. Profile and story URLs are deliberately not
# claimed: the actor returns a different shape for them and they are not "a memory" here.
_IG_REEL_RE = re.compile(
    r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:reel|reels|tv)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

# Static posts. Saved as a bare link on purpose: a post carries far less than a reel and
# is not worth an actor run, so it costs nothing and is marked `skipped` rather than
# failed. Instagram's login wall means there is no free way to read one.
_IG_POST_RE = re.compile(
    r"instagram\.com/(?:[A-Za-z0-9_.]+/)?p/([A-Za-z0-9_-]+)", re.IGNORECASE
)

_APIFY_BASE = "https://api.apify.com/v2/acts"

#: Upper bound on the text handed to the AI provider.
MAX_CONTENT_CHARS = 8000


class ApifyNotConfiguredError(PermanentExtractionError):
    """Missing token. Retrying cannot conjure one, so this is permanent."""


class InstagramReelExtractor(DeferredExtractor):
    content_type = ContentType.instagram
    #: Two-phase. A profile crawl can run for minutes on Apify; blocking a worker on
    #: that wastes a process and, worse, is capped by the task time limit.
    deferred = True

    def can_handle(self, url: str) -> bool:
        # Claimed even when Apify is unconfigured, on purpose: falling through to the
        # article fetcher would "succeed" with an empty login-wall page, which reads as a
        # silent product failure. Failing loudly names the missing setting instead.
        return bool(_IG_REEL_RE.search(url))

    def _require_token(self) -> None:
        if not settings.APIFY_TOKEN:
            raise ApifyNotConfiguredError(
                "Instagram links need APIFY_TOKEN — Instagram blocks server-side fetches, "
                "so there is no no-key fallback that returns real content."
            )

    async def start(self, url: str) -> str:
        """Fire the actor and return immediately with its run id.

        Uses `/runs` rather than `/run-sync-get-dataset-items`: the sync endpoint holds
        the connection until the crawl finishes and is capped around 300s, so it cannot
        express a long crawl at all. This returns in about a second regardless of how
        long the run will take.
        """
        self._require_token()
        endpoint = f"{_APIFY_BASE}/{settings.APIFY_INSTAGRAM_ACTOR}/runs"
        params: dict[str, Any] = {}
        # Ask Apify to call us back. Registered per-run rather than per-account so a
        # deployment cannot receive callbacks meant for another environment.
        webhook = self._webhook_config()
        if webhook:
            params["webhooks"] = webhook

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                endpoint,
                json=self._actor_input(url),
                params=params,
                headers={"Authorization": f"Bearer {settings.APIFY_TOKEN}"},
            )
        self._raise_for_status(resp)
        data = resp.json().get("data") or {}
        run_id = data.get("id")
        if not run_id:
            raise RuntimeError("Apify did not return a run id")
        log.info("apify_run_started", run_id=run_id, url=url[:80])
        return str(run_id)

    @staticmethod
    def _webhook_config() -> str | None:
        """base64 JSON of the webhook Apify should call. None when no public URL is set."""
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
        """Read a finished run's items. Called after the webhook, never from `build`."""
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

    def build(self, items: list[dict[str, Any]]) -> ExtractedContent:
        return self._build(items, "")

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        # Credit exhaustion and a bad token are configuration problems, not blips.
        if resp.status_code == 402:
            raise PermanentExtractionError(
                "Apify account is out of credits — top up or lower usage."
            )
        if resp.status_code in (401, 403):
            raise PermanentExtractionError("Apify rejected the token (check APIFY_TOKEN).")
        # 429 and 5xx are transient: let the retry policy handle them.
        resp.raise_for_status()

    def _actor_input(self, url: str) -> dict[str, Any]:

        payload: dict[str, Any] = {
            "directUrls": [url],
            "resultsType": "posts",
            "resultsLimit": 1,
            "addParentData": False,
        }
        # Instagram blocks datacenter IPs aggressively. Without a residential proxy the
        # actor mostly returns empty results, so this is opt-in but strongly recommended
        # in production; it requires a paid Apify plan.
        if settings.APIFY_USE_PROXY:
            payload["proxyConfiguration"] = {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
            }
        return payload

    def _build(self, items: list[dict[str, Any]], url: str) -> ExtractedContent:
        """Map the actor payload onto ExtractedContent. Pure — no I/O, so it is testable."""
        if not items:
            raise PermanentExtractionError(
                "Instagram returned nothing for that link — the post may be private, "
                "deleted, or the URL may not point at a post or reel."
            )
        post = items[0]

        # The actor reports an error inline rather than failing the run.
        if post.get("error"):
            code = str(post["error"])
            detail = str(post.get("errorDescription") or "")[:120]
            # A deleted post, a private account or a bad URL answers the same way every
            # time; retrying four times just spends four more actor runs.
            if code in {"not_found", "no_items", "private"}:
                raise PermanentExtractionError(
                    f"Instagram returned '{code}': {detail or 'post is unavailable'}. "
                    "It may be private, deleted, or not a post/reel URL."
                )
            raise RuntimeError(f"Instagram scrape failed: {code} {detail}".strip())

        caption = (post.get("caption") or "").strip()
        hashtags = [h for h in (post.get("hashtags") or []) if isinstance(h, str)]
        mentions = [m for m in (post.get("mentions") or []) if isinstance(m, str)]
        owner = post.get("ownerUsername") or post.get("ownerFullName")
        is_video = (post.get("type") or "").lower() == "video" or bool(post.get("videoUrl"))

        # One text blob for the AI pipeline. Caption first because it carries the meaning;
        # hashtags and mentions are strong tag signals; counts give the model a sense of
        # what mattered. Everything is labelled so the model is not guessing at fragments.
        parts: list[str] = []
        if owner:
            parts.append(f"Instagram {'reel' if is_video else 'post'} by @{owner}")
        if caption:
            parts.append(caption)
        if hashtags:
            parts.append("Hashtags: " + " ".join(f"#{h.lstrip('#')}" for h in hashtags))
        if mentions:
            parts.append("Mentions: " + " ".join(f"@{m.lstrip('@')}" for m in mentions))
        music = (post.get("musicInfo") or {}) if isinstance(post.get("musicInfo"), dict) else {}
        if music.get("song_name"):
            artist = music.get("artist_name") or ""
            parts.append(f"Audio: {music['song_name']} {artist}".strip())
        comments = [
            c.get("text")
            for c in (post.get("latestComments") or [])
            if isinstance(c, dict) and c.get("text")
        ]
        if comments:
            parts.append("Top comments: " + " | ".join(str(c) for c in comments[:5]))

        content = "\n\n".join(parts).strip() or None
        # The blob is fed to the LLM verbatim. A viral post's comment thread can run to
        # thousands of characters and blow the prompt budget for no extra signal.
        if content and len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS].rstrip() + "…"

        # A caption's first line is a better title than a truncated blob.
        title = caption.splitlines()[0].strip() if caption else None
        if title and len(title) > 120:
            title = title[:117].rstrip() + "…"
        if not title:
            kind = "reel" if is_video else "post"
            title = f"Instagram {kind}" + (f" by @{owner}" if owner else "")

        return ExtractedContent(
            type=self.content_type,
            title=title,
            content=content,
            thumbnail_url=post.get("displayUrl") or post.get("thumbnailUrl"),
            metadata={
                "owner": owner,
                "is_video": is_video,
                "video_url": post.get("videoUrl"),
                "video_duration": post.get("videoDuration"),
                "likes": post.get("likesCount"),
                "comments": post.get("commentsCount"),
                "views": post.get("videoViewCount") or post.get("videoPlayCount"),
                "posted_at": post.get("timestamp"),
                "hashtags": hashtags,
                "mentions": mentions,
                "shortcode": post.get("shortCode"),
                "source": "apify",
            },
        )


class InstagramPostExtractor(Extractor):
    """Static Instagram posts — saved as a link, never sent to Apify.

    A deliberate cost decision: a post is a caption and an image, which is not worth an
    actor run, whereas a reel carries a caption, audio, hashtags and a comment thread.
    Instagram serves a login wall to server-side fetches, so there is no free way to read
    the caption — the honest outcome is a saved link with no summary, marked `skipped`
    rather than `failed` so it does not look broken.
    """

    content_type = ContentType.instagram
    deferred = False

    def can_handle(self, url: str) -> bool:
        return bool(_IG_POST_RE.search(url))

    async def extract(self, url: str) -> ExtractedContent:
        match = _IG_POST_RE.search(url)
        shortcode = match.group(1) if match else None
        return ExtractedContent(
            type=self.content_type,
            title=f"Instagram post {shortcode}" if shortcode else "Instagram post",
            content=None,
            thumbnail_url=None,
            metadata={"shortcode": shortcode, "source": "link-only"},
            # No text exists, so there is nothing for the model to read. Asking it anyway
            # would spend tokens to hallucinate about a URL.
            enrich=False,
        )
