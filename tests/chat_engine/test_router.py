"""Every branch of `route`, in the order the router checks them.

The ordering cases matter more than the pattern cases: a pattern that stops matching
loses one phrasing, while a branch checked in the wrong order silently changes what gets
saved.
"""
from __future__ import annotations

import pytest

from app.services.chat_engine.router import Intent, route, wants_detail

URL = "https://x.com/a"


# --- attachments win outright --------------------------------------------------------


def test_an_attachment_is_a_capture() -> None:
    assert route("look at this", has_attachment=True) is Intent.CAPTURE


def test_an_attachment_with_no_caption_is_still_a_capture() -> None:
    """A photo sent bare is the commonest capture there is."""
    assert route(None, has_attachment=True) is Intent.CAPTURE


def test_an_attachment_beats_a_command() -> None:
    assert route("/note", has_attachment=True) is Intent.CAPTURE


def test_an_attachment_beats_recall_phrasing() -> None:
    assert route("did i save this?", has_attachment=True) is Intent.CAPTURE


# --- nothing to read -----------------------------------------------------------------


@pytest.mark.parametrize("text", [None, "", "   ", "\n\t "])
def test_empty_text_is_chat(text: str | None) -> None:
    assert route(text) is Intent.CHAT


# --- commands ------------------------------------------------------------------------


def test_a_leading_slash_is_a_command() -> None:
    assert route("/recent") is Intent.COMMAND


def test_a_command_is_never_re_read_as_prose() -> None:
    """`/find` contains a recall word; the slash still decides."""
    assert route("/find sweden") is Intent.COMMAND


def test_a_command_beats_a_url() -> None:
    assert route("/note https://x.com/a", url=URL) is Intent.COMMAND


def test_leading_whitespace_does_not_hide_a_command() -> None:
    assert route("   /help") is Intent.COMMAND


# --- links ---------------------------------------------------------------------------


def test_a_url_is_a_capture() -> None:
    assert route("https://x.com/a", url=URL) is Intent.CAPTURE


def test_a_url_beats_the_phrasing_around_it() -> None:
    """Someone pasting a link is saving it, whatever they typed alongside."""
    assert route("what is this?", url=URL) is Intent.CAPTURE


def test_a_url_beats_recall_phrasing() -> None:
    assert route("did i already save https://x.com/a", url=URL) is Intent.CAPTURE


def test_link_text_without_a_parsed_url_is_not_a_capture() -> None:
    """The caller finds the link. No url in, no capture out."""
    assert route("https://x.com/a") is Intent.CHAT


# --- meta ----------------------------------------------------------------------------


def test_who_are_you_and_your_name_is_meta() -> None:
    assert route("Who are you? What is your name") is Intent.META


@pytest.mark.parametrize(
    "text",
    [
        "who are you",
        "whats your name?",
        "what can you do",
        "who made you",
        "are you a bot",
        "are you human",
        "are you an ai?",
    ],
)
def test_meta_phrasings(text: str) -> None:
    assert route(text) is Intent.META


def test_meta_is_checked_before_recall() -> None:
    """`find` is recall phrasing; a question about the bot is still meta."""
    assert route("find out who made you") is Intent.META


# --- recall --------------------------------------------------------------------------


def test_what_did_i_save_this_week_is_recall() -> None:
    assert route("what did I save this week?") is Intent.RECALL


def test_any_cooking_videos_is_recall() -> None:
    assert route("any cooking videos?") is Intent.RECALL


@pytest.mark.parametrize(
    "text",
    [
        "i saved something about rust",
        "did i keep that pdf",
        "have i got anything on sweden",
        "show me the tax stuff",
        "find the sweden article",
        "remember the job portal thing",
        "my saves about python",
        "whats in my vault",
        "my notes on hiring",
        "my links from the conference",
        "anything from last week",
        "what came in this week",
        "what did i keep yesterday",
    ],
)
def test_recall_phrasings(text: str) -> None:
    assert route(text) is Intent.RECALL


@pytest.mark.parametrize(
    "text",
    ["any cooking videos", "any reels about sweden", "any posts on hiring", "any articles"],
)
def test_any_kind_phrasings(text: str) -> None:
    assert route(text) is Intent.RECALL


def test_a_kind_word_before_any_is_not_recall() -> None:
    """The kind has to follow "any" -- otherwise every sentence with "post" in it matches."""
    assert route("videos are fun, got any thoughts") is Intent.CHAT


def test_recall_is_case_insensitive() -> None:
    assert route("SHOW ME MY NOTES") is Intent.RECALL


# --- chat, the default ---------------------------------------------------------------


def test_a_greeting_is_chat() -> None:
    assert route("Hii") is Intent.CHAT


def test_general_knowledge_is_chat() -> None:
    """The load-bearing case: a question mark alone must not mean recall."""
    assert route("what is the capital of France?") is Intent.CHAT


@pytest.mark.parametrize(
    "text",
    ["thanks!", "how does pgvector work?", "why is the sky blue?", "ok"],
)
def test_chat_phrasings(text: str) -> None:
    assert route(text) is Intent.CHAT


def test_a_recall_word_inside_another_word_does_not_match() -> None:
    """Word boundaries, not substrings -- "confined" is not "find"."""
    assert route("the space felt confined") is Intent.CHAT


# --- the enum itself -----------------------------------------------------------------


def test_intent_is_a_str_enum() -> None:
    """Values are compared and logged as text, so they are part of the contract."""
    assert Intent.RECALL == "recall"
    assert {i.value for i in Intent} == {
        "command",
        "capture",
        "meta",
        "status",
        "recall",
        "chat",
    }


# --- "help" ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Help", "help", "HELP", "help!", "help?"])
def test_help_on_its_own_is_meta(text: str) -> None:
    """Someone typing only "help" is asking what this thing does."""
    assert route(text) is Intent.META


def test_help_inside_a_search_is_still_recall() -> None:
    """Why "help" is anchored: as a substring it would swallow real searches."""
    assert route("help me find my notes about docker") is Intent.RECALL


def test_helpful_is_not_help() -> None:
    assert route("helpful tips please") is Intent.CHAT


def test_slash_help_is_a_command_not_meta() -> None:
    assert route("/help") is Intent.COMMAND


# --- detail: how much of a memory the answer may see ----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "What exactly did that article say?",
        "Explain the details from that saved post.",
        "What did the article say about cache invalidation?",
        "give me the full text of that one",
        "quote it for me",
        "tell me more detail about the redis one",
    ],
)
def test_a_question_asking_for_the_words_wants_detail(text: str) -> None:
    assert wants_detail(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "what did I save about redis?",
        "any cooking videos?",
        "what did I save this week?",
        "show me my notes",
        "Hii",
        None,
        "",
    ],
)
def test_an_ordinary_question_does_not_want_detail(text: str | None) -> None:
    """The default is cards. Detail is paid for only when it was asked for."""
    assert wants_detail(text) is False


def test_detail_is_orthogonal_to_the_lane() -> None:
    """It never decides routing -- only how much of an already-RECALL memory to show."""
    question = "what exactly did that article say?"
    assert route(question) is Intent.CHAT or route(question) is Intent.RECALL
    assert wants_detail(question) is True


# --- "did that save?" -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Is it saved?",
        "is that saved",
        "was this stored",
        "has it been added",
        "did it save",
        "did that go through?",
        "did you save it",
        "have you kept that",
        "is it still processing?",
        "what's the status",
        "status",
        "saved?",
        "did it work",
        "any luck?",
        "is my last link saved",
    ],
)
def test_asking_what_became_of_the_last_save_is_status(text: str) -> None:
    """A question with no subject in it is about an outcome, not about the vault."""
    assert route(text) is Intent.STATUS


@pytest.mark.parametrize(
    "text",
    [
        "did i save the perfume link?",
        "did i save anything about docker",
        "show me what I saved yesterday",
        "find my saved articles",
        "what did I save this week?",
    ],
)
def test_a_question_that_names_its_subject_is_still_recall(text: str) -> None:
    """The whole reason STATUS is anchored to the start of the message.

    "did i save X" carries a retrieval phrase *and* something to search for. Letting the
    status patterns claim it would answer "your last save was a reel" to someone asking
    about a perfume.
    """
    assert route(text) is Intent.RECALL


def test_status_beats_recall_when_both_would_match() -> None:
    """"did it save" contains no subject; ranking recall first would spend an embedding
    ranking the user's memories against the word "it"."""
    assert route("did it save?") is Intent.STATUS


def test_a_link_is_still_a_capture_even_when_phrased_as_a_status_check() -> None:
    """Capture outranks every phrasing. Answering would drop the link on the floor."""
    assert (
        route("did you save it?", url="https://example.com/post") is Intent.CAPTURE
    )


def test_a_status_question_in_bengali_is_status() -> None:
    """The phrase list is English-first; a small non-Latin set covers the commonest
    case, and anything missed falls through to retrieval rather than to the chat lane."""
    assert route("সেভ হয়েছে?") is Intent.STATUS
