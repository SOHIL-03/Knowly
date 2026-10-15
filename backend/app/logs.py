"""Structured JSON logging for Cloud Logging.

Cloud Logging parses one JSON object per line on stdout. We never log raw email,
tokens or answer text; user ids are hashed.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

# Secrets (e.g. OpenAI and Gemini API keys) are removed from every formatted log line, including stack traces.
_secrets: set[str] = set()
_secrets_lock = threading.Lock()
# Google API keys have a recognisable shape; redact them even if they were never registered.
_GOOGLE_KEY = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
REDACTED = "[REDACTED]"


def register_secret(value: str | None) -> None:
    if value and len(value) >= 8:
        with _secrets_lock:
            _secrets.add(value)


def redact(text: str) -> str:
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for s in secrets:
        if s in text:
            text = text.replace(s, REDACTED)
    return _GOOGLE_KEY.sub(REDACTED, text)


def uid_hash(uid: str | None) -> str:
    if not uid:
        return "-"
    return hashlib.sha256(uid.encode()).hexdigest()[:16]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "correlationId": correlation_id.get(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                # A field must never overwrite severity/message/logger/correlationId.
                payload[f"field_{key}" if key in payload else key] = value
        if record.exc_info:
            # Cloud Error Reporting groups entries that carry this type and a stack trace.
            payload["@type"] = "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"
            payload["stack_trace"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


# Third-party loggers that flood the output at DEBUG; they stay at WARNING unless LOG_LEVEL=TRACE.
_NOISY = ("urllib3", "httpx", "httpcore", "google.auth", "google.api_core", "grpc", "asyncio", "multipart", "watchfiles")


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    wanted = (level or "INFO").upper()
    root.setLevel("DEBUG" if wanted == "TRACE" else wanted)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.DEBUG if wanted == "TRACE" else logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log(logger: logging.Logger, level: int, message: str, /, **fields: Any) -> None:
    if "uid" in fields:
        fields["uidHash"] = uid_hash(fields.pop("uid"))
    logger.log(level, message, extra={"fields": fields})


class Timer:
    def __init__(self) -> None:
        self.start = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)


def exc_fields(exc: BaseException, frames: int = 4) -> dict[str, Any]:
    """Where and why an exception happened, safe to log (type, short message, last stack frames).

    Messages are truncated and pass through redact() with the rest of the line, so keys never leak."""
    return {
        "errorType": f"{type(exc).__module__}.{type(exc).__name__}",
        "errorDetail": str(exc)[:300],
        "errorAt": [f"{f.filename}:{f.lineno} {f.name}" for f in traceback.extract_tb(exc.__traceback__)[-frames:]],
    }


@contextmanager
def step(logger: logging.Logger, name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Logs `<name>_start`, then `<name>_done` with durationMs, or `<name>_failed` with the error.

    The yielded dict collects extra fields to attach to the done/failed line:
        with step(logger, "parse_issue", issueId=iid) as out:
            out["items"] = 3
    The exception is re-raised unchanged; callers keep their own handling."""
    timer = Timer()
    extra: dict[str, Any] = {}
    log(logger, logging.INFO, f"{name}_start", **fields)
    try:
        yield extra
    except Exception as exc:
        log(logger, logging.ERROR, f"{name}_failed", durationMs=timer.ms(), **fields, **extra, **exc_fields(exc))
        raise
    log(logger, logging.INFO, f"{name}_done", durationMs=timer.ms(), **fields, **extra)
