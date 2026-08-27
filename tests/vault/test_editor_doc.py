"""EditorJS document sanitizing — the gate between a contenteditable and the database."""
from __future__ import annotations

import pytest

from app.services.editor_doc import EditorDocumentError, sanitize, to_plain, to_rich


def _para(text: str) -> dict[str, object]:
    return {"type": "paragraph", "data": {"text": text}}


def test_unknown_tags_are_dropped_with_their_attributes() -> None:
    """Output is re-serialized from an allowlist, so nothing unknown can reach a renderer."""
    document, content = sanitize([_para('hello <img src=x onerror="alert(1)"> world')])
    assert content == "hello  world"
    assert document["blocks"][0]["data"]["text"] == "hello  world"


def test_allowed_inline_formatting_is_kept() -> None:
    """The point of the allowlist: bold a user applied has to survive the round trip."""
    document, content = sanitize([_para("a <b>bold</b> and <i>italic</i> line")])
    assert document["blocks"][0]["data"]["text"] == "a <b>bold</b> and <i>italic</i> line"
    assert content == "a bold and italic line", "content stays flat for search/highlights"


def test_tag_spellings_are_normalized() -> None:
    """execCommand emits <strong>/<em> in some browsers and <b>/<i> in others."""
    document, _ = sanitize([_para("<strong>x</strong><em>y</em>")])
    assert document["blocks"][0]["data"]["text"] == "<b>x</b><i>y</i>"


def test_attributes_are_stripped_from_allowed_tags() -> None:
    document, _ = sanitize([_para('<b style="x" onclick="alert(1)">hi</b>')])
    assert document["blocks"][0]["data"]["text"] == "<b>hi</b>"


def test_only_web_links_survive() -> None:
    document, _ = sanitize(
        [_para('<a href="https://x.test/a">ok</a> <a href="javascript:alert(1)">no</a>')]
    )
    assert document["blocks"][0]["data"]["text"] == '<a href="https://x.test/a">ok</a> no'


def test_scheme_check_survives_padding() -> None:
    """Whitespace and control characters inside the scheme are a known bypass."""
    document, _ = sanitize([_para('<a href="java\tscript:alert(1)">x</a>')])
    assert "<a" not in document["blocks"][0]["data"]["text"]


def test_unbalanced_markup_is_closed() -> None:
    """A truncated paste must not leak an open tag into whatever renders the block."""
    document, _ = sanitize([_para("<b><i>text</b>")])
    assert document["blocks"][0]["data"]["text"] == "<b><i>text</i></b>"


def test_entities_are_text_and_stay_escaped_in_the_stored_markup() -> None:
    """What the user typed is `<script>`; what is stored must not be a live tag."""
    document, content = sanitize([_para("&lt;script&gt;alert(1)&lt;/script&gt;")])
    assert content == "<script>alert(1)</script>", "the plain projection is what they typed"
    assert document["blocks"][0]["data"]["text"] == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_a_real_script_tag_keeps_only_its_text() -> None:
    document, content = sanitize([_para("<script>alert(1)</script>after")])
    assert "<script" not in document["blocks"][0]["data"]["text"]
    assert content == "alert(1)after"


def test_edit_round_trips_without_drifting() -> None:
    """Text the user typed must survive save -> escape -> save unchanged."""
    first = sanitize([_para("&lt;div&gt; &amp; friends")])[1]
    # What the client sends back after escaping `first` for the editor.
    escaped = first.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    assert sanitize([_para(escaped)])[1] == first


def test_br_and_block_closes_become_newlines() -> None:
    _, content = sanitize([_para("one<br>two</p>three")])
    assert content == "one\ntwo\nthree"


def test_unknown_block_types_are_dropped() -> None:
    document, content = sanitize(
        [{"type": "image", "data": {"url": "https://x/y.png"}}, _para("kept")]
    )
    assert content == "kept"
    assert [b["type"] for b in document["blocks"]] == ["paragraph"]


def test_list_items_keep_their_formatting() -> None:
    document, content = sanitize(
        [{"type": "list", "data": {"style": "unordered", "items": ["<b>one</b>", "two"]}}]
    )
    assert document["blocks"][0]["data"]["items"] == ["<b>one</b>", "two"]
    assert content == "- one\n- two"


def test_headers_and_lists_flatten_in_document_order() -> None:
    document, content = sanitize(
        [
            {"type": "header", "data": {"text": "Title", "level": 3}},
            {"type": "list", "data": {"style": "unordered", "items": ["one", "two"]}},
        ]
    )
    assert content == "Title\n\n- one\n- two"
    assert document["blocks"][0]["data"]["level"] == 3


def test_nested_list_shape_is_accepted() -> None:
    """`@editorjs/list` v2 nests objects; guessing the version would drop every bullet."""
    _, content = sanitize(
        [
            {
                "type": "list",
                "data": {
                    "style": "ordered",
                    "items": [{"content": "outer", "items": [{"content": "inner"}]}],
                },
            }
        ]
    )
    assert content == "1. outer\n  1. inner"


def test_code_keeps_its_markup() -> None:
    """A code block is a textarea's value; stripping it would delete the user's example."""
    _, content = sanitize([{"type": "code", "data": {"code": "<div>hi</div>"}}])
    assert content == "<div>hi</div>"


def test_empty_document_is_refused() -> None:
    """Pressing Save must never be a way to silently wipe a memory's body."""
    with pytest.raises(EditorDocumentError):
        sanitize([_para("   "), {"type": "paragraph", "data": {}}])


def test_non_list_payload_is_refused() -> None:
    with pytest.raises(EditorDocumentError):
        sanitize({"blocks": []})


def test_too_many_blocks_is_refused() -> None:
    with pytest.raises(EditorDocumentError):
        sanitize([_para("x") for _ in range(501)])


def test_nbsp_is_a_space() -> None:
    assert to_rich("a\xa0b") == "a b"


def test_plain_projection_drops_our_own_tags() -> None:
    assert to_plain("<b>a</b><br><i>b</i>") == "a\nb"
