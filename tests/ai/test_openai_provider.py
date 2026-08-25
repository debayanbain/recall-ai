"""OpenAI provider: parsing defensiveness and embedding width.

Same contract as the Gemini tests — models return prose and fences whatever the prompt
says, and a vector that does not match the column width is a write that fails at insert.
"""
from __future__ import annotations

import pytest

from app.ai.openai import OpenAIProvider
from app.core.config import settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('["ai","startups"]', ["ai", "startups"]),
        ('```json\n["Career Growth","JobSearch"]\n```', ["career growth", "jobsearch"]),
        ('```\n["a","b"]```', ["a", "b"]),
        ("careers, upskilling, free resources", ["careers", "upskilling", "free resources"]),
        ("", []),
    ],
)
def test_tag_parsing_survives_fences_and_prose(raw: str, expected: list[str]) -> None:
    assert OpenAIProvider._parse_tags(raw) == expected


def test_tag_list_is_capped() -> None:
    raw = "[" + ",".join(f'"t{i}"' for i in range(20)) + "]"
    assert len(OpenAIProvider._parse_tags(raw)) == 7


def test_embedding_matches_the_column_width_natively() -> None:
    """text-embedding-3-small is 1536 dims, so nothing should be padded."""
    vec = [0.1] * 1536
    assert OpenAIProvider._fit_dim(vec) == vec
    assert settings.EMBEDDING_DIM == 1536


def test_a_shorter_model_is_padded_not_rejected() -> None:
    out = OpenAIProvider._fit_dim([0.1] * 768)
    assert len(out) == settings.EMBEDDING_DIM
    assert out[1000] == 0.0


def test_a_longer_model_is_truncated() -> None:
    assert len(OpenAIProvider._fit_dim([0.1] * 3072)) == settings.EMBEDDING_DIM


async def test_missing_key_names_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        await OpenAIProvider().generate_summary("hello")


def test_factory_returns_the_openai_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai import factory

    monkeypatch.setattr(settings, "AI_PROVIDER", "openai")
    factory.get_ai_provider.cache_clear()
    assert isinstance(factory.get_ai_provider(), OpenAIProvider)
    factory.get_ai_provider.cache_clear()
