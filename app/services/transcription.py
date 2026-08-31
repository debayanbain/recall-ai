"""Speech-to-text for voice notes: what an audio clip may be, and what it said.

Deliberately **not** a method on `AIProvider`. That Protocol is implemented by every
provider and Gemini has no Whisper equivalent wired up here; adding `transcribe` to it
would oblige a provider to implement something it cannot do, and because the Protocol is
structural the omission would only surface at runtime, deep inside the pipeline. Speech
is its own capability with its own switch (`OPENAI_API_KEY`), so a vault summarising with
Gemini still records voice notes, and a vault with no OpenAI key reports voice as
unavailable up front instead of failing after someone has already spoken.

Every decision here is made from the **bytes**. A voice note has no meaningful filename --
`MediaRecorder` hands the browser a Blob and the client invents a name for it -- so the
container is sniffed from its signature and the name given to the provider is one this
module wrote. The allowlist is closed and covers exactly what a browser can record
(WebM/Opus in Chrome and Firefox, MP4/AAC in Safari) plus the formats a user might
already have on disk.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import get_logger
from app.core.scripts import contradicts_script, script_of

log = get_logger("ai.transcription")

# `script_of` / `contradicts_script` live in `app/core/scripts.py` -- the chat router and
# the scope gate need the same primitives, and two copies of "which script is this" is
# how only one of them gets a fix. Imported into this namespace so callers of this module
# keep one import for the whole language question.
__all__ = ["contradicts_script", "script_of"]

#: Signature -> (extension, mime type). Order matters: WAV is a RIFF container and must
#: be recognised before the loose MP3 frame-sync check below.
_SIGNATURES: tuple[tuple[bytes, int, str, str], ...] = (
    # EBML header. Also matches .mkv, which is fine -- we only ever hand it to the
    # transcriber, never render it.
    (b"\x1aE\xdf\xa3", 0, "webm", "audio/webm"),
    (b"OggS", 0, "ogg", "audio/ogg"),
    (b"fLaC", 0, "flac", "audio/flac"),
    (b"ID3", 0, "mp3", "audio/mpeg"),
)

#: Text longer than this is a transcript of something far past the duration cap, or a
#: provider misbehaving. Clipped rather than refused -- the words already spoken are the
#: memory.
_MAX_TEXT_CHARS = 100_000

#: Languages a caller may pin, ISO-639-1 as the API wants them, mapped to the display
#: name stored in metadata. A closed list on purpose: this value is forwarded to a
#: provider and rendered on a page, so it is re-derived from a key rather than passed
#: through. "" means auto-detect.
LANGUAGES: dict[str, str] = {
    "bn": "bengali",
    "hi": "hindi",
    "en": "english",
    "ur": "urdu",
    "ta": "tamil",
    "te": "telugu",
    "mr": "marathi",
    "gu": "gujarati",
    "pa": "punjabi",
    "ar": "arabic",
    "es": "spanish",
    "fr": "french",
    "de": "german",
    "pt": "portuguese",
    "ru": "russian",
    "ja": "japanese",
    "ko": "korean",
    "zh": "chinese",
    "id": "indonesian",
    "ne": "nepali",
}


def normalise_language(code: str | None) -> str | None:
    """Turn a caller-supplied language code into one we will forward, or None.

    Unknown and empty both mean auto-detect. Never raises: a bad code is a client bug,
    and refusing the whole recording over it would cost the user the words.
    """
    key = (code or "").strip().lower()[:8]
    return key if key in LANGUAGES else None


#: The download name for the kept audio. Server-generated in full, like the object key:
#: a voice note has no filename of its own, and inventing one client-side would put
#: attacker-controlled text into a Content-Disposition header for nothing.
VOICE_FILE_STEM = "voice-note"

#: What a card and a list row show. The AI pipeline replaces this with `ai_label` once it
#: runs; until then a snippet of what was actually said beats "Voice note 3".
_TITLE_CHARS = 80


class TranscriptionError(ValueError):
    """The clip was refused. Written for a human; quotes no audio and no provider text."""


class TranscriptionUnavailable(RuntimeError):
    """No speech-to-text is configured. A deployment fact, not the user's mistake."""


class TranscriptionFailed(RuntimeError):
    """The provider refused or broke. The message is ours; theirs is only logged."""


@dataclass(frozen=True)
class VoiceClip:
    """A validated recording: what container it is and what to call it."""

    data: bytes
    ext: str
    mime_type: str

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def provider_filename(self) -> str:
        """The name handed to the provider, which infers the decoder from the suffix.

        Server-generated in full: nothing the client sent reaches this string.
        """
        return f"voice.{self.ext}"


@dataclass(frozen=True)
class Transcript:
    """What was said, in whatever language it was said in."""

    text: str
    #: Whisper's own detection, e.g. "english", "hindi". None when the model does not
    #: report one -- the transcript is still the memory, the label is metadata.
    language: str | None
    #: Seconds of audio as the provider measured it, not as the client claimed.
    duration: float | None
    model: str


def max_voice_bytes() -> int:
    return settings.MAX_VOICE_NOTE_MB * 1024 * 1024


def transcription_enabled() -> bool:
    return bool(settings.OPENAI_API_KEY)


def inspect(data: bytes) -> VoiceClip:
    """Validate a recording. Raises `TranscriptionError` with an actionable message."""
    if not data:
        raise TranscriptionError("That recording is empty. Hold the button and speak.")

    limit = max_voice_bytes()
    if len(data) > limit:
        raise TranscriptionError(
            f"That recording is {len(data) // 1_048_576}MB — the limit is "
            f"{limit // 1_048_576}MB. Try a shorter note."
        )

    ext, mime = _sniff(data)
    return VoiceClip(data=data, ext=ext, mime_type=mime)


def _sniff(data: bytes) -> tuple[str, str]:
    """Decide the container from its signature alone."""
    for magic, offset, ext, mime in _SIGNATURES:
        if data[offset : offset + len(magic)] == magic:
            return ext, mime
    # RIFF covers WAV, AVI and WebP alike; the form type is what makes it audio.
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav", "audio/wav"
    # ISO base media (MP4/M4A) puts its brand at offset 4, not 0.
    if data[4:8] == b"ftyp":
        return "m4a", "audio/mp4"
    # A bare MPEG audio frame: eleven sync bits set. Checked last because it is the
    # loosest test here and every container above would also pass a byte-level glance.
    if len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3", "audio/mpeg"
    raise TranscriptionError(
        "That doesn't look like an audio recording. Record again, or upload a "
        "webm, m4a, mp3, wav, ogg or flac file."
    )


#: Bars kept for a saved waveform. The client sends this many; anything longer is a
#: client that is not ours, and anything unbounded is a JSONB column someone can grow for
#: free.
WAVEFORM_BUCKETS = 48


def parse_waveform(raw: str | None) -> list[int] | None:
    """Validate the amplitude peaks a recorder sent alongside its clip.

    This is client-written data on its way into a JSONB column and, later, into an SVG on
    the memory page — so it is re-derived rather than trusted: parsed as JSON, required to
    be a flat list of finite numbers, truncated to `WAVEFORM_BUCKETS` and clamped to
    0-100 ints. Nothing from the input survives except magnitudes in that range.

    Returns None for anything that does not fit. It fails closed and silently on purpose:
    a waveform is decoration beside a transcript, and refusing the whole save because the
    picture was malformed would cost the user the words.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        log.info("waveform_rejected", reason="not_json")
        return None
    if not isinstance(data, list) or not data:
        log.info("waveform_rejected", reason="not_a_list")
        return None

    peaks: list[int] = []
    for value in data[:WAVEFORM_BUCKETS]:
        # bool is an int subclass, and `True` in a waveform means the sender is not a
        # recorder. Reject the whole thing rather than coercing it to a bar.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            log.info("waveform_rejected", reason="non_numeric")
            return None
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            log.info("waveform_rejected", reason="not_finite")
            return None
        peaks.append(max(0, min(100, int(number))))
    return peaks


def title_from(text: str) -> str:
    """A short, honest name for the memory, taken from what was actually said.

    Not a summary and not a model call: the pipeline writes `ai_label` moments later.
    This only has to be better than a placeholder for the seconds in between.
    """
    first = text.strip().split("\n", 1)[0].strip()
    if len(first) <= _TITLE_CHARS:
        return first
    head = first[:_TITLE_CHARS]
    cut = head.rsplit(" ", 1)[0] if " " in head else head
    return f"{cut}…"


# Two attempts, not the three the text providers use. A retry here re-uploads the whole
# clip and pays for a second transcription of it, so a persistent failure costs real
# money to confirm.
#
# The retry sits on the *provider call*, not on `transcribe`, so an empty transcript --
# a real answer, and the user's cue to speak up -- is never paid for twice.
@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=8), reraise=True)
async def _call_provider(
    clip: VoiceClip, model: str, response_format: str, language: str | None
) -> Any:
    client = _client()
    extra: dict[str, Any] = {"language": language} if language else {}
    return await client.audio.transcriptions.create(
        model=model,
        file=(clip.provider_filename, clip.data, clip.mime_type),
        response_format=response_format,
        **extra,
    )


async def transcribe(clip: VoiceClip, language: str | None = None) -> Transcript:
    """Transcribe a clip. `language` (ISO-639-1) pins it; None means auto-detect.

    Auto-detection is the default because it is what someone who thinks in one language
    and types in another actually wants. But it *is* a guess, and on a short or noisy clip
    it is a guess that goes badly wrong in a specific way: Bengali speech has been
    observed coming back as Traditional Chinese, confidently and in full. Passing a
    language removes the guess entirely, which is why the recorder offers the choice.
    """
    model = settings.OPENAI_TRANSCRIBE_MODEL
    pinned = normalise_language(language) or normalise_language(
        settings.TRANSCRIBE_LANGUAGE
    )
    # verbose_json is what carries the detected language and the real duration, but only
    # the whisper-* models accept it; the gpt-4o transcribers answer plain json.
    verbose = model.startswith("whisper")

    try:
        response = await _call_provider(
            clip, model, "verbose_json" if verbose else "json", pinned
        )
    except TranscriptionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - one opaque answer for every provider fault
        # The provider's own message is logged, never returned: it can quote request
        # details and, on a misconfiguration, name the account it was rejected for.
        log.warning(
            "voice_transcribe_failed", model=model, error=type(exc).__name__, bytes=clip.size
        )
        raise TranscriptionFailed(
            "Speech-to-text is unavailable right now — your recording wasn't saved. "
            "Try again in a moment."
        ) from exc

    text = (getattr(response, "text", "") or "").strip()[:_MAX_TEXT_CHARS]
    if not text:
        # Silence, or a clip that is all room noise. Refused rather than saved: an empty
        # memory is one the user has to find and delete later.
        raise TranscriptionError(
            "We couldn't hear anything in that recording. Try again, a little closer "
            "to the mic."
        )

    reported = getattr(response, "language", None)
    duration = getattr(response, "duration", None)
    script = script_of(text)

    # What the label ends up being:
    #   1. the language the caller pinned -- no detection happened at all;
    #   2. the script, when the model's answer *contradicts* the characters;
    #   3. otherwise the model's answer, which is the more specific of the two.
    # Only a contradiction demotes the model. "hindi" against Devanagari agrees, and
    # replacing it with "devanagari" would lose real information; "chinese" against
    # Bengali characters does not, and there the response field is simply wrong.
    mismatch = contradicts_script(str(reported) if reported else None, script)

    if pinned:
        label: str | None = LANGUAGES[pinned]
    elif mismatch and script:
        # The model contradicted the characters, so the characters win.
        label = script
    elif reported:
        # Agreeing, or Latin script where `script_of` can say nothing. Keep the model's
        # answer: it is the more specific of the two.
        label = str(reported)
    else:
        label = script

    if mismatch:
        # Worth a line in the log: a rise here is a detection problem, and the only way
        # to tell "one bad clip" from "this model cannot hear this language" is to see it.
        log.warning(
            "voice_language_mismatch", reported=str(reported), script=script, model=model
        )

    log.info(
        "voice_transcribed",
        model=model,
        language=label,
        pinned=bool(pinned),
        duration=duration,
        chars=len(text),
        bytes=clip.size,
    )
    return Transcript(
        text=text,
        language=label,
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        model=model,
    )


def _client() -> Any:
    """Lazily built so importing this module never requires a key."""
    from openai import AsyncOpenAI  # lazy import, mirrors ai/openai.py

    if not settings.OPENAI_API_KEY:
        raise TranscriptionUnavailable("Speech-to-text is not configured.")
    return AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
