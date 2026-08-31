"""A message in another script must reach the lane that can actually answer it.

The bug these pin is a silent one and it took two components to make. The router's
question test reads English opening words, so "আমার নোট দেখাও" was not a question. The
scope gate's normaliser strips everything outside `[a-z0-9' ]`, so the same sentence
became an empty string, which the gate read as an emoji -- a reaction -- and allowed into
the conversation lane. That lane is given no memories by design. So asking about your own
vault in Bengali was answered by a model that had never seen it, and nothing anywhere
reported a problem.
"""
from __future__ import annotations

import pytest

from app.ai.chat.planner import looks_like_question
from app.core import scripts
from app.services.chat_engine import scope

# Measured, not invented -- see the constant's docstring. Greetings sit at 2-5 letters in
# every script tried; anything with a subject and a verb starts at 6.
GREETINGS = ["হ্যালো", "শুভ সকাল", "ধন্যবাদ", "কেমন আছো", "你好", "谢谢", "नमस्ते", "مرحبا"]
REQUESTS = [
    "আমার নোট দেখাও",
    "আমি কি সেভ করেছি",
    "চাকরি সম্পর্কে কি আছে",
    "আমার লিংক গুলো দেখাও",
    "今天視頻就拍到這裡啦",
]


# --- the router ----------------------------------------------------------------------


@pytest.mark.parametrize("message", REQUESTS)
def test_a_non_latin_request_is_routed_to_retrieval(message: str) -> None:
    """Retrieval is the lane that can be wrong safely: with no match it says so."""
    assert looks_like_question(message) is True


@pytest.mark.parametrize("message", GREETINGS)
def test_a_non_latin_greeting_is_not_a_question(message: str) -> None:
    """Sending "hello" to a vector search answers "nothing in your vault" to a hello."""
    assert looks_like_question(message) is False


def test_a_non_latin_question_mark_still_wins() -> None:
    assert looks_like_question("আমি চাকরি সম্পর্কে কি সেভ করেছি?") is True


def test_latin_routing_is_unchanged() -> None:
    assert looks_like_question("what did i save about jobs?") is True
    assert looks_like_question("show me my notes") is True
    assert looks_like_question("hello") is False
    assert looks_like_question("/note buy milk") is False


def test_an_emoji_is_not_a_question() -> None:
    """`isalpha()` is what separates a letter from a sticker."""
    assert looks_like_question("😀🎉") is False
    assert scripts.non_latin_letters("😀🎉") == 0


# --- the gate ------------------------------------------------------------------------


@pytest.mark.parametrize("message", GREETINGS)
def test_a_non_latin_greeting_is_still_social(message: str) -> None:
    verdict = scope.check(message)
    assert verdict.allowed is True
    assert verdict.reason == "social"


def test_a_sticker_is_still_a_reaction() -> None:
    """The behaviour this branch was written for, kept intact."""
    assert scope.check("😀").reason == "social"
    assert scope.check("!!!").reason == "social"


@pytest.mark.parametrize("message", REQUESTS)
def test_an_unreadable_sentence_is_no_longer_waved_through(message: str) -> None:
    """It used to answer `social`, which is what sent it to the model with no vault.

    The router now routes these to retrieval before the gate is consulted at all, so this
    is the second layer rather than the first -- but a gate that reads a Bengali sentence
    as a greeting is wrong whether or not anything depends on it today.
    """
    verdict = scope.check(message)
    assert verdict.allowed is False
    assert verdict.reason == "unreadable_script"


def test_a_blocked_shape_is_still_blocked_first() -> None:
    """Ordering matters: the blocked check runs on the raw text, before normalisation."""
    assert scope.check("translate this note").reason == "blocked_shape"


def test_latin_verdicts_are_unchanged() -> None:
    assert scope.check("hi").reason == "social"
    assert scope.check("what did i save?").allowed is True
    assert scope.check("who is sunny leone").allowed is False


# --- the threshold -------------------------------------------------------------------


def test_the_boundary_is_where_the_measurements_put_it() -> None:
    """Greetings end at 5 letters and requests begin at 6, in every script tried."""
    assert scripts.SHORT_MESSAGE_LETTERS == 5
    assert all(scripts.non_latin_letters(g) <= 5 for g in GREETINGS)
    assert all(scripts.non_latin_letters(r) >= 6 for r in REQUESTS)


def test_short_general_knowledge_goes_to_retrieval_not_the_model() -> None:
    """"সানি লিওন কে" is 6 letters, so it is a question -- and retrieval answers it with
    "nothing in your vault" instead of a biography. Same outcome the gate exists for."""
    assert looks_like_question("সানি লিওন কে") is True


# --- the prompts ---------------------------------------------------------------------


def test_both_lanes_are_told_to_answer_in_the_users_language() -> None:
    """Nothing detects the reply language; the only lever is the instruction itself."""
    from app.ai.chat import chain

    assert "language the person asked in" in chain._SYSTEM
    assert "language the person wrote in" in chain._CONVERSE_SYSTEM
    # A declined request must be declined in the language it was asked in, or the refusal
    # is itself unreadable to the person who triggered it.
    assert "decline in the language it was asked in" in chain._CONVERSE_SYSTEM


# --- the reply nobody translates ------------------------------------------------------


def _no_match(subject: str, days: int | None = None) -> str:
    from app.ai.chat.planner import MemoryQuery
    from app.services.recall_chat import _nothing_found

    return _nothing_found(MemoryQuery(search_text=subject, days=days))


def test_the_no_match_reply_follows_the_question_script() -> None:
    """Produced with no model call, so nothing in the loop could translate it.

    Now the most likely reply to a Bengali question -- non-Latin text routes to
    retrieval -- so leaving it in English would have made the fix visible as a bug.
    """
    assert _no_match("চাকরি").startswith("আপনার ভল্টে")
    assert _no_match("नौकरी").startswith("आपके वॉल्ट")


def test_the_time_window_is_translated_too() -> None:
    assert _no_match("চাকরি", 7).startswith("গত 7 দিনে")
    assert "पिछले 7 दिनों में" in _no_match("नौकरी", 7)


def test_the_subject_is_echoed_verbatim() -> None:
    """It is the user's own words; handing back a translation of their search term is
    telling them they looked for something they did not."""
    assert "“চাকরি”" in _no_match("চাকরি")


def test_english_is_unchanged() -> None:
    assert _no_match("jobs") == "I couldn't find anything about “jobs” in your vault."
    assert _no_match("jobs", 7).endswith("in the last 7 days.")
    assert _no_match("") == "Nothing saved yet."
    assert _no_match("", 7) == "Nothing saved in the last 7 days."


def test_an_unlisted_script_falls_back_to_english() -> None:
    """A missing entry is plain English on purpose: a translation that reads as broken is
    worse than one that was never attempted."""
    assert _no_match("工作").startswith("I couldn't find")


# --- the enrichment prompts -----------------------------------------------------------


def test_every_provider_asks_for_the_content_language() -> None:
    """A Bengali note whose summary and tags come back in English is a card its own
    author reads in translation."""
    import inspect as _inspect

    from app.ai import gemini, openai
    from app.ai.prompts import label_prompt

    for module in (gemini, openai):
        # Twice: the summary and the tags. Both prompts are written out per provider,
        # so a rule added to one and not the other is the failure this catches.
        body = _inspect.getsource(module)
        assert body.count("SAME LANGUAGE as the content") == 2, module.__name__

    assert "SAME LANGUAGE as the content" in label_prompt("x")


def test_the_category_stays_english() -> None:
    """It is an enum checked with `in _CATEGORIES`; a translated word becomes "Other"."""
    import inspect as _inspect

    from app.ai import gemini, openai

    for module in (gemini, openai):
        body = _inspect.getsource(module)
        assert "English word from that list" in body
