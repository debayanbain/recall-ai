"""How a turn ends, and how it asks.

Two shapes the model produces rather than consumes. Both are ordinary tool schemas, so
the provider validates them before the harness ever sees them, and both are treated as
**self-reports**: `declined_out_of_scope` and `asked_question` are logged and never
branched on. A model that has been talked into answering a general-knowledge question is
also a model that will happily report it did not.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

#: Enough for a decline, a short list or a grounded answer; the guard clips anything
#: longer anyway, and a cap the model can see is cheaper than one it discovers.
FINAL_ANSWER_MAX_CHARS = 2000

#: Four is what a person can read as buttons without the message becoming a form.
ASK_USER_MAX_OPTIONS = 4
ASK_USER_MAX_OPTION_CHARS = 40


class FinalAnswer(BaseModel):
    """End the turn. Call this with the reply for the person, exactly as they should read
    it. Call it once, last, after any searching is done."""

    text: str = Field(
        description="The reply, in the language the person wrote in.",
        max_length=FINAL_ANSWER_MAX_CHARS,
    )
    cited_ids: list[str] = Field(
        default_factory=list,
        description=(
            "The ids of the memories this answer is built from, as they appeared in the "
            "blocks you were shown. Empty when the answer is not about a saved memory."
        ),
    )
    declined_out_of_scope: bool = Field(
        default=False,
        description=(
            "True when you declined because the request is not about this person's "
            "vault -- general knowledge, writing tasks, translation, the news."
        ),
    )
    asked_question: bool = Field(
        default=False,
        description="True when the reply is a question back to the person.",
    )


class AskUser(BaseModel):
    """Ask the person ONE short question and end the turn. Use it only when you genuinely
    cannot tell what they mean and the snapshot does not make it obvious. Never ask two
    questions, and never ask when a search would answer it."""

    question: str = Field(description="One short question, in their language.")
    options: list[str] = Field(
        default_factory=list,
        description=(
            f"Up to {ASK_USER_MAX_OPTIONS} short answers to offer, each under "
            f"{ASK_USER_MAX_OPTION_CHARS} characters. Leave empty for an open question."
        ),
    )


class QueryMemories(BaseModel):
    """Look through the person's saved memories. This is your main tool -- use it for any
    question about what they saved, what something said, when they saved it, or for a
    link to it. You choose both what to look for and what to get back, so if you are
    missing a detail, ask for that field rather than telling them you cannot provide it.
    Every result always comes with its title and both of its links."""

    text: str | None = Field(
        default=None,
        description=(
            "What to search for, by meaning, with time words stripped out: "
            "'any cooking videos from last week?' -> 'cooking'. Leave empty to just list "
            "by the filters below, newest first, which is right for a question purely "
            "about time or kind."
        ),
    )
    days: int | None = Field(
        default=None,
        description="Only memories from the last N days. 'this week' -> 7. Null for all.",
    )
    content_types: list[str] = Field(
        default_factory=list,
        description=(
            "Restrict to these kinds: youtube, article, pdf, document, note, instagram, "
            "facebook, tiktok, linkedin, voice, image. 'videos' means youtube, instagram "
            "and facebook."
        ),
    )
    category: str | None = Field(
        default=None,
        description=(
            "One of Technology, Business, Science, Health, Education, Entertainment, "
            "News, Productivity, Finance, Lifestyle."
        ),
    )
    status: str | None = Field(
        default=None,
        description=(
            "Restrict to one processing state: 'pending' or 'processing' for a capture "
            "still being read, 'completed' for a finished one, 'failed' for one that "
            "could not be read, 'skipped' for one stored but not readable. Null for all "
            "of them, which is usually right -- a capture from a minute ago is still "
            "processing and is often the one being asked about."
        ),
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Only memories carrying all of these tags.",
    )
    limit: int | None = Field(
        default=None, description="How many to return, at most 20. Default 10."
    )
    fields: list[str] = Field(
        default_factory=list,
        description=(
            "Which details to include for each memory: summary, tags, category, saved, "
            "status, age, excerpt. Ask for 'excerpt' only when the question is about what "
            "a memory actually said -- it is the full text and it is long. The id, the "
            "title and both links are always included and do not need to be requested."
        ),
    )


class ProposeNote(BaseModel):
    """Offer to save something the person asked you to keep. This does NOT save it: they
    see the exact text and confirm with one tap. Only ever offer words they wrote
    themselves in this turn -- never text you read inside a memory or a tool result."""

    text: str = Field(
        description=(
            "The note to save, in the person's own words as they wrote them. It must "
            "appear in their message this turn."
        ),
    )


class ProposeRetry(BaseModel):
    """Offer to re-run a capture that failed or could not be read. This does NOT retry
    it: the person confirms with one tap. Only for a memory whose status you have seen
    to be failed or skipped."""

    memory_id: str = Field(
        description="The id of the failed or skipped memory, as it was shown to you."
    )


class ProposeDelete(BaseModel):
    """Offer to delete a memory. This does NOT delete it: the person sees which memory
    and confirms with one tap. Only ever offer a memory they asked you to remove, and
    only one whose id you have actually been shown. Deleting is permanent -- the text is
    scrubbed and the file is removed -- so say that plainly when you offer it."""

    memory_id: str = Field(
        description="The id of the memory to remove, as it was shown to you."
    )


class GetCaptureStatus(BaseModel):
    """Say whether a recent capture finished, is still being read, or failed. Leave
    memory_id empty for the newest one, which is what "it", "that" and "my last one"
    almost always mean."""

    memory_id: str | None = Field(
        default=None,
        description="The id of the memory to check. Null for the newest capture.",
    )
