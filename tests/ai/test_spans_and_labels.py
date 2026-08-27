"""Highlight spans and label parsing — the two AI outputs the UI renders literally."""
from __future__ import annotations

from app.ai.parsing import clean_label, parse_string_list
from app.ai.spans import keep_verbatim

CONTENT = (
    "Facebook reel by Deepak On Board\n\n"
    "GLOBAL JOB SERIES — EPISODE 2: SWEDEN\n \n"
    "​Looking to build your career in Sweden? You don't need a middleman or agency.\n"
    "Swedish companies prefer direct applications, and the hiring process is completely "
    "transparent.\n"
    "Platsbanken is the largest official job portal run by the Swedish Public Employment "
    "Service.\n"
    "#SwedenJobs #WorkInSweden"
)


# --- keep_verbatim -------------------------------------------------------------------


def test_an_exact_sentence_survives() -> None:
    span = "Platsbanken is the largest official job portal run by the Swedish Public"
    assert keep_verbatim([span], CONTENT) == [span]


def test_a_paraphrase_is_dropped() -> None:
    """The whole point: the UI marks these inside the text, so they must be in the text."""
    assert keep_verbatim(["Sweden has three good job portals for foreigners"], CONTENT) == []


def test_line_breaks_and_zero_width_padding_do_not_break_a_match() -> None:
    """Captions wrap mid-sentence; a model quoting it on one line is still quoting it."""
    span = "Looking to build your career in Sweden? You don't need a middleman or agency."
    assert keep_verbatim([span], CONTENT) == [span]


def test_case_differences_are_tolerated() -> None:
    assert keep_verbatim(["swedish companies prefer direct applications"], CONTENT)


def test_a_fragment_too_short_to_mark_is_dropped() -> None:
    assert keep_verbatim(["Sweden", "#SwedenJobs"], CONTENT) == []


def test_a_span_inside_another_is_dropped_not_nested() -> None:
    """Two marks over the same sentence render as one; keep the longer intent."""
    long_span = "Swedish companies prefer direct applications, and the hiring process is"
    short_span = "Swedish companies prefer direct applications"
    assert keep_verbatim([short_span, long_span], CONTENT) == [long_span]


def test_result_follows_the_document_not_the_model() -> None:
    """Marks are applied in reading order; returning them shuffled invites off-by-ones."""
    later = "Platsbanken is the largest official job portal"
    earlier = "You don't need a middleman or agency."
    assert keep_verbatim([later, earlier], CONTENT) == [earlier, later]


def test_empty_source_yields_nothing() -> None:
    assert keep_verbatim(["anything at all, of sufficient length"], "") == []


# --- clean_label ---------------------------------------------------------------------


def test_label_strips_quotes_prefix_and_period() -> None:
    assert clean_label('"Title: Sweden job platforms — direct apply."') == (
        "Sweden job platforms — direct apply"
    )


def test_label_takes_only_the_first_line() -> None:
    assert clean_label("Sweden job platforms\n\nThis reel explains…") == (
        "Sweden job platforms"
    )


def test_label_is_capped_for_the_card() -> None:
    out = clean_label("x" * 200)
    assert len(out) <= 60 and out.endswith("…")


def test_an_empty_answer_is_empty_not_a_crash() -> None:
    assert clean_label("   ") == ""


# --- parse_string_list ---------------------------------------------------------------


def test_a_json_array_parses() -> None:
    assert parse_string_list('["first one", "second one"]') == ["first one", "second one"]


def test_code_fences_are_stripped() -> None:
    assert parse_string_list('```json\n["fenced"]\n```') == ["fenced"]


def test_prose_falls_back_to_the_quoted_runs() -> None:
    """Comma-splitting a sentence would cut quotes in half and never match the source."""
    raw = 'Sure! Here they are: "the first key sentence", and "the second key sentence".'
    assert parse_string_list(raw) == ["the first key sentence", "the second key sentence"]


def test_unparseable_output_is_empty_rather_than_garbage() -> None:
    assert parse_string_list("I could not find anything important.") == []
