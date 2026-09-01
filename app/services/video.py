"""Reading a video, so a reel is more than its caption.

An Instagram reel's caption is not its content. The thing a creator actually wants you to
do is usually burned into the frames -- a domain on a title card, a handle in the corner,
"link in bio" over a product shot -- or said out loud. None of that reaches the caption,
so a vault built from captions alone is one where the useful half of every reel is missing
and nothing reports a problem.

This is the existing image and speech capabilities pointed at a video, not a new provider:
frames go to `OPENAI_VISION_MODEL`, audio goes to `OPENAI_TRANSCRIBE_MODEL`. Both halves
are needed and neither substitutes for the other -- a spoken URL is invisible to the
frames, an on-screen one is invisible to the audio.

Four things a caller must respect:

* **The video URL is attacker-influencable.** It arrives inside an Apify payload derived
  from a URL somebody pasted. `fetch_video` therefore validates every redirect hop through
  `app/core/net.py::assert_safe_url` and caps the download while streaming it, rather than
  trusting `Content-Length` -- which the CDN writes, and which a lie in is a
  memory-exhaustion bug.
* **The audio is remuxed out, not uploaded whole.** The transcription API refuses anything
  over 25MB, and a 30MB video whose audio track is 900KB would fail for no reason. The
  existing AAC stream is copied into an `.m4a` container with no re-encode.
* **Everything this returns is untrusted text.** It is a model's reading of text an
  attacker chose to put on screen. It is stored as content and marked as machine-written,
  and every URL in it goes through `app/core/links.py` before anything renders it.
* **Nothing here follows a link it found.** See the note at the top of `core/links.py`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.links import extract_links
from app.core.logging import get_logger
from app.core.net import MAX_REDIRECTS, assert_safe_url
from app.services import transcription

log = get_logger("ai.video")

#: Frames decoded while walking forward from a keyframe to one sample target. Ten
#: seconds at 30fps -- comfortably past any real GOP, and a hard stop on a file whose
#: timestamps do not advance.
_MAX_DECODE_PER_TARGET = 300

#: Clipped rather than refused. A model that runs away describing a video is a cost
#: problem, not a correctness one, and the first paragraphs are the answer.
_MAX_TEXT_CHARS = 12_000

#: What the frames are asked for. Ordered so the part that matters most here -- text
#: visible on screen, transcribed exactly -- is asked for first and separately from the
#: description, which is the part a model is most tempted to embroider.
_PROMPT = (
    "These are frames sampled in order from a short video. Answer in three sections, "
    "using exactly these headings:\n\n"
    "On-screen text:\n"
    "Transcribe every piece of text visible in the frames, exactly as written, one per "
    "line. Include web addresses, domain names, @handles, button labels and prices. Do "
    "not correct spelling or expand abbreviations. If a word is not legible, skip it -- "
    "never guess at text you cannot read. If there is no text, write 'none'.\n\n"
    "Links:\n"
    "List every web address or bare domain that appears in the frames, one per line, "
    "exactly as shown. If there are none, write 'none'.\n\n"
    "What happens:\n"
    "Two to four sentences describing what the video shows, for someone who cannot watch "
    "it and who will later search for it from memory. Describe only what is visible.\n\n"
    "Write the three headings exactly as given, in plain text. No markdown anywhere: "
    "no #, no **, no bullet characters. Text you transcribe from a frame is copied as "
    "it appears and is the one thing you must not reformat."
)



#: The three section headings `_PROMPT` asks for. Used to repair the model's own headings
#: without touching anything else -- see `_plain_headings`.
_HEADINGS = frozenset({"on-screen text:", "links:", "what happens:"})


def _plain_headings(text: str) -> str:
    """Strip markdown syntax from the section headings, and from nothing else.

    The model is asked for plain text and returns `### On-screen text:` often enough to
    matter -- it is a request, not a constraint. The body is stored as `content` and
    rendered as plain text, so the markers show up literally on the page.

    Deliberately *not* a general markdown strip. The on-screen section is a verbatim
    transcription of what a frame shows, and a frame that genuinely reads `**SALE**` or
    `#1` must survive intact -- a cosmetic cleanup that edits quoted material is a worse
    bug than the cosmetic problem. So a line is only rewritten when, with the syntax
    removed, it *is* one of the three headings we asked for.
    """
    lines = []
    for line in text.splitlines():
        candidate = re.sub(r"^\s*#{1,6}\s*", "", line.strip())
        candidate = re.sub(r"^\*\*(.+?)\*\*$", r"\1", candidate).strip()
        lines.append(candidate if candidate.lower() in _HEADINGS else line)
    return "\n".join(lines)


class VideoError(ValueError):
    """This video cannot be read. An answer, not a fault -- the caller carries on."""


class VideoUnavailable(RuntimeError):
    """Video reading is not configured. A deployment fact, not a bad video."""


class VideoFailed(RuntimeError):
    """The provider or the network broke. Retryable; the message is ours, theirs logged."""


@dataclass(frozen=True)
class VideoReading:
    """What a video turned out to contain."""

    #: Text the vision model read off the frames, verbatim, plus its description.
    frames_text: str | None
    #: What was said, if the clip had an audio track and transcription is on.
    speech: str | None
    #: Safe http(s) links found across both, already through `core.links`.
    links: list[str] = field(default_factory=list)
    frames_read: int = 0
    duration_seconds: float | None = None

    @property
    def text(self) -> str:
        """The two readings as one labelled blob, for the AI pipeline and the embedding."""
        parts: list[str] = []
        if self.speech:
            parts.append("Spoken in the video:\n" + self.speech)
        if self.frames_text:
            parts.append("Seen in the video:\n" + self.frames_text)
        return "\n\n".join(parts).strip()

    def __bool__(self) -> bool:
        return bool(self.text)


def video_understanding_enabled() -> bool:
    return settings.video_understanding_enabled


def max_video_bytes() -> int:
    return settings.MAX_VIDEO_MB * 1024 * 1024


async def fetch_video(url: str) -> bytes:
    """Download a video, validating every hop and capping the size while streaming.

    Redirects are followed manually for the same reason `ArticleExtractor` does it: an
    allowed external URL is free to redirect to an internal one, so each hop is
    re-validated rather than the first one being trusted for the whole chain.
    """
    limit = max_video_bytes()
    current = url

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            assert_safe_url(current)
            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise VideoError("The video link redirected to nowhere.")
                    current = str(httpx.URL(current).join(location))
                    continue

                if resp.status_code >= 400:
                    # 4xx here is an expired CDN signature far more often than anything
                    # else, and re-running the actor is the only thing that fixes it.
                    raise VideoError(
                        f"The video could not be downloaded (HTTP {resp.status_code}). "
                        "Its link may have expired."
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        # Refused rather than truncated: half a video decodes to a
                        # broken container, and there is no partial answer worth paying
                        # a vision call for.
                        raise VideoError(
                            f"That video is larger than {settings.MAX_VIDEO_MB}MB, so it "
                            "wasn't read."
                        )
                    chunks.append(chunk)

                data = b"".join(chunks)
                if not data:
                    raise VideoError("The video link returned nothing.")
                return data

    raise VideoError("The video link redirected too many times.")


def sample_frames(data: bytes) -> tuple[list[bytes], float | None]:
    """Decode evenly spaced frames as JPEG bytes. Blocking and CPU-bound -- run in a thread.

    Each target is reached by seeking (which lands on the preceding keyframe) and then
    decoding forward to the target, under a frame budget.

    The obvious cheaper design -- decode keyframes only, via `skip_frame = "NONKEY"` --
    was measured wrong here, and wrong in the quietest possible way. A short H.264 video
    routinely carries a GOP of 250, so a 6-second clip has exactly one keyframe: all eight
    "samples" came back byte-identical, the model was billed for eight copies of one
    picture, and the reading described the first moment of the video as though it were the
    whole thing. Nothing about that looks like a failure from the outside.
    """
    import av  # lazy: the binary ffmpeg import is heavy and only this path needs it
    from PIL import Image

    wanted = max(1, settings.VIDEO_FRAME_COUNT)
    frames: list[bytes] = []

    try:
        # `av.open` is typed as returning either container kind whatever the mode, so the
        # read mode is narrowed here rather than inferred.
        container = cast("Any", av.open(io.BytesIO(data), mode="r"))
    except Exception as exc:  # noqa: BLE001 - anything unopenable is "cannot read"
        raise VideoError("That file could not be opened as a video.") from exc

    try:
        if not container.streams.video:
            raise VideoError("That video has no picture track.")
        stream = container.streams.video[0]

        duration = (
            float(container.duration) / av.time_base if container.duration else None
        )
        if duration is not None and duration > settings.MAX_VIDEO_SECONDS:
            # Sampling eight frames across an hour describes nothing, and paying for it
            # to describe nothing is the worst of both.
            raise VideoError(
                f"That video is longer than {settings.MAX_VIDEO_SECONDS // 60} minutes, "
                "so it wasn't read."
            )

        # Midpoints of `wanted` equal slices, so the first frame is not the black frame
        # every video opens on and the last is not the cut to black it ends on.
        targets = (
            [duration * (i + 0.5) / wanted for i in range(wanted)]
            if duration
            else [0.0]
        )

        # Deduplicated by content: a still title card, or a target that could not be
        # reached, yields a frame identical to one already held. Sending it again pays
        # twice for one picture and tells the model a moment repeated when it did not.
        digests: set[bytes] = set()
        for target in targets:
            frame = _frame_at(container, stream, target)
            if frame is None:
                continue
            jpeg = _encode(frame, Image)
            digest = hashlib.sha256(jpeg).digest()
            if digest in digests:
                continue
            digests.add(digest)
            frames.append(jpeg)

        return frames, duration
    finally:
        container.close()


def _frame_at(container: Any, stream: Any, seconds: float) -> Any | None:
    """Decode the frame at `seconds`, or None if there is none.

    `seek` lands on the keyframe at or before the target, which on a long GOP can be
    seconds earlier, so the frames after it are decoded until one reaches the target. The
    budget bounds that walk: without it a single-keyframe file would decode itself in full
    once per target.
    """
    try:
        if stream.time_base:
            container.seek(int(seconds / float(stream.time_base)), stream=stream)

        fallback: Any | None = None
        for index, frame in enumerate(container.decode(stream)):
            fallback = frame
            if frame.time is not None and frame.time >= seconds:
                return frame
            if index >= _MAX_DECODE_PER_TARGET:
                # Past the budget the nearest frame decoded is the honest answer; it is
                # still a real moment of this video, just not the one asked for.
                break
        return fallback
    except Exception as exc:  # noqa: BLE001 - one unreadable frame is not a failure
        log.debug("video_frame_seek_failed", at=seconds, error=type(exc).__name__)
    return None


def _encode(frame: Any, image_module: Any) -> bytes:
    """Downscale a decoded frame and JPEG-encode it.

    Not scaled as small as it could be: on-screen text is the point of reading these at
    all, and a domain rendered in a thin font stops being legible before the picture does.
    """
    image = frame.to_image()
    edge = max(1, settings.VIDEO_FRAME_MAX_EDGE)
    if max(image.size) > edge:
        ratio = edge / max(image.size)
        size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        image = image.resize(size, image_module.LANCZOS)

    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def extract_audio(data: bytes) -> bytes | None:
    """Copy the audio track into an `.m4a` container, or None if there is none.

    A stream *copy*, not a re-encode: the source is already AAC in every reel this will
    ever see, re-encoding would cost CPU to lose quality, and the point of the exercise is
    only to stop a 30MB video being uploaded to an API that refuses anything over 25MB.
    """
    import av  # lazy, as above

    try:
        source = cast("Any", av.open(io.BytesIO(data), mode="r"))
    except Exception:  # noqa: BLE001
        return None

    try:
        if not source.streams.audio:
            return None
        in_stream = source.streams.audio[0]

        buf = io.BytesIO()
        output = cast("Any", av.open(buf, mode="w", format="mp4"))
        try:
            out_stream = output.add_stream_from_template(in_stream)
            for packet in source.demux(in_stream):
                if packet.dts is None:  # the flush packet demux ends on
                    continue
                packet.stream = out_stream
                output.mux(packet)
        finally:
            output.close()

        audio = buf.getvalue()
        return audio or None
    except Exception as exc:  # noqa: BLE001 - no audio track is not a failure to read
        log.info("video_audio_extract_failed", error=type(exc).__name__)
        return None
    finally:
        source.close()


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=8), reraise=True)
async def _call_provider(frames: list[bytes]) -> Any:
    """One vision call carrying every frame. Two attempts, like `vision._call_provider`.

    A retry re-uploads every frame and pays for a second reading of all of them, so a
    persistent failure costs real money to confirm.
    """
    from openai import AsyncOpenAI  # lazy import, mirrors ai/openai.py
    from openai.types.chat import (
        ChatCompletionContentPartParam,
        ChatCompletionMessageParam,
    )

    if not settings.OPENAI_API_KEY:
        raise VideoUnavailable("Video reading is not configured.")

    content: list[ChatCompletionContentPartParam] = [{"type": "text", "text": _PROMPT}]
    for jpeg in frames:
        encoded = base64.b64encode(jpeg).decode()
        content.append(
            {
                "type": "image_url",
                # `detail: high` is what makes small on-screen text readable. On a frame
                # already scaled to VIDEO_FRAME_MAX_EDGE the extra cost is bounded, and
                # low detail loses exactly the thing being looked for.
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}", "detail": "high"},
            }
        )

    messages: list[ChatCompletionMessageParam] = [{"role": "user", "content": content}]
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return await client.chat.completions.create(
        model=settings.OPENAI_VISION_MODEL,
        messages=messages,
        # Low but non-zero, matching vision and the text providers: a reading should not
        # drift between two runs over the same frames.
        temperature=0.2,
        max_tokens=1200,
    )


async def read_frames(frames: list[bytes]) -> str:
    """Read text and meaning off sampled frames. Raises on anything unusable."""
    if not video_understanding_enabled():
        raise VideoUnavailable("Video reading is not configured.")
    if not frames:
        raise VideoError("No frames could be decoded from that video.")

    try:
        response = await _call_provider(frames)
    except (VideoUnavailable, VideoError):
        raise
    except Exception as exc:  # noqa: BLE001 - one opaque answer for every provider fault
        # The provider's own message is logged, never returned: it can quote request
        # details and, on a misconfiguration, name the account it was rejected for.
        log.warning(
            "video_read_failed",
            model=settings.OPENAI_VISION_MODEL,
            frames=len(frames),
            error=type(exc).__name__,
        )
        raise VideoFailed("The video couldn't be read right now.") from exc

    text = ""
    if response.choices:
        text = (response.choices[0].message.content or "").strip()
    if not text:
        raise VideoError("Nothing could be read from that video.")
    return _plain_headings(text)[:_MAX_TEXT_CHARS]


async def transcribe_audio(video_bytes: bytes) -> str | None:
    """Transcribe the clip's audio track, or None when there is nothing to transcribe.

    Never raises. A reel with no speech, a silent screen recording and a transcription
    service having a bad minute all mean the same thing to the caller: the frames are the
    reading. Losing the audio half of a video is a worse memory; losing the item over it
    would be a bug.
    """
    if not (settings.VIDEO_TRANSCRIBE_AUDIO and transcription.transcription_enabled()):
        return None

    audio = await asyncio.to_thread(extract_audio, video_bytes)
    if not audio:
        return None

    try:
        clip = transcription.inspect(audio)
        transcript = await transcription.transcribe(clip)
    except Exception as exc:  # noqa: BLE001 - the frames still carry the item
        # Includes TranscriptionError for a genuinely silent clip, which is the common
        # case: a large share of reels are music over text cards.
        log.info("video_speech_unavailable", error=type(exc).__name__)
        return None

    return transcript.text or None


async def read_video(data: bytes) -> VideoReading:
    """Read a downloaded video: what is written on it, and what is said in it.

    Raises `VideoError` when the file cannot be read at all -- which the caller treats as
    "no reading", not as a failure -- and `VideoFailed` when a provider broke, which is
    worth retrying.
    """
    if not video_understanding_enabled():
        raise VideoUnavailable("Video reading is not configured.")

    # Decoding is CPU-bound and PyAV is synchronous, so it must not run on the worker's
    # event loop -- the loop is what the concurrent HTTP calls in this task need.
    frames, duration = await asyncio.to_thread(sample_frames, data)

    speech = await transcribe_audio(data)

    frames_text: str | None = None
    try:
        frames_text = await read_frames(frames)
    except VideoError:
        # Undecodable or unreadable frames are survivable when there is speech; without
        # speech there is nothing, and the caller sees an empty reading.
        log.info("video_frames_unreadable", frames=len(frames))
    # VideoFailed deliberately propagates: a provider fault is worth a retry, and
    # silently keeping only the audio would make a transient outage look like a video
    # that has no text on it.

    links = extract_links(frames_text, speech, limit=settings.MAX_EXTRACTED_LINKS)

    log.info(
        "video_read",
        frames=len(frames),
        duration=duration,
        has_speech=bool(speech),
        has_frames_text=bool(frames_text),
        links=len(links),
    )
    return VideoReading(
        frames_text=frames_text,
        speech=speech,
        links=links,
        frames_read=len(frames),
        duration_seconds=duration,
    )
