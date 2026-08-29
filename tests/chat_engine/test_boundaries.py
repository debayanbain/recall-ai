"""The router serves every surface, so it may not know about any of them.

A single import of one surface's helpers is all it takes for the next surface to have to
either drag that dependency in or fork the file. Cheaper to fail here than to notice
after the second caller exists.
"""
from __future__ import annotations

from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[2] / "app" / "services" / "chat_engine"


def test_the_package_exists() -> None:
    """Guards the test itself: an empty glob would pass the check below vacuously."""
    assert _PACKAGE.is_dir()
    assert list(_PACKAGE.glob("*.py"))


def test_no_module_mentions_the_messaging_surface() -> None:
    offenders = [
        path.name
        for path in sorted(_PACKAGE.rglob("*.py"))
        if "telegram" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []
