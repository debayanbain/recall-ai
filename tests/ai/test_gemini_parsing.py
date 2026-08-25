"""Pure-function tests for Gemini response parsing (no API calls)."""
from __future__ import annotations

from app.ai.gemini import GeminiProvider


def test_parse_tags_plain_json() -> None:
    assert GeminiProvider._parse_tags('["ai", "Startups"]') == ["ai", "startups"]


def test_parse_tags_fenced_json() -> None:
    raw = '```json\n["python", "fastapi"]\n```'
    assert GeminiProvider._parse_tags(raw) == ["python", "fastapi"]


def test_parse_tags_garbage_returns_empty() -> None:
    assert GeminiProvider._parse_tags("not json at all") == []


def test_fit_dim_pads_short_vectors() -> None:
    out = GeminiProvider._fit_dim([0.1, 0.2])
    assert len(out) == 1536
    assert out[:2] == [0.1, 0.2]
    assert out[2] == 0.0
