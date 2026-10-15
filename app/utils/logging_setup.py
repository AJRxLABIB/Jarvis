"""Structured logging.

* JSON-lines rotating file log (read later by the GUI log viewer).
* Optional human-readable console log (enabled with DEBUG=true).
* Every record passes a redaction filter so secrets never reach disk.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

from app.utils.redaction import redact, redact_value

ROOT_LOGGER_NAME = "jarvis"
LOG_FILE_NAME = "jarvis.log"
_HANDLER_TAG = "_jarvis_handler"
_RESERVED_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "taskName"}

_state_lock = threading.Lock()
_log_file: Path | None = None


def get_logger(name: str) -> logging.Logger:
    """Logger inside the ``jarvis`` namespace, e.g. ``get_logger("database")``."""
    if name == ROOT_LOGGER_NAME or name.startswith(ROOT_LOGGER_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED_ATTRS and not k.startswith("_")}


class RedactingFilter(logging.Filter):
    """Redacts the rendered message and every structured field."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed %-args must never break logging
            message = f"{record.msg!r} (args={record.args!r})"
        record.msg = redact(message)
        record.args = None
        if not hasattr(record, "trace_id"):  # every line written during a request carries that request's ID
            try:
                from app.core.runs import current

                run = current()
                if run is not None:
                    record.trace_id = run.id
            except Exception:
                pass
        for key, value in _extra_fields(record).items():
            setattr(record, key, redact_value(value, key=key))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "thread": record.threadName,
        }
        fields = _extra_fields(record)
        if fields:
            payload["fields"] = fields
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")

    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 (stdlib name)
        base = super().formatMessage(record)
        fields = {k: v for k, v in _extra_fields(record).items() if k != "event"}
        if fields:
            base += "  " + " ".join(f"{k}={v}" for k, v in fields.items())
        return base

    def formatException(self, ei: Any) -> str:  # noqa: N802
        return redact(super().formatException(ei))


def _teardown_unlocked() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def setup_logging(
    logs_dir: Path,
    level: str = "INFO",
    *,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
    console: bool = False,
) -> Path:
    """Configure logging (idempotent) and return the log file path."""
    global _log_file
    with _state_lock:
        _teardown_unlocked()
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = logs_dir / LOG_FILE_NAME
        redactor = RedactingFilter()

        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8", delay=True
        )
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(redactor)
        setattr(file_handler, _HANDLER_TAG, True)

        root = logging.getLogger()
        root.addHandler(file_handler)

        # pythonw.exe / windowed executables have no stderr.
        if console and sys.stderr is not None:
            console_handler = logging.StreamHandler(sys.stderr)
            console_handler.setFormatter(ConsoleFormatter())
            console_handler.addFilter(redactor)
            setattr(console_handler, _HANDLER_TAG, True)
            root.addHandler(console_handler)

        root.setLevel(logging.WARNING)  # third-party libraries: warnings and above only
        logging.getLogger(ROOT_LOGGER_NAME).setLevel(level.upper())
        logging.captureWarnings(True)
        _log_file = log_file
        return log_file


def teardown_logging() -> None:
    """Flush and close our handlers (releases the log file on Windows)."""
    global _log_file
    with _state_lock:
        _teardown_unlocked()
        _log_file = None


def current_log_file() -> Path | None:
    return _log_file


def log_event(logger: logging.Logger, event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Write a structured event, e.g. ``log_event(log, "tool_call", tool="open_application")``."""
    # ``level`` is this function's own parameter; a field with that name would be
    # swallowed silently, so callers must use a distinct name (e.g. risk_level).
    extra = {(f"field_{k}" if k in _RESERVED_ATTRS else k): v for k, v in fields.items()}
    extra["event"] = event
    logger.log(level, event, extra=extra)


def install_exception_hooks() -> None:
    """Log unhandled exceptions instead of letting a raw traceback be the only trace."""

    def _hook(exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        get_logger("crash").critical(
            "Unhandled exception", exc_info=(exc_type, exc, tb), extra={"event": "unhandled_exception"}
        )
        if sys.stderr is not None:
            where = _log_file or "the log file"
            print(f"An unexpected error occurred ({exc_type.__name__}). Details were written to {where}.", file=sys.stderr)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit or args.exc_value is None:
            return
        get_logger("crash").critical(
            "Unhandled exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            extra={"event": "unhandled_thread_exception"},
        )

    sys.excepthook = _hook
    threading.excepthook = _thread_hook
