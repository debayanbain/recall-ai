"""All four card fields in one schema-checked call, instead of four prose ones.

`AIProvider` asks the model four separate questions about the same text -- summarise it,
tag it, categorise it, name it -- and each of them ships the *whole item* again. Input is
where the money is: at 12000 characters a piece that is roughly 12k tokens of input per
item to produce about 120 tokens of output. One call carrying the text once produces the
same four fields for about a quarter of that.

The second gain is the one that shows up in the data rather than the bill. Those four
answers arrive as prose and are recovered by string handling -- `_parse_tags` strips code
fences the model was told not to emit and falls back to splitting on commas, and the
category is matched against a list with everything unrecognised silently becoming
"Other". Both are guesses about what the model meant. **OpenAI structured outputs remove
the guess**: `strict` JSON schema is enforced by the decoder, so `tags` is an array of
strings because it cannot be anything else, and `category` is one of eleven words because
the other words are not generatable.

Why this is a module and not a fifth method on `AIProvider`: the Protocol is structural,
so a provider that has not implemented a new method fails at runtime inside the pipeline
rather than at type-check time, and every fake in the test suite has to grow it. This is a
capability one provider has, so it lives beside `transcription.py` and `vision.py` --
their own switch, their own failure type, and a caller that asks whether it is available.

**It never becomes the only path.** `ProcessingService` falls back to the four-call
enrichment when this is unconfigured *or* when it fails, so a schema change at the
provider, a model that stops supporting `json_schema`, or a bad deploy degrades to the
older behaviour rather than to an item stuck at `failed`.

Highlights are deliberately not in here. They are verbatim quotes checked against the
body afterwards (`keep_verbatim`), they need the full `content` rather than the truncated
enrichment input, and they are the one field where a longer, focused prompt earns its
place.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from app.ai import parsing
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("ai.enrichment")

#: The same list the providers classify against, and the same trailing catch-all. Written
#: into the schema as an enum, so an unlisted value is not merely rejected afterwards --
#: it cannot be produced.
CATEGORIES = (
    "Technology", "Business", "Science", "Health", "Education",
    "Entertainment", "News", "Productivity", "Finance", "Lifestyle", "Other",
)

#: How much of the item the model reads. Matches the per-field prompts it replaces, so
#: switching between the two paths cannot change what the model was shown.
MAX_INPUT = 12000

#: Enough for a 3-sentence summary, seven tags and a 7-word label, with room to spare.
_MAX_OUTPUT_TOKENS = 400

_MAX_TAGS = 7
_MAX_TAG_CHARS = 40

_INSTRUCTIONS = (
    "You are cataloguing one item a person saved to their own memory vault. "
    "Read the content and fill in every field.\n\n"
    # Each rule below is the one its per-field prompt carried, kept word for word in
    # substance: a rule dropped in the move is a regression nothing would report.
    "summary: 2-3 concise sentences, factual and neutral. Write it in the SAME LANGUAGE "
    "as the content -- a note written in Bengali must not come back summarised in "
    "English, or its own author reads their memory in translation.\n"
    "tags: 3-7 short topical tags, lowercase. Use the SAME LANGUAGE as the content.\n"
    "category: exactly one value from the list. This one is ALWAYS the English word, "
    "whatever language the content is in -- it is an enum, not prose.\n"
    "label: name this specific item in 3 to 7 words, the way a person would name it in "
    "a reading list. Be concrete: the product, place, method, claim or number at its "
    "centre. A generic subject area (\"technology\", \"career advice\") is wrong; two "
    "different items must never get the same name. No quotes, no trailing period, no "
    "prefix. Write it in the SAME LANGUAGE as the content."
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "label": {"type": "string"},
    },
    # `strict` mode requires every property to be listed and additional ones forbidden.
    "required": ["summary", "tags", "category", "label"],
    "additionalProperties": False,
}


class EnrichmentFailed(RuntimeError):
    """The provider could not be reached, or answered with something unusable.

    Our own wording, never the provider's: theirs can name the account it rejected, and
    this string is stored on the item.
    """


@dataclass(frozen=True, slots=True)
class Enrichment:
    """The four card fields, already validated."""

    summary: str
    tags: list[str]
    category: str
    label: str


def enrichment_available() -> bool:
    """True when a combined enrichment can be attempted at all.

    Gated on the OpenAI key alone rather than on `AI_PROVIDER`, exactly like
    transcription and vision: a vault summarising with Gemini can still use this, and a
    deployment with neither simply takes the four-call path.
    """
    return settings.ENRICHMENT_COMBINED and bool(settings.OPENAI_API_KEY)


async def enrich(text: str) -> Enrichment:
    """One call, four fields. Raises `EnrichmentFailed` rather than returning a partial.

    A partial would be worse than nothing here: the caller's fallback produces all four,
    and a half-filled `Enrichment` would silently ship an item with no label and no way
    to tell that apart from a model that had nothing to say.
    """
    body = (text or "").strip()
    if not body:
        raise EnrichmentFailed("There was no content to catalogue.")

    try:
        raw = await _call_provider(body[:MAX_INPUT])
    except EnrichmentFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - provider errors can name the account
        log.warning("enrichment_provider_failed", error=type(exc).__name__)
        raise EnrichmentFailed("We couldn't catalogue that item.") from exc

    return _validate(raw)


# Two attempts, not three. Each one re-sends the whole item, and the fallback path is
# still there behind this -- a third try buys less than it costs.
@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=8), reraise=True)
async def _call_provider(text: str) -> Any:
    from openai import AsyncOpenAI  # lazy: importing this module must not need a key

    if not settings.OPENAI_API_KEY:
        raise EnrichmentFailed("Cataloguing is not configured.")

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model=settings.OPENAI_TEXT_MODEL,
        messages=[
            {"role": "system", "content": _INSTRUCTIONS},
            {"role": "user", "content": text},
        ],
        # Low but non-zero, matching the per-field calls this replaces: tag extraction
        # benefits from a little variety and summaries should not drift between runs.
        temperature=0.2,
        max_tokens=_MAX_OUTPUT_TOKENS,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "enrichment", "schema": _SCHEMA, "strict": True},
        },
    )
    content = (response.choices[0].message.content or "").strip()
    if not content:
        # A refusal, or a response cut off by the token ceiling. Either way there is no
        # object to read, and an empty string is not one.
        raise EnrichmentFailed("The model returned nothing to catalogue.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        # Should be unreachable under `strict`, which is exactly why it is checked: the
        # guarantee belongs to the provider, and a provider changing its mind about a
        # guarantee is a thing that happens.
        log.warning("enrichment_not_json", head=content[:120])
        raise EnrichmentFailed("The model's answer could not be read.") from exc
    return parsed


def _validate(raw: Any) -> Enrichment:
    """Re-derive every field from the response. Defence in depth, not distrust of JSON.

    `strict` mode makes the shape guaranteed and the category an enum; none of that
    bounds *length*, and all four values reach a database column, a card and a prompt. So
    the tags are still lowercased, trimmed and capped, the label still goes through the
    same cleaner the per-field path uses, and the category is still checked against the
    list rather than assumed to be in it.
    """
    if not isinstance(raw, dict):
        # `strict` makes the top level an object. Checked here rather than at the parse,
        # because this is the boundary every caller crosses -- including a test stubbing
        # the provider, which is exactly where a wrong shape gets written by hand.
        raise EnrichmentFailed("The model's answer could not be read.")

    summary = str(raw.get("summary") or "").strip()
    label = parsing.clean_label(str(raw.get("label") or ""))

    tags: list[str] = []
    for value in raw.get("tags") or []:
        tag = str(value).strip().lower()
        if tag and len(tag) <= _MAX_TAG_CHARS and tag not in tags:
            tags.append(tag)

    category = str(raw.get("category") or "").strip()
    if category not in CATEGORIES:
        # Unreachable while the enum holds. Kept because "Other" is the honest answer to
        # an unrecognised category and a KeyError is not.
        log.info("enrichment_unknown_category", got=category[:40])
        category = "Other"

    if not summary:
        # The one field with no sensible default: a card with no summary is a card with
        # nothing on it, and the fallback path can produce one.
        raise EnrichmentFailed("The model returned no summary.")

    return Enrichment(summary=summary, tags=tags[:_MAX_TAGS], category=category, label=label)
