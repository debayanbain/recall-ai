"""Defensive parsing of model prose into the shapes the pipeline stores.

Shared by both providers on purpose: `_parse_tags` predates this and stays duplicated in
each provider because its behaviour is pinned by that provider's tests, but a *new*
output format written twice is a format that drifts apart on the next edit.

Models return code fences, a leading "Sure, here are", smart quotes and a trailing period
however firmly the prompt says otherwise, so every helper here assumes the response is
prose that happens to contain the answer.
"""
from __future__ import annotations

import json
import re

from app.core.logging import get_logger

log = get_logger("ai.parsing")

_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)
#: A quoted string, tolerating the smart quotes models emit inside JSON-ish output.
_QUOTED = re.compile(r'"([^"]{4,})"|“([^”]{4,})”')
MAX_LABEL_CHARS = 60


def _strip_fences(raw: str) -> str:
    return _FENCE.sub("", raw).strip()


def parse_string_list(raw: str) -> list[str]:
    """Read a JSON array of strings, falling back to every quoted run in the text.

    The fallback matters more here than for tags: a highlight is a whole sentence, so
    comma-splitting a malformed response would slice quotes in half and produce spans
    that can never match the source.
    """
    cleaned = _strip_fences(raw)
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        log.warning("string_list_parse_failed", raw=raw[:200])
    return [(a or b).strip() for a, b in _QUOTED.findall(cleaned)]


def clean_label(raw: str) -> str:
    """First line, unquoted, unpunctuated, capped — or "" if the model said nothing."""
    line = _strip_fences(raw).splitlines()[0] if _strip_fences(raw) else ""
    line = line.strip().strip('"“”').strip()
    # Models answer "Label: Sweden job platforms" about a fifth of the time.
    line = re.sub(r"^(label|title|name)\s*[:\-]\s*", "", line, flags=re.IGNORECASE)
    line = line.rstrip(".").strip()
    if len(line) > MAX_LABEL_CHARS:
        line = line[: MAX_LABEL_CHARS - 1].rstrip() + "…"
    return line
