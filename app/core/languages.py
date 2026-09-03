"""The one list of languages this service names, and the only place it is written down.

It exists in `core/` rather than beside its first caller because two unrelated features
now depend on the same closed set: a voice note's transcription language, which is
forwarded to a provider and rendered on a page, and `ENRICHMENT_LANGUAGE`, which decides
what language a card's summary, tags and label come back in. A second copy of this table
is a copy that gets a language added to one of them.

Closed on purpose in both cases. Neither value is ever passed through as typed: the code
is a key, and what reaches a provider (or a prompt) is the name this file maps it to.
"""
from __future__ import annotations

#: ISO-639-1 as the transcription API wants them, mapped to the display name stored in
#: metadata and interpolated into prompts. "" means auto-detect / follow the content.
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


def language_name(code: str) -> str | None:
    """The English name for a code, or None when the code is not one we know.

    Returns the name rather than the code because every consumer wants prose: a prompt
    saying "write it in bn" is a prompt asking the model to guess.
    """
    return LANGUAGES.get(code.strip().lower())
