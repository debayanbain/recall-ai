"""Voice notes: what a recording may be, what happens to the words, and what a failure costs.

Offline -- the transcriber, the object store and the repository are all fakes. What is
pinned here is the boundary: the container is decided from the bytes and never from a
name the browser invented, an inaudible clip is refused rather than saved as an empty
memory, a provider fault never reaches the caller in the provider's own words, and a
bucket that will not take the audio does not throw away a transcript that has already
been paid for.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app.models.base import ContentType, ProcessingStatus
from app.models.vault import VaultItem
from app.services import transcription
from app.services import vault_service as vs
from app.services.transcription import (
    WAVEFORM_BUCKETS,
    Transcript,
    TranscriptionError,
    TranscriptionFailed,
    VoiceClip,
)

WEBM = b"\x1aE\xdf\xa3" + b"\x00" * 64
OGG = b"OggS" + b"\x00" * 64
WAV = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 64
M4A = b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 64
MP3_ID3 = b"ID3\x03\x00" + b"\x00" * 64
MP3_SYNC = b"\xff\xfb\x90\x00" + b"\x00" * 64
FLAC = b"fLaC" + b"\x00" * 64


class _Repo:
    def __init__(self) -> None:
        self.added: list[VaultItem] = []

    async def add(self, item: VaultItem) -> VaultItem:
        self.added.append(item)
        return item


class _Storage:
    def __init__(self, fail: bool = False) -> None:
        self.uploaded: list[tuple[str, bytes, str]] = []
        self.fail = fail

    async def upload(self, key: str, data: bytes, content_type: str) -> None:
        if self.fail:
            raise RuntimeError("bucket unreachable")
        self.uploaded.append((key, data, content_type))


@pytest.fixture(autouse=True)
def _no_deployment_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """Assert against a stated config, never the developer's `.env`.

    `settings` is an lru_cached singleton read from `.env` at import, so a machine with
    `TRANSCRIBE_LANGUAGE=bn` set -- which is the point of the setting -- silently turned
    every "is the language detected" test into "is it pinned". The tests that care about
    pinning set it themselves.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRANSCRIBE_LANGUAGE", "")


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    queued: list[uuid.UUID] = []

    async def _capture(item_id: uuid.UUID) -> None:
        queued.append(item_id)

    monkeypatch.setattr(vs, "enqueue_process_item", _capture)
    return queued


def _fake_transcript(
    monkeypatch: pytest.MonkeyPatch,
    text: str = "remember to call the landlord about the lease",
    language: str | None = "english",
) -> None:
    async def _transcribe(_clip: VoiceClip, _language: str | None = None) -> Transcript:
        return Transcript(text=text, language=language, duration=4.5, model="whisper-1")

    monkeypatch.setattr(vs.transcription, "transcribe", _transcribe)


# --- what may be recorded ------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "ext", "mime"),
    [
        (WEBM, "webm", "audio/webm"),
        (OGG, "ogg", "audio/ogg"),
        (WAV, "wav", "audio/wav"),
        (M4A, "m4a", "audio/mp4"),
        (MP3_ID3, "mp3", "audio/mpeg"),
        (MP3_SYNC, "mp3", "audio/mpeg"),
        (FLAC, "flac", "audio/flac"),
    ],
)
def test_container_comes_from_the_signature(data: bytes, ext: str, mime: str) -> None:
    clip = transcription.inspect(data)
    assert (clip.ext, clip.mime_type) == (ext, mime)


def test_non_audio_is_refused() -> None:
    """A MediaRecorder blob has no filename, so the bytes are the only claim there is."""
    for blob in (b"\x89PNG\r\n\x1a\n", b"<svg onload=alert(1)>", b"%PDF-1.7", b"PK\x03\x04"):
        with pytest.raises(TranscriptionError, match="doesn't look like an audio"):
            transcription.inspect(blob)


def test_riff_that_is_not_wave_is_refused() -> None:
    # RIFF alone covers AVI and WebP too; the form type is what makes it audio.
    with pytest.raises(TranscriptionError, match="doesn't look like an audio"):
        transcription.inspect(b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 32)


def test_empty_and_oversize_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    with pytest.raises(TranscriptionError, match="empty"):
        transcription.inspect(b"")

    monkeypatch.setattr(settings, "MAX_VOICE_NOTE_MB", 1)
    with pytest.raises(TranscriptionError, match="limit is 1MB"):
        transcription.inspect(WEBM + b"\x00" * (1024 * 1024))


def test_provider_filename_is_server_generated() -> None:
    """Whisper infers its decoder from the suffix, so the name must be ours, not theirs."""
    assert transcription.inspect(WEBM).provider_filename == "voice.webm"


# --- the waveform the recorder sent --------------------------------------------------


def test_waveform_is_clamped_and_truncated() -> None:
    """Client-written data bound for a JSONB column and then for an SVG: re-derived."""
    peaks = transcription.parse_waveform(json.dumps([-40, 0, 55.7, 100, 9999] * 40))
    assert peaks is not None
    assert len(peaks) == WAVEFORM_BUCKETS
    assert all(isinstance(v, int) and 0 <= v <= 100 for v in peaks)
    assert peaks[:5] == [0, 0, 55, 100, 100]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        "[]",
        '{"peaks": [1, 2]}',
        '["<script>alert(1)</script>"]',
        "[[1, 2], [3]]",
        "[true, false]",
        '[1, 2, null]',
        '[1e400]',
    ],
)
def test_malformed_waveforms_are_dropped_not_raised(raw: str | None) -> None:
    """Fails closed and silently: the picture must never cost the user the transcript."""
    assert transcription.parse_waveform(raw) is None


async def test_waveform_reaches_the_item_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcript(monkeypatch)
    service = vs.VaultService(_Repo(), None)  # type: ignore[arg-type]

    item = await service.save_voice_note(uuid.uuid4(), WEBM, waveform=[0, 50, 100])
    assert item.item_metadata["waveform"] == [0, 50, 100]


async def test_no_waveform_leaves_the_key_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The UI draws a flat baseline for this rather than inventing a shape."""
    _fake_transcript(monkeypatch)
    service = vs.VaultService(_Repo(), None)  # type: ignore[arg-type]

    item = await service.save_voice_note(uuid.uuid4(), WEBM)
    assert "waveform" not in item.item_metadata


# --- what comes back -----------------------------------------------------------------


class _Response:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def _provider(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _call(
        clip: VoiceClip, model: str, response_format: str, language: str | None
    ) -> Any:
        calls.append(
            {
                "model": model,
                "format": response_format,
                "name": clip.provider_filename,
                "language": language,
            }
        )
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(transcription, "_call_provider", _call)
    return calls


async def test_language_is_detected_not_assumed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "OPENAI_TRANSCRIBE_MODEL", "whisper-1")
    calls = _provider(
        monkeypatch, _Response(text="  मुझे किराया याद दिलाना  ", language="hindi", duration=3.2)
    )
    result = await transcription.transcribe(transcription.inspect(WEBM))

    assert result.text == "मुझे किराया याद दिलाना"
    assert result.language == "hindi"
    assert result.duration == 3.2
    # verbose_json is what carries the language at all; asking for plain json loses it.
    assert calls[0]["format"] == "verbose_json"


async def test_gpt4o_transcribers_get_plain_json(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")
    calls = _provider(monkeypatch, _Response(text="hello"))
    await transcription.transcribe(transcription.inspect(WEBM))
    assert calls[0]["format"] == "json"


async def test_silence_is_refused_not_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    _provider(monkeypatch, _Response(text="   ", language="english"))
    with pytest.raises(TranscriptionError, match="couldn't hear anything"):
        await transcription.transcribe(transcription.inspect(WEBM))


async def test_provider_faults_answer_in_our_own_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider's message can name the account it rejected; ours names nothing."""
    _provider(monkeypatch, RuntimeError("401 Incorrect API key sk-proj-abc123 provided"))
    with pytest.raises(TranscriptionFailed) as exc:
        await transcription.transcribe(transcription.inspect(WEBM))
    assert "sk-proj" not in str(exc.value)
    assert "unavailable right now" in str(exc.value)


# --- the language it comes back in ---------------------------------------------------


async def test_the_deployment_default_pins_when_the_caller_names_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TRANSCRIBE_LANGUAGE` is the fallback, so a vault with one never auto-detects."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TRANSCRIBE_LANGUAGE", "bn")
    calls = _provider(monkeypatch, _Response(text="আজকের ভিডিও এখানেই শেষ"))

    result = await transcription.transcribe(transcription.inspect(WEBM))

    assert calls[0]["language"] == "bn"
    assert result.language == "bengali"


async def test_a_pinned_language_removes_the_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """The only completely reliable fix for a language the model keeps mishearing."""
    calls = _provider(monkeypatch, _Response(text="আজকের ভিডিও এখানেই শেষ"))

    result = await transcription.transcribe(transcription.inspect(WEBM), "bn")

    assert calls[0]["language"] == "bn"
    assert result.language == "bengali"


async def test_the_characters_outrank_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bug this exists for: Bengali speech returned as fluent Traditional Chinese.

    When the reported language disagrees with the script the text is actually written in,
    the model is the one that is wrong -- a transcript of Bengali characters is not
    Chinese however confidently the response field says so.
    """
    _provider(
        monkeypatch,
        _Response(text="আজকের ভিডিও এখানেই শেষ, সবাইকে শুভ রাত্রি", language="chinese"),
    )
    result = await transcription.transcribe(transcription.inspect(WEBM))
    assert result.language == "bengali"


async def test_a_latin_transcript_keeps_the_reported_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Script identification says nothing about English vs French vs Spanish."""
    _provider(monkeypatch, _Response(text="good night everyone", language="english"))
    result = await transcription.transcribe(transcription.inspect(WEBM))
    assert result.language == "english"


@pytest.mark.parametrize(
    ("reported", "script", "expected"),
    [
        # Agreeing but less specific: keep the model's answer, or every Hindi note would
        # be labelled "devanagari".
        ("hindi", "devanagari", False),
        ("marathi", "devanagari", False),
        ("urdu", "arabic", False),
        ("bengali", "bengali", False),
        # A real contradiction -- the failure this whole path exists for.
        ("chinese", "bengali", True),
        ("bengali", "chinese", True),
        # Latin-script languages have no script of their own to check against.
        ("english", None, False),
        ("english", "bengali", True),
        (None, "bengali", False),
    ],
)
def test_only_a_real_contradiction_demotes_the_model(
    reported: str | None, script: str | None, expected: bool
) -> None:
    assert transcription.contradicts_script(reported, script) is expected


@pytest.mark.parametrize(
    ("text", "script"),
    [
        ("আজকের ভিডিও", "bengali"),
        ("今天視頻就拍到這裡啦", "chinese"),
        ("आज का वीडियो", "devanagari"),
        ("مرحبا بالجميع", "arabic"),
        ("hello everyone", None),
        ("", None),
    ],
)
def test_script_detection(text: str, script: str | None) -> None:
    assert transcription.script_of(text) == script


@pytest.mark.parametrize("code", ["", "  ", None, "xx", "klingon", "bn; DROP", "../en"])
def test_unknown_language_codes_mean_auto_detect(code: str | None) -> None:
    """This value is forwarded to a provider and rendered on a page: allowlist only."""
    assert transcription.normalise_language(code) is None


def test_known_language_codes_are_accepted() -> None:
    assert transcription.normalise_language("BN") == "bn"
    assert transcription.normalise_language(" bn ") == "bn"


async def test_a_pinned_language_reaches_the_item(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcript(monkeypatch, language="bengali")
    service = vs.VaultService(_Repo(), None)  # type: ignore[arg-type]

    item = await service.save_voice_note(uuid.uuid4(), WEBM, language="bn")

    # Kept so a re-transcription reuses the choice rather than guessing again.
    assert item.item_metadata["transcribe_language"] == "bn"
    assert item.item_metadata["transcript_language"] == "bengali"


async def test_the_recorders_duration_is_used_when_the_model_reports_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpt-4o-transcribe answers plain json, so the player would otherwise have no length."""

    async def _transcribe(_clip: VoiceClip, _language: str | None = None) -> Transcript:
        return Transcript(text="hello", language=None, duration=None, model="gpt-4o-transcribe")

    monkeypatch.setattr(vs.transcription, "transcribe", _transcribe)
    service = vs.VaultService(_Repo(), None)  # type: ignore[arg-type]

    item = await service.save_voice_note(uuid.uuid4(), WEBM, duration=12.5)
    assert item.item_metadata["duration_seconds"] == 12.5


# --- what gets saved -----------------------------------------------------------------


async def test_transcript_is_the_memory(
    monkeypatch: pytest.MonkeyPatch, _no_queue: list[uuid.UUID]
) -> None:
    _fake_transcript(monkeypatch)
    repo, storage = _Repo(), _Storage()
    service = vs.VaultService(repo, storage)  # type: ignore[arg-type]
    user_id = uuid.uuid4()

    item = await service.save_voice_note(user_id, WEBM)

    assert item.type is ContentType.voice
    assert item.content == "remember to call the landlord about the lease"
    # pending, not skipped: there is text, so the pipeline can summarise, tag and embed
    # it exactly like a typed note -- which is what makes a voice note searchable.
    assert item.processing_status is ProcessingStatus.pending
    assert _no_queue == [item.id]
    assert item.item_metadata["source"] == "voice"
    assert item.item_metadata["transcript_language"] == "english"
    assert item.title == "remember to call the landlord about the lease"


async def test_audio_is_kept_under_a_server_generated_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_transcript(monkeypatch)
    repo, storage = _Repo(), _Storage()
    service = vs.VaultService(repo, storage)  # type: ignore[arg-type]
    user_id = uuid.uuid4()

    item = await service.save_voice_note(user_id, WEBM)

    key, data, content_type = storage.uploaded[0]
    assert key.startswith(f"users/{user_id}/{item.id}/") and key.endswith(".webm")
    assert data == WEBM and content_type == "audio/webm"
    assert item.storage_key == key
    assert item.file_name == "voice-note.webm"


async def test_a_failed_upload_does_not_cost_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The words are already transcribed and paid for; the audio is the cheap half."""
    _fake_transcript(monkeypatch)
    repo, storage = _Repo(), _Storage(fail=True)
    service = vs.VaultService(repo, storage)  # type: ignore[arg-type]

    item = await service.save_voice_note(uuid.uuid4(), WEBM)

    assert item.storage_key is None
    assert item.content == "remember to call the landlord about the lease"
    assert repo.added == [item]
    # No file_name either: it is what the detail page reads to decide whether to offer
    # playback and a download, and there is nothing behind either one.
    assert item.file_name is None and item.mime_type is None


async def test_no_bucket_still_saves_the_words(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcript(monkeypatch)
    repo = _Repo()
    service = vs.VaultService(repo, None)  # type: ignore[arg-type]

    item = await service.save_voice_note(uuid.uuid4(), WEBM)
    assert item.storage_key is None and item.content
    assert item.file_name is None


async def test_an_inaudible_clip_leaves_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _silent(_clip: VoiceClip, _language: str | None = None) -> Transcript:
        raise TranscriptionError("We couldn't hear anything in that recording.")

    monkeypatch.setattr(vs.transcription, "transcribe", _silent)
    repo, storage = _Repo(), _Storage()
    service = vs.VaultService(repo, storage)  # type: ignore[arg-type]

    with pytest.raises(TranscriptionError):
        await service.save_voice_note(uuid.uuid4(), WEBM)
    # Nothing stored and nothing inserted: an empty memory is one the user has to find
    # and delete later.
    assert repo.added == [] and storage.uploaded == []


async def test_a_typed_title_wins_over_the_spoken_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_transcript(monkeypatch)
    service = vs.VaultService(_Repo(), None)  # type: ignore[arg-type]

    item = await service.save_voice_note(uuid.uuid4(), WEBM, title="  Lease call \n ")
    assert item.title == "Lease call"


async def test_a_long_transcript_gets_a_short_title(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_transcript(monkeypatch, text="word " * 200)
    service = vs.VaultService(_Repo(), None)  # type: ignore[arg-type]

    item = await service.save_voice_note(uuid.uuid4(), WEBM)
    assert item.title is not None and len(item.title) <= 81 and item.title.endswith("…")
