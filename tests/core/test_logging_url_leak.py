"""The bot token lives in the Telegram API *path*, so a logged URL is a leaked credential.

`services/telegram/client.py` never logs a URL, but that care is worth nothing while
httpx's own logger narrates every request at INFO -- `HTTP Request: POST
https://api.telegram.org/bot<token>/sendMessage`. `log_sink.redact` cannot catch it
either: it matches on key names, and this arrives as one preformatted string.
"""
from __future__ import annotations

import logging

from app.core.logging import _URL_LOGGING_LIBRARIES, configure_logging


def test_url_logging_libraries_are_quiet_at_info() -> None:
    configure_logging("api")
    for name in _URL_LOGGING_LIBRARIES:
        logger = logging.getLogger(name)
        assert not logger.isEnabledFor(logging.INFO), (
            f"{name} would log request URLs at INFO"
        )
        # A failing request must still be visible; this is a mute, not a gag.
        assert logger.isEnabledFor(logging.WARNING)


def test_httpx_is_covered() -> None:
    """Named explicitly: it is the one that leaked, and the regression to guard."""
    assert "httpx" in _URL_LOGGING_LIBRARIES
