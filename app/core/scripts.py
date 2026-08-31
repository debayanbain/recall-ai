"""Recognising that a message is not written in the Latin alphabet.

Two places need this and they must not drift apart: the router, deciding whether a plain
message is a question worth searching the vault for, and the chat gate, deciding whether
it may reach the conversation model. Both were built out of English word patterns, and
both failed the same way on a message in another script -- silently, and in opposite
directions.

The failure was concrete. `_KEEP_RE` in the scope gate strips everything outside
`[a-z0-9' ]`, so "আমার নোট দেখাও" normalises to a run of spaces. That is
indistinguishable from an emoji-only message, which the gate treats as a reaction and
waves through as social -- so every Bengali sentence was classified as a greeting and
answered by the chat model, which never touches the vault. Meanwhile the router's
question test matches English opening words, so the same sentence was never routed to
retrieval either. Asking about your own memories in Bengali quietly did nothing.

What this module supplies is deliberately *not* language identification. It identifies
**scripts** -- which Unicode block the characters fall in -- an observable fact needing no
model, no table and no network. Script is not language: Hindi and Marathi share
Devanagari, and no amount of character counting separates them. Everything here is
honest about that limit.

Transcription uses the same primitives for a different question -- whether the language a
speech model reported can even be written in the characters it returned -- which is how a
Bengali voice note transcribed as Chinese was caught. One home for both, or the two
copies drift and only one gets the fix.
"""
from __future__ import annotations

#: Letters, in a non-Latin script, at or below which a message is a reaction rather than
#: a request. Measured rather than guessed -- greetings and requests separate cleanly:
#:
#:     হ্যালো 3   শুভ সকাল 5   ধন্যবাদ 5   কেমন আছো 5   你好 2   नमस्ते 4   مرحبا 5
#:     সানি লিওন কে 6   আমার নোট দেখাও 8   আমি কি সেভ করেছি 8   今天視頻就拍到這裡啦 10
#:
#: Greetings land at 2-5 in every script tried; anything carrying a subject and a verb
#: starts at 6. Note that Bengali vowel signs are combining marks, not letters, so the
#: count is lower than the visible character count -- another reason to measure.
#:
#: Counted in *letters*, not words, because Chinese, Japanese and Thai are written with
#: no spaces at all: a whitespace word count reads a whole sentence as one word and would
#: send every Chinese question to the wrong lane.
#:
#: The trade runs the safe way round. Too low sends a greeting to retrieval, which answers
#: "I couldn't find anything about that in your vault" -- clumsy, visible, recoverable.
#: Too high sends a real question to the conversation lane, which cannot see the vault and
#: will say so while the answer sat in it the whole time. It also means a short piece of
#: general knowledge ("সানি লিওন কে", 6) goes to retrieval and is answered with "nothing
#: in your vault" rather than a biography -- the same outcome the scope gate exists for.
SHORT_MESSAGE_LETTERS = 5


def non_latin_letters(text: str) -> int:
    """How many letters in this text are outside ASCII.

    `str.isalpha()` is what separates a letter from an emoji or a punctuation mark, which
    matters: an emoji-only message is a reaction and must keep behaving like one.
    """
    return sum(1 for char in text if ord(char) > 127 and char.isalpha())


def is_non_latin(text: str) -> bool:
    """True when the message is written in a script the English patterns cannot read."""
    return non_latin_letters(text) > 0


def is_short_reaction(text: str) -> bool:
    """True for a greeting, an acknowledgement, an emoji -- anything too small to be a ask.

    Pure punctuation and emoji answer True as well, which preserves the gate's existing
    treatment of a sticker as social.
    """
    return non_latin_letters(text) <= SHORT_MESSAGE_LETTERS


#: Unicode blocks that identify a language's script, for labelling a transcript when the
#: model does not report one. Ranges only -- this is not language identification, it is
#: script identification, which is all that can honestly be claimed from characters:
#: Hindi and Marathi share Devanagari, so both answer "devanagari" territory and the
#: first entry wins. Enough to tell Bengali from Chinese, which is the failure that
#: matters here.
_SCRIPTS: tuple[tuple[str, int, int], ...] = (
    ("bengali", 0x0980, 0x09FF),
    ("devanagari", 0x0900, 0x097F),
    ("gurmukhi", 0x0A00, 0x0A7F),
    ("gujarati", 0x0A80, 0x0AFF),
    ("tamil", 0x0B80, 0x0BFF),
    ("telugu", 0x0C00, 0x0C7F),
    ("kannada", 0x0C80, 0x0CFF),
    ("malayalam", 0x0D00, 0x0D7F),
    ("sinhala", 0x0D80, 0x0DFF),
    ("thai", 0x0E00, 0x0E7F),
    ("arabic", 0x0600, 0x06FF),
    ("hebrew", 0x0590, 0x05FF),
    ("greek", 0x0370, 0x03FF),
    ("cyrillic", 0x0400, 0x04FF),
    ("korean", 0xAC00, 0xD7AF),
    ("japanese", 0x3040, 0x30FF),
    ("chinese", 0x4E00, 0x9FFF),
)


def script_of(text: str) -> str | None:
    """Which script a text is written in, or None for plain Latin/unknown.

    Used to label a transcript when the model reports no language of its own, to notice
    when a *detected* language disagrees with the characters that came back -- which is
    exactly the shape of the bug this exists for, Bengali speech returned as Han
    characters -- and to pick which translation of a fixed reply to send back.

    The first matching block per character wins, and the commonest block across the whole
    text is the answer, so a Bengali sentence carrying an English brand name is still
    Bengali.
    """
    counts: dict[str, int] = {}
    for char in text:
        point = ord(char)
        for name, low, high in _SCRIPTS:
            if low <= point <= high:
                counts[name] = counts.get(name, 0) + 1
                break
    if not counts:
        return None
    return max(counts, key=lambda name: counts[name])


#: The script each language the model might name is written in. Used to tell "the model
#: was less specific than the characters" (hindi vs devanagari -- fine, keep hindi) from
#: "the model contradicted the characters" (chinese vs bengali -- the model is wrong).
#: Languages written in Latin are absent and answer None, which matches `script_of`.
_LANGUAGE_SCRIPT: dict[str, str] = {
    "bengali": "bengali",
    "assamese": "bengali",
    "hindi": "devanagari",
    "marathi": "devanagari",
    "nepali": "devanagari",
    "sanskrit": "devanagari",
    "punjabi": "gurmukhi",
    "gujarati": "gujarati",
    "tamil": "tamil",
    "telugu": "telugu",
    "kannada": "kannada",
    "malayalam": "malayalam",
    "sinhala": "sinhala",
    "thai": "thai",
    "arabic": "arabic",
    "urdu": "arabic",
    "persian": "arabic",
    "pashto": "arabic",
    "hebrew": "hebrew",
    "yiddish": "hebrew",
    "greek": "greek",
    "russian": "cyrillic",
    "ukrainian": "cyrillic",
    "bulgarian": "cyrillic",
    "serbian": "cyrillic",
    "macedonian": "cyrillic",
    "belarusian": "cyrillic",
    "kazakh": "cyrillic",
    "mongolian": "cyrillic",
    "korean": "korean",
    "japanese": "japanese",
    "chinese": "chinese",
    "cantonese": "chinese",
}


def contradicts_script(reported: str | None, script: str | None) -> bool:
    """True when the model's language cannot be written in the script that came back.

    Deliberately narrower than "they differ". Hindi is written in Devanagari, so
    `reported="hindi"` against `script="devanagari"` agrees -- the model was simply more
    specific than characters can be, and throwing that away would make every Hindi note
    say "devanagari". A Bengali script with `reported="chinese"` is a real contradiction,
    and that is the case worth acting on.
    """
    if not reported or not script:
        return False
    expected = _LANGUAGE_SCRIPT.get(reported.strip().lower())
    # An unmapped language is one written in Latin, so any non-Latin script contradicts it.
    return expected != script
