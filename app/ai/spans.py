"""Verbatim-span filtering for AI highlights.

A highlight is only useful if the frontend can find it in the stored content and wrap it
in a `<mark>`. Models paraphrase, merge two sentences, fix the author's typos and trim
emoji — all of which produce a span that no longer occurs in the text. Keeping one would
either silently disappear in the UI or, worse, be rendered as a quote the author never
wrote, so the model's output is treated as a *proposal* and checked against the source
before it is stored.

Matching normalizes whitespace and case only. Facebook captions arrive with hard line
breaks and zero-width padding inside otherwise-identical sentences, and a model that
returns the sentence on one line is quoting correctly by any reasonable reading. Anything
looser (fuzzy ratios, prefix matching) starts approving paraphrases again, which is the
one thing this is here to prevent.
"""
from __future__ import annotations

import re

#: Zero-width characters Facebook and Instagram pad captions with.
_INVISIBLE = re.compile(r"[​‌‍﻿]")
_WS = re.compile(r"\s+")

#: Below this a "highlight" is a word, not a point, and marking it just speckles the text.
MIN_SPAN_CHARS = 24
#: Above this the highlight is most of a paragraph, which reads as nothing highlighted.
MAX_SPAN_CHARS = 320
MAX_SPANS = 5


def normalize(text: str) -> str:
    """Casefold and collapse whitespace so line wrapping is not a mismatch."""
    return _WS.sub(" ", _INVISIBLE.sub("", text)).strip().casefold()


def keep_verbatim(spans: list[str], source: str) -> list[str]:
    """Return the spans that genuinely occur in `source`, de-duplicated, order preserved.

    Overlapping spans are dropped rather than merged: two highlights covering the same
    sentence render as one mark anyway, and picking the longer one keeps the intent.
    """
    haystack = normalize(source)
    if not haystack:
        return []

    kept: list[str] = []
    claimed: list[tuple[int, int]] = []
    # Longest first so a sentence wins over a fragment of itself, then restored to the
    # model's own ordering, which follows the document.
    for span in sorted(spans, key=len, reverse=True):
        cleaned = _INVISIBLE.sub("", span).strip().strip('"“”')
        if not MIN_SPAN_CHARS <= len(cleaned) <= MAX_SPAN_CHARS:
            continue
        needle = normalize(cleaned)
        start = haystack.find(needle)
        if start == -1:
            continue
        end = start + len(needle)
        if any(start < c_end and c_start < end for c_start, c_end in claimed):
            continue
        claimed.append((start, end))
        kept.append(cleaned)
        if len(kept) >= MAX_SPANS:
            break

    return sorted(kept, key=lambda s: haystack.find(normalize(s)))
