"""File sink for structlog -- the local half of the centralized log trail.

Development only. Every structlog event from the API, the Celery worker and beat is
appended as one JSON line under `LOG_DIR` (default `logs/`), one file per source per day:

    logs/api-2026-08-25.jsonl
    logs/worker-2026-08-25.jsonl
    logs/beat-2026-08-25.jsonl

That is the whole point of the folder -- three processes, one place to grep, with
`request_id` correlating a request across all of them:

    jq -c 'select(.request_id=="abc123")' logs/*-2026-08-25.jsonl

Outside `ENV=dev` nothing is ever written: the sink is hard-gated on the environment, not
merely defaulted off. A production container's filesystem is ephemeral and unmonitored, so
files there would be a PII spill that nobody reads. stdout stays the transport in
staging/prod; the platform's own drain collects it.

Retention is `LOG_RETENTION_DAYS` (15). Files are pruned by the sink itself when it rolls
over to a new day, so retention holds with no cron, no worker and no beat running.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import settings

#: structlog level names to severity, for the sink's own threshold.
_LEVELS: dict[str, int] = {
    "debug": 10,
    "info": 20,
    "warn": 30,
    "warning": 30,
    "error": 40,
    "critical": 50,
    "exception": 40,
}

#: Substrings that make a key a credential. Matched case-insensitively against the whole
#: key, so `access_token`, `X-Api-Key` and `client_secret` are all caught. Deliberately
#: specific -- a bare "auth" would redact `auth_provider="google"`, which is not a secret
#: and is worth having in the trail.
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "encryption_key",
    "signature",
    "session_id",
    "client_id",
)

_REDACTED = "[redacted]"

#: Keys promoted to top-level record fields; they must not be duplicated into `context`.
_PROMOTED: frozenset[str] = frozenset(
    {
        "event",
        "level",
        "logger",
        "logger_name",
        "timestamp",
        "request_id",
        "user_id",
        "path",
        "method",
        "status",
        "status_code",
        "duration_ms",
        "exception",
        "exc_info",
        "stack",
    }
)

_MAX_STRING = 2_000
_MAX_EXCEPTION = 8_000
_MAX_CONTEXT_KEYS = 60

#: Only files this sink itself could have written are ever deleted by the prune. Anything
#: else in the folder -- a note, a copied-out crash dump -- is left alone.
_LOG_FILE = re.compile(r"^(?P<source>[a-z0-9_]+)-(?P<date>\d{4}-\d{2}-\d{2})\.jsonl$")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _clip(value: str, limit: int = _MAX_STRING) -> str:
    return value if len(value) <= limit else value[:limit] + "...[truncated]"


def _scrub(value: Any, depth: int = 0) -> Any:
    """Make a value JSON-safe, size-bounded and free of nested credentials."""
    if depth > 4:
        return _clip(str(value))
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, dict):
        return {
            str(k): (_REDACTED if _is_sensitive(str(k)) else _scrub(v, depth + 1))
            for k, v in list(value.items())[:_MAX_CONTEXT_KEYS]
        }
    if isinstance(value, list | tuple | set):
        return [_scrub(v, depth + 1) for v in list(value)[:_MAX_CONTEXT_KEYS]]
    return _clip(str(value))


def redact(event_dict: dict[str, Any]) -> dict[str, Any]:
    """Everything not promoted to a field, with credential-looking keys removed.

    Redaction happens on the way *in*, not on the way out: a log file is also a thing
    people copy, paste into an issue and archive, so a token that reaches the disk has
    already leaked.
    """
    context: dict[str, Any] = {}
    for key, value in event_dict.items():
        if key in _PROMOTED or key.startswith("_"):
            continue
        if len(context) >= _MAX_CONTEXT_KEYS:
            break
        context[key] = _REDACTED if _is_sensitive(key) else _scrub(value)
    return context


def _coerce_timestamp(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _coerce_user_id(raw: Any) -> str | None:
    if isinstance(raw, uuid.UUID):
        return str(raw)
    if isinstance(raw, str):
        try:
            return str(uuid.UUID(raw))
        except ValueError:
            return None
    return None


def _coerce_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _coerce_float(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    return None


def build_record(event_dict: dict[str, Any], source: str) -> dict[str, Any]:
    """Turn a structlog event dict into the JSON record written to the file.

    Fixed keys first so `jq` and grep have a stable shape across every source. Never
    raises: a malformed event must not take down the call site that logged it.
    """
    exception = event_dict.get("exception")
    return {
        "ts": _coerce_timestamp(event_dict.get("timestamp")).isoformat(),
        "level": str(event_dict.get("level") or "info").lower()[:20],
        "source": source[:20],
        "logger": _clip(str(event_dict.get("logger") or event_dict.get("logger_name") or ""), 200)
        or None,
        "event": _clip(str(event_dict.get("event") or ""), 500) or "(empty)",
        "request_id": _clip(str(event_dict["request_id"]), 100)
        if event_dict.get("request_id")
        else None,
        "user_id": _coerce_user_id(event_dict.get("user_id")),
        "path": _clip(str(event_dict["path"]), 500) if event_dict.get("path") else None,
        "method": _clip(str(event_dict["method"]), 10) if event_dict.get("method") else None,
        "status_code": _coerce_int(event_dict.get("status_code") or event_dict.get("status")),
        "duration_ms": _coerce_float(event_dict.get("duration_ms")),
        "exception": _clip(str(exception), _MAX_EXCEPTION) if exception else None,
        "context": redact(event_dict),
    }


def log_dir() -> Path:
    return Path(settings.LOG_DIR).expanduser().resolve()


def prune_expired(directory: Path, retention_days: int, today: date | None = None) -> list[str]:
    """Delete log files older than the retention window. Returns what was removed.

    Keyed on the date in the filename rather than mtime: an old file that gets touched
    (opened in an editor, copied) must still expire on schedule. Only names this sink
    could have produced are considered, so nothing else in the folder is at risk.
    """
    if not directory.is_dir():
        return []
    cutoff = (today or datetime.now(UTC).date()) - timedelta(days=retention_days)
    removed: list[str] = []
    for entry in directory.iterdir():
        match = _LOG_FILE.match(entry.name)
        if not match or not entry.is_file():
            continue
        try:
            stamp = date.fromisoformat(match.group("date"))
        except ValueError:
            continue
        if stamp < cutoff:
            try:
                entry.unlink()
                removed.append(entry.name)
            except OSError:
                continue
    return removed


class FileLogSink:
    """Appends JSON lines to `LOG_DIR/<source>-<date>.jsonl`, pruning on rollover.

    Writes are synchronous: one `os.write` to an fd opened `O_APPEND`, which the kernel
    serialises across processes, so the prefork worker's children share a file without a
    lock between them. That is cheap enough for local development and removes the entire
    class of background-flusher bugs -- there is no queue to overflow and nothing to
    lose on a hard exit.
    """

    def __init__(self, source: str) -> None:
        self.source = source
        self._lock = threading.Lock()
        self._fd: int | None = None
        self._day: date | None = None
        self._threshold = _LEVELS.get(settings.LOG_FILE_LEVEL.lower(), 20)
        self._failed = False

    def enabled_for(self, level: str) -> bool:
        return _LEVELS.get(level.lower(), 20) >= self._threshold

    def write(self, event_dict: dict[str, Any]) -> None:
        """Append one record. Swallows every error -- logging must not break the app."""
        try:
            with self._lock:
                fd = self._open_for(datetime.now(UTC).date())
                if fd is not None:
                    self._write_line(fd, event_dict)
        except Exception as exc:  # noqa: BLE001 - never re-log; that would recurse
            self._complain(f"write failed: {type(exc).__name__}: {exc}")

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                finally:
                    self._fd = None
                    self._day = None

    # -- internals -------------------------------------------------------------------

    def _write_line(self, fd: int, event_dict: dict[str, Any]) -> None:
        """One `os.write` of one JSON line. Caller holds the lock."""
        record = build_record(event_dict, self.source)
        line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
        os.write(fd, line.encode("utf-8"))

    def _open_for(self, today: date) -> int | None:
        """Return the fd for today's file, rolling over (and pruning) at midnight."""
        if self._fd is not None and self._day == today:
            return self._fd
        if self._failed:
            return None

        directory = log_dir()
        try:
            # 0o700: the trail carries request paths and user ids, so it is not
            # world-readable on a shared machine.
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / f"{self.source}-{today.isoformat()}.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            # A read-only or missing directory disables the sink for this process rather
            # than raising on every single log line.
            self._failed = True
            self._complain(f"cannot open log file in {directory}: {exc}")
            return None

        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd, self._day = fd, today

        # Rollover is the natural moment to enforce retention: it happens once a day in a
        # long-running process and once at startup in a short-lived one.
        # `_write_line`, not `write`: the lock is already held by the caller.
        for name in prune_expired(directory, settings.LOG_RETENTION_DAYS, today):
            self._write_line(
                fd,
                {
                    "event": "log_file_expired",
                    "level": "info",
                    "logger": "log_sink",
                    "file": name,
                    "retention_days": settings.LOG_RETENTION_DAYS,
                },
            )
        return self._fd

    @staticmethod
    def _complain(message: str) -> None:
        """Report a sink failure without going through structlog (which feeds the sink)."""
        print(f"[log-sink] {message}", file=sys.stderr, flush=True)


_sink: FileLogSink | None = None


def get_sink() -> FileLogSink | None:
    return _sink


def start_sink(source: str) -> FileLogSink | None:
    """Create the process-wide sink. No-op unless file logging is on. Idempotent."""
    global _sink
    if not settings.file_logging_enabled:
        return None
    if _sink is None or _sink.source != source:
        if _sink is not None:
            _sink.close()
        _sink = FileLogSink(source=source)
    return _sink


def stop_sink() -> None:
    global _sink
    if _sink is not None:
        _sink.close()
        _sink = None


def sink_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: mirror the event to disk, pass it through untouched."""
    sink = _sink
    if sink is not None and sink.enabled_for(str(event_dict.get("level") or "info")):
        sink.write(event_dict)
    return event_dict
