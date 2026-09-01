"""YouTube extractor: oEmbed for the card, the Data API for what the video actually says.

oEmbed is keyless and returns exactly four useful fields -- title, author, thumbnail and
an embed snippet. It does **not** return the description, and the description is where
every link a creator mentions on screen actually lives ("full guide at ...", the affiliate
block, the timestamps). Reading a YouTube memory built from oEmbed alone means the model
summarised a title and invented the rest, which is worse than an honest empty state.

So there are three tiers here, each degrading into the one below rather than failing:

1. **oEmbed** -- always, keyless. Title, author, thumbnail. Enough for a card.
2. **Data API v3 `videos.list`** -- when `YOUTUBE_API_KEY` is set. The real description,
   the creator's own tags, the channel and the publish date. 1 quota unit per save against
   a 10,000/day allowance, so a personal vault will not come close.
3. **The caption track** -- keyless, best-effort. This is the closest thing to reading the
   video: a spoken URL or product name that appears in no description is in here. It is
   fetched from YouTube's player endpoints, which block datacenter IPs often enough that a
   failure must never fail the save. Logged and dropped.

Links found in the description and the transcript go through `app/core/links.py`, which is
what decides whether a string is safe to store and show. Nothing here fetches them.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from app.core.config import settings
from app.core.links import collect_links
from app.core.logging import get_logger
from app.core.net import assert_safe_url
from app.extractors.base import ExtractedContent, Extractor
from app.models.base import ContentType

log = get_logger("extractor.youtube")

_YT_RE = re.compile(
    r"(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)", re.IGNORECASE
)

# A video id is 11 characters of the URL-safe alphabet. Pinned to that shape because it is
# interpolated into a Google API request: the endpoint is fixed and the id is the only
# user-controlled part of it, so constraining the id is what keeps that request ours.
_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?(?:[^#]*&)?v=|youtu\.be/|youtube\.com/shorts/)"
    r"(?P<id>[A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
)

_OEMBED = "https://www.youtube.com/oembed"
_DATA_API = "https://www.googleapis.com/youtube/v3/videos"


class YouTubeExtractor(Extractor):
    content_type = ContentType.youtube

    def can_handle(self, url: str) -> bool:
        return bool(_YT_RE.search(url))

    async def extract(self, url: str) -> ExtractedContent:
        # oembed is a fixed YouTube endpoint, but the `url` parameter is user-supplied
        # and YouTube echoes/redirects on it, so validate before handing it over.
        assert_safe_url(url)
        video_id = self._video_id(url)

        oembed = await self._fetch_oembed(url)
        snippet = await self._fetch_snippet(video_id) if video_id else {}
        transcript = await self._fetch_transcript(video_id) if video_id else None

        title = snippet.get("title") or oembed.get("title")
        author = snippet.get("channelTitle") or oembed.get("author_name")
        description = (snippet.get("description") or "").strip()
        description = description[: settings.YOUTUBE_MAX_DESCRIPTION_CHARS]
        creator_tags = [t for t in (snippet.get("tags") or []) if isinstance(t, str)][:25]

        # One labelled blob for the AI pipeline, same shape as the Instagram extractor's.
        # Labelled rather than concatenated so the model is not guessing which fragment is
        # the creator's own words and which is a machine's reading of the audio.
        parts: list[str] = []
        if title:
            parts.append(f"YouTube video: {title}" + (f" by {author}" if author else ""))
        if description:
            parts.append("Description:\n" + description)
        if creator_tags:
            parts.append("Creator tags: " + ", ".join(creator_tags))
        if transcript:
            parts.append("Transcript:\n" + transcript)
        content = "\n\n".join(parts).strip() or None

        # Description first: those links are the creator's own, typed deliberately, and
        # are the ones a viewer is meant to follow. A URL appearing only in the transcript
        # was spoken aloud, and a transcription of a spoken domain is a guess -- kept, but
        # tagged as such so the reader can say so.
        links = collect_links(
            [("description", description), ("transcript", transcript)],
            limit=settings.MAX_EXTRACTED_LINKS,
        )

        return ExtractedContent(
            type=self.content_type,
            title=title,
            content=content,
            thumbnail_url=oembed.get("thumbnail_url"),
            metadata={
                "author": author,
                "provider": oembed.get("provider_name"),
                "html": oembed.get("html"),
                "video_id": video_id,
                "description": description or None,
                "creator_tags": creator_tags,
                "published_at": snippet.get("publishedAt"),
                "has_transcript": bool(transcript),
                # [{"url", "source"}]. The source is load-bearing, not decoration: a
                # description link was typed by the creator, a transcript one was heard
                # by a model, and only one of those is safe to present as a fact.
                "links": links,
            },
        )

    @staticmethod
    def _video_id(url: str) -> str | None:
        match = _VIDEO_ID_RE.search(url)
        return match.group("id") if match else None

    async def _fetch_oembed(self, url: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(_OEMBED, params={"url": url, "format": "json"})
            resp.raise_for_status()
            data = resp.json()
        return data if isinstance(data, dict) else {}

    async def _fetch_snippet(self, video_id: str) -> dict[str, Any]:
        """The real description, or `{}`. Never raises: this tier is an upgrade.

        A missing key, an exhausted quota or a video the API will not describe all mean
        the same thing to the caller -- carry on with what oEmbed gave us. Raising here
        would turn a degraded memory into a failed save.
        """
        if not settings.youtube_api_enabled:
            return {}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    _DATA_API,
                    params={
                        "part": "snippet",
                        "id": video_id,
                        "key": settings.YOUTUBE_API_KEY,
                    },
                )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - an upgrade tier never breaks the save
            # Only the exception type. An httpx error's message carries the full request
            # URL, and this request's query string carries the API key.
            log.warning(
                "youtube_data_api_failed", video_id=video_id, error=type(exc).__name__
            )
            return {}

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            log.info("youtube_data_api_empty", video_id=video_id)
            return {}
        snippet = items[0].get("snippet") if isinstance(items[0], dict) else None
        return snippet if isinstance(snippet, dict) else {}

    async def _fetch_transcript(self, video_id: str) -> str | None:
        """The caption track as plain text, or None. Never raises.

        `youtube-transcript-api` is synchronous and does real network I/O, so it runs in a
        thread rather than blocking the worker's event loop.
        """
        if not settings.YOUTUBE_TRANSCRIPT_ENABLED:
            return None
        try:
            return await asyncio.to_thread(self._transcript_blocking, video_id)
        except Exception as exc:  # noqa: BLE001 - captions are the most fragile tier
            # Disabled captions, an age gate, a video with no track, and a datacenter IP
            # YouTube has decided to block all land here. None of them is our user's
            # problem and none should cost them the save.
            log.info(
                "youtube_transcript_unavailable",
                video_id=video_id,
                error=type(exc).__name__,
            )
            return None

    @staticmethod
    def _transcript_blocking(video_id: str) -> str | None:
        from youtube_transcript_api import YouTubeTranscriptApi  # lazy: heavy import

        api = YouTubeTranscriptApi()
        wanted = [
            code.strip()
            for code in settings.YOUTUBE_TRANSCRIPT_LANGUAGES.split(",")
            if code.strip()
        ] or ["en"]

        try:
            fetched = api.fetch(video_id, languages=wanted)
        except Exception:  # noqa: BLE001 - fall back to whatever track does exist
            # A video captioned only in its own language still carries the URLs and
            # product names that are the point of reading it at all, so the preferred
            # list is a preference and not a requirement.
            available = list(api.list(video_id))
            if not available:
                return None
            fetched = available[0].fetch()

        text = " ".join(
            snippet.text.strip()
            for snippet in fetched
            if getattr(snippet, "text", "").strip()
        )
        # Collapse the runs of whitespace that auto-captions are full of before the cap,
        # or the limit is spent on spaces.
        text = re.sub(r"\s+", " ", text).strip()
        return text[: settings.YOUTUBE_MAX_TRANSCRIPT_CHARS] or None
