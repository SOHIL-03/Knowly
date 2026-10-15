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
from typing import Any

correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

# Secrets (e.g. Gemini API keys) are removed from every formatted log line, including stack traces.
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
            payload.update(extra)
        if record.exc_info:
            # Cloud Error Reporting groups entries that carry this type and a stack trace.
            payload["@type"] = "type.googleapis.com/google.devtools.clouderrorreporting.v1beta1.ReportedErrorEvent"
            payload["stack_trace"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    if "uid" in fields:
        fields["uidHash"] = uid_hash(fields.pop("uid"))
    logger.log(level, message, extra={"fields": fields})


class Timer:
    def __init__(self) -> None:
        self.start = time.perf_counter()

    def ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)
