"""Reading an uploaded image, so a picture becomes something the vault can find.

Without this an image is stored and downloadable and nothing else: `processing_status`
is `skipped`, there is no text, no embedding, and the only way back to it is remembering
its filename. Describing it -- and transcribing whatever text is *in* it -- gives the
ordinary pipeline something to summarise, tag, label and embed, which is what makes a
screenshot of a receipt findable by asking about the receipt.

Its own capability, like `transcription`, and for the same reason: this is an OpenAI call
with an OpenAI switch (`OPENAI_API_KEY`), not a fifth method on the `AIProvider` Protocol
that every provider would then owe an implementation of. A vault summarising with Gemini
still reads its images.

Two things the caller must respect:

* **The output is a description, not the author's words.** It is stored as `content` so
  search and the embedding can reach it, and marked `item_metadata["content_source"] =
  "vision"` so the reader can say so. Presenting a machine's account of a picture as if
  the user wrote it is the one way this feature can lie.
* **The bytes go to the provider, the presigned URL does not.** Handing OpenAI a signed
  bucket URL would send a live bearer credential to a third party and make our private
  bucket externally fetchable for its lifetime. The image is downloaded and inlined as a
  data URL instead.
"""
from __future__ import annotations

import base64
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("ai.vision")

#: What the model can actually decode. HEIC is in the *upload* allowlist and not here:
#: it is stored and downloadable, it just cannot be read, which is `skipped`, not failed.
_SUPPORTED_MIME = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

#: Clipped rather than refused -- a model that runs away with a description is a cost
#: problem, not a correctness one, and the first paragraphs are the answer.
_MAX_TEXT_CHARS = 20_000

_PROMPT = (
    "Describe this image for someone who cannot see it and who will later search for it "
    "from memory. Cover what it shows, the setting, and anything identifying. Then, if "
    "the image contains any readable text, transcribe it EXACTLY as written under a line "
    "reading 'Text in image:'. Do not guess at text that is illegible, and do not invent "
    "detail that is not visible. Plain prose, no markdown headings."
)

#: A carousel slide is a document, not a photograph, and the two want opposite answers.
#:
#: `_PROMPT` asks what a picture *shows*, which is right for a screenshot of a receipt and
#: wrong for a slide that is a numbered table of job sites: the first run produced two
#: paragraphs about the Brandenburg Gate and a street scene before reaching any of the
#: names, then a loose 'Text in image:' dump where the numbering came apart -- one slide
#: transcribed as "1 2 3 4 5 1 2 3 4 5" with the names detached from their ranks. The
#: names and their order ARE the content; the background is not.
_SLIDE_PROMPT = (
    "This is one slide from a social-media carousel. It is a document, not a photograph: "
    "transcribe it, do not describe it.\n"
    "Rules:\n"
    "1. Start with the slide's own heading on its own line, exactly as written.\n"
    "2. If the slide is a numbered or bulleted list, reproduce EVERY row on its own line "
    "in the slide's own order, as `N. Name — label`, where N is the number printed on the "
    "slide. Never renumber, never merge two rows, never emit a number without its name.\n"
    "3. Copy names, brands, domains and URLs character for character, including things "
    "like relocate.me, Jobbörse.de or arbeitsagentur.de. These are the point of the "
    "slide; a misspelt one is worse than a missing one.\n"
    "4. Keep any closing line, call to action or footnote as its own line.\n"
    "5. Describe the imagery in ONE short sentence at the end, prefixed 'Image: ', and "
    "only if it carries meaning the text does not.\n"
    "6. Transcribe only what is legible. Never invent a row, a rank, a label or a domain."
)


class VisionError(ValueError):
    """This image cannot be read. Not a fault -- the item is `skipped`, not `failed`."""


class VisionUnavailable(RuntimeError):
    """No vision model is configured. A deployment fact, not a bad image."""


class VisionFailed(RuntimeError):
    """The provider refused or broke. Retryable; the message is ours, theirs is logged."""


def vision_enabled() -> bool:
    return bool(settings.OPENAI_API_KEY)


def max_image_bytes() -> int:
    return settings.MAX_VISION_IMAGE_MB * 1024 * 1024


def can_describe(mime_type: str | None, size: int) -> bool:
    """Whether it is worth queueing this image for the worker at all.

    Checked at save time so an unreadable image is marked `skipped` immediately rather
    than making a round trip through the queue to be skipped there.
    """
    return (
        vision_enabled()
        and (mime_type or "") in _SUPPORTED_MIME
        and 0 < size <= max_image_bytes()
    )


# Two attempts, not three: a retry re-uploads the whole image and pays for a second
# reading of it, so a persistent failure costs real money to confirm.
@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=1, max=8), reraise=True)
async def _call_provider(data_url: str, prompt: str, max_tokens: int) -> Any:
    from openai import AsyncOpenAI  # lazy import, mirrors ai/openai.py

    if not settings.OPENAI_API_KEY:
        raise VisionUnavailable("Image reading is not configured.")

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return await client.chat.completions.create(
        model=settings.OPENAI_VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        # Low but non-zero, matching the other providers: a description should not drift
        # between two runs over the same picture.
        temperature=0.2,
        max_tokens=max_tokens,
    )


async def describe_image(
    data: bytes, mime_type: str | None, *, purpose: str = "photo"
) -> str:
    """Read an image. Raises on anything unusable.

    `purpose="slide"` swaps in `_SLIDE_PROMPT` and a larger output budget: a carousel
    slide is a document whose rows are the content, and describing it as a picture buries
    them. See that constant for what the photo prompt actually produced on one.
    """
    if not vision_enabled():
        raise VisionUnavailable("Image reading is not configured.")
    if (mime_type or "") not in _SUPPORTED_MIME:
        raise VisionError(f"Images of type {mime_type or 'unknown'} can't be read.")
    if not data:
        raise VisionError("That image is empty.")
    if len(data) > max_image_bytes():
        raise VisionError(
            f"That image is {len(data) // 1_048_576}MB — too large to read "
            f"(limit {max_image_bytes() // 1_048_576}MB)."
        )

    data_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"

    slide = purpose == "slide"
    # A ten-row table with labels, a heading and a footer does not fit the photo budget;
    # a clipped transcription loses the last rows, which is the half nobody notices.
    try:
        response = await _call_provider(
            data_url, _SLIDE_PROMPT if slide else _PROMPT, 1600 if slide else 800
        )
    except VisionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - one opaque answer for every provider fault
        # The provider's own message is logged, never stored: it can quote request
        # details and, on a misconfiguration, name the account it was rejected for.
        log.warning(
            "vision_describe_failed",
            model=settings.OPENAI_VISION_MODEL,
            error=type(exc).__name__,
            bytes=len(data),
        )
        raise VisionFailed("Couldn't read that image just now.") from exc

    text = (response.choices[0].message.content or "").strip()[:_MAX_TEXT_CHARS]
    if not text:
        # The model had nothing to say. Treated as unreadable rather than as a failure:
        # retrying costs another reading to reach the same silence.
        raise VisionError("Nothing readable was found in that image.")

    log.info(
        "vision_described",
        model=settings.OPENAI_VISION_MODEL,
        chars=len(text),
        bytes=len(data),
    )
    return text
