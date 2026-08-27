"""Structured logging with correlation/request IDs via structlog.

Two destinations, one call site. Every event is rendered to stdout (JSON outside dev),
and in development it is also appended to `LOG_DIR/<source>-<date>.jsonl` by
`app.core.log_sink` -- the centralized folder shared by the API, the Celery worker and
beat, kept for `LOG_RETENTION_DAYS` (15). Outside `ENV=dev` the file half is off and
stdout is the only destination.
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.core.config import settings
from app.core.log_sink import sink_processor, start_sink, stop_sink

#: Which process this is, stamped on every row so one query can separate API noise from
#: worker failures. Set by `configure_logging`.
_source = "api"

#: Third-party loggers that put a whole request URL on an INFO line.
#:
#: `httpx` emits `HTTP Request: POST <url> "HTTP/1.1 200 OK"` at INFO, and the Telegram
#: Bot API carries the bot token *in the path* -- so one INFO line writes a live
#: credential to stdout and into the terminal scrollback, defeating the care taken in
#: `services/telegram/client.py` never to log a URL itself. `log_sink.redact` cannot
#: catch it: it matches on key names, and this arrives as one preformatted string.
#: botocore is here for the same reason -- a presigned B2 URL *is* the bearer credential
#: for that object, and its debug logging prints signed URLs in full.
#:
#: WARNING rather than removed: a failing request still says so, it just stops narrating
#: the successful ones. Raise an individual one temporarily if you need the trace, and
#: do not commit it.
_URL_LOGGING_LIBRARIES = (
    "httpx",
    "httpcore",
    "botocore",
    "boto3",
    "s3transfer",
    "urllib3",
)


def configure_logging(source: str = "api") -> None:
    """Configure structlog + stdlib logging. JSON in prod, pretty in dev.

    `source` is one of api / worker / beat and ends up in `app_logs.source`.
    """
    global _source
    _source = source

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Renders exc_info into an `exception` string *before* the sink runs, so the
        # written line carries the traceback rather than an unserializable exc_info tuple.
        structlog.processors.format_exc_info,
        # Last processor before the renderer: it sees the fully-built event dict and
        # returns it untouched.
        sink_processor,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if settings.ENV != "dev"
        else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.DEBUG else logging.INFO
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO,
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # After basicConfig, which is what put the root logger at INFO in the first place.
    for name in _URL_LOGGING_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


def start_log_sink() -> None:
    """Open this process's log file. Call once, after `configure_logging`. No-op in prod."""
    start_sink(_source)


def stop_log_sink() -> None:
    """Close the log file. Call on shutdown."""
    stop_sink()


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Lazy logger whose name travels in the event dict.

    The name is passed as an *initial value*, never via `.bind()`. `get_logger` returns a
    lazy proxy that resolves the processor chain on first use, but `.bind()` resolves it
    immediately -- and every module here does `log = get_logger("http")` at import time,
    which is before the lifespan calls `configure_logging()`. Binding eagerly therefore
    froze structlog's *default* chain into those loggers, with no file sink in it, and the
    logs/ folder was never created. Initial values stay deferred, so the sink is present.

    (`PrintLoggerFactory` ignores the positional name, which is why the name has to travel
    as a key at all. The key is `logger_name` because `logger` is a reserved argument of
    `structlog.wrap_logger`; both `ConsoleRenderer` and `build_record` read either.)
    """
    logger: structlog.stdlib.BoundLogger = (
        structlog.get_logger(logger_name=name) if name else structlog.get_logger()
    )
    return logger
