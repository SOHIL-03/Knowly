"""Shared Gemini API key pool with sequential failover.

Every independent Gemini request starts with GEMINI_API_KEY_1 and moves to the next configured key
(1 -> 2 -> ... -> 10) only when the current key's quota is exhausted, or when it stays rate limited
after a bounded number of retries. Nothing remembers the last key that worked: the next request
starts at key 1 again.

Errors are classified from the provider's structured fields (HTTP status, google.rpc QuotaFailure /
RetryInfo details, Retry-After), not from free-text messages:

  quota_exhausted     429 with a per-day quota, a quota limit of 0, or a long retry delay
                      -> try the next key
  rate_limited        429 with a short (per-minute) limit
                      -> retry the same key after the server's retry delay (bounded), then next key
  server_unavailable  500/502/503/504 (e.g. "model is experiencing high demand")
                      -> retry the same key with backoff (bounded); never switch keys, since the
                         overload is on the model and other keys would hit it too
  network             connection errors and timeouts -> same as server_unavailable
  bad_request, auth, not_found, other
                      -> raised at once; never switch keys

Key values never appear in logs, exceptions or API responses: log lines carry only the key's
variable name (e.g. GEMINI_API_KEY_3), and app.logs redacts any registered key value.
"""

from __future__ import annotations

import enum
import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.logs import Timer, get_logger, log, register_secret

logger = get_logger(__name__)

MAX_KEYS = 10
MAX_TRANSIENT_RETRIES_PER_KEY = 2  # explicit bound on retries of one key within one request
BACKOFF_SECONDS = (2.0, 5.0)
MAX_RETRY_AFTER_SECONDS = 30.0  # longer server-requested waits are treated as exhausted quota
LONG_RETRY_DELAY_SECONDS = 120.0


class Category(enum.StrEnum):
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    SERVER_UNAVAILABLE = "server_unavailable"
    NETWORK = "network"
    BAD_REQUEST = "bad_request"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    OTHER = "other"


FAILOVER = {Category.QUOTA_EXHAUSTED}
RETRY_SAME_KEY = {Category.RATE_LIMITED, Category.SERVER_UNAVAILABLE, Category.NETWORK}


@dataclass(frozen=True)
class ApiKey:
    name: str
    value: str = field(repr=False)  # never shown in repr / logs

    def __str__(self) -> str:
        return self.name


@dataclass
class Classified:
    category: Category
    code: int | None
    status: str | None
    retry_after: float | None
    quota_ids: list[str]


# --- errors (sanitized: no key values, no provider message text) ----------------------------

class GeminiError(RuntimeError):
    """Base for sanitized Gemini failures. `category` is safe to log and show."""

    def __init__(self, message: str, category: Category | str, attempts: int = 0) -> None:
        super().__init__(message)
        self.category = Category(category)
        self.attempts = attempts


# --- configuration ---------------------------------------------------------------------------

def load_keys(settings: Settings) -> list[ApiKey]:
    """GEMINI_API_KEY_1..10 in numerical order; empty or missing slots are skipped. When no
    numbered key is set, the single legacy GEMINI_API_KEY is used."""
    keys: list[ApiKey] = []
    for i in range(1, MAX_KEYS + 1):
        value = (getattr(settings, f"gemini_api_key_{i}", "") or "").strip()
        if value:
            keys.append(ApiKey(f"GEMINI_API_KEY_{i}", value))
    if not keys:
        legacy = (settings.gemini_api_key or "").strip()
        if legacy:
            keys.append(ApiKey("GEMINI_API_KEY", legacy))
    for k in keys:
        register_secret(k.value)
    return keys


# --- classification ----------------------------------------------------------------------------

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def _code(exc: BaseException) -> int | None:
    for attr in ("code", "status_code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _error_body(exc: BaseException) -> dict[str, Any]:
    for attr in ("details", "body"):
        body = getattr(exc, attr, None)
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                body = None
        if isinstance(body, list) and len(body) == 1:
            body = body[0]
        if isinstance(body, dict):
            return body.get("error", body) if isinstance(body.get("error"), dict) else body
    return {}


def _retry_after_header(exc: BaseException) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify(exc: BaseException) -> Classified:
    code = _code(exc)
    body = _error_body(exc)
    status = body.get("status") if isinstance(body.get("status"), str) else getattr(exc, "status", None)
    status = status if isinstance(status, str) else None
    details = body.get("details") if isinstance(body.get("details"), list) else []

    quota_ids: list[str] = []
    zero_limit = False
    retry_after = _retry_after_header(exc)
    reasons: list[str] = []
    for d in details:
        if not isinstance(d, dict):
            continue
        dtype = str(d.get("@type", ""))
        if dtype.endswith("QuotaFailure"):
            for v in d.get("violations") or []:
                if isinstance(v, dict):
                    if v.get("quotaId"):
                        quota_ids.append(str(v["quotaId"]))
                    if str(v.get("quotaValue", "")).strip() == "0":
                        zero_limit = True
        elif dtype.endswith("RetryInfo"):
            m = _DURATION.match(str(d.get("retryDelay", "")))
            if m:
                retry_after = float(m.group(1))
        elif dtype.endswith("ErrorInfo") and d.get("reason"):
            reasons.append(str(d["reason"]))

    name = type(exc).__name__
    if code == 429 or status == "RESOURCE_EXHAUSTED":
        per_day = any("PerDay" in q for q in quota_ids)
        long_wait = retry_after is not None and retry_after > LONG_RETRY_DELAY_SECONDS
        category = Category.QUOTA_EXHAUSTED if (per_day or zero_limit or long_wait) else Category.RATE_LIMITED
        code = code or 429
    elif code in (500, 502, 503, 504) or status in ("UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"):
        category = Category.SERVER_UNAVAILABLE
    elif code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED") or "API_KEY_INVALID" in reasons:
        category = Category.AUTH
    elif code == 404 or status == "NOT_FOUND":
        category = Category.NOT_FOUND
    elif code == 400 or status in ("INVALID_ARGUMENT", "FAILED_PRECONDITION"):
        category = Category.BAD_REQUEST
    elif name in ("ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
                  "RemoteProtocolError", "TimeoutException", "APIConnectionError", "APITimeoutError"):
        category = Category.NETWORK
    else:
        category = Category.OTHER
    return Classified(category, code, status, retry_after, quota_ids)


# --- pool ----------------------------------------------------------------------------------------

class GeminiKeyPool:
    """Runs one Gemini request with sequential key failover. Thread-safe for concurrent requests:
    each call keeps its own position in the key list, and clients are created per key once."""

    def __init__(
        self,
        settings: Settings,
        *,
        keys: list[ApiKey] | None = None,
        client_factory: Callable[[ApiKey], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.vertex = settings.gemini_use_vertex and keys is None
        self.keys = keys if keys is not None else ([] if self.vertex else load_keys(settings))
        if self.vertex:
            self.keys = [ApiKey("VERTEX_AI", "")]
        self._factory = client_factory or self._default_factory
        self._clients: dict[str, Any] = {}
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.keys)

    @property
    def key_names(self) -> list[str]:
        return [k.name for k in self.keys]

    def _default_factory(self, key: ApiKey) -> Any:
        from google import genai

        if self.vertex:
            s = self.settings
            return genai.Client(vertexai=True, project=s.gcp_project, location=s.gemini_vertex_location)
        return genai.Client(api_key=key.value)

    def _client(self, key: ApiKey) -> Any:
        client = self._clients.get(key.name)
        if client is None:
            client = self._factory(key)
            self._clients[key.name] = client
        return client

    def call[R](self, what: str, fn: Callable[[Any], R]) -> R:
        """Runs fn(client) starting at key 1. Returns fn's result unchanged; raises GeminiError."""
        if not self.keys:
            raise GeminiError("Gemini is not configured: set GEMINI_API_KEY_1 (or GEMINI_API_KEY)", Category.AUTH)
        timer = Timer()
        total = len(self.keys)
        attempts = 0
        seen: list[Category] = []
        for idx, key in enumerate(self.keys, start=1):
            retries = 0
            while True:
                attempts += 1
                try:
                    result = fn(self._client(key))
                except Exception as exc:
                    c = classify(exc)
                    log(logger, logging.WARNING, "gemini_attempt_failed", what=what, attempt=f"{idx}/{total}",
                        key=key.name, category=c.category.value, code=c.code, status=c.status,
                        quotaIds=c.quota_ids[:3], retryAfter=c.retry_after, sameKeyRetry=retries)
                    if c.category in RETRY_SAME_KEY and retries < MAX_TRANSIENT_RETRIES_PER_KEY:
                        if c.category == Category.RATE_LIMITED and c.retry_after and c.retry_after > MAX_RETRY_AFTER_SECONDS:
                            seen.append(Category.QUOTA_EXHAUSTED)
                            break  # too long to wait inside a request: try the next key
                        wait = c.retry_after if c.retry_after is not None else BACKOFF_SECONDS[retries]
                        wait = min(wait, MAX_RETRY_AFTER_SECONDS) * (0.9 + 0.2 * random.random())
                        retries += 1
                        self._sleep(wait)
                        continue
                    if c.category in FAILOVER or c.category == Category.RATE_LIMITED:
                        seen.append(c.category)
                        break  # next key
                    log(logger, logging.WARNING, "gemini_request_failed", what=what, key=key.name,
                        category=c.category.value, attempts=attempts, durationMs=timer.ms())
                    raise GeminiError(_message(c.category, what), c.category, attempts) from None
                if idx > 1:
                    log(logger, logging.INFO, "gemini_failover_succeeded", what=what, attempt=f"{idx}/{total}",
                        key=key.name, attempts=attempts, durationMs=timer.ms())
                else:
                    log(logger, logging.DEBUG, "gemini_request_ok", what=what, key=key.name, attempts=attempts,
                        durationMs=timer.ms())
                return result
        category = Category.QUOTA_EXHAUSTED if Category.QUOTA_EXHAUSTED in seen else Category.RATE_LIMITED
        log(logger, logging.ERROR, "gemini_all_keys_exhausted", what=what, keys=total, attempts=attempts,
            category=category.value, durationMs=timer.ms())
        raise GeminiError(
            f"All {total} configured Gemini API key(s) are out of quota or rate limited.", category, attempts
        ) from None


def _message(category: Category, what: str) -> str:
    return {
        Category.SERVER_UNAVAILABLE: f"{what}: Gemini is temporarily unavailable.",
        Category.NETWORK: f"{what}: could not reach Gemini.",
        Category.BAD_REQUEST: f"{what}: Gemini rejected the request as invalid.",
        Category.AUTH: f"{what}: Gemini rejected the API key or permissions.",
        Category.NOT_FOUND: f"{what}: the Gemini model or resource was not found.",
    }.get(category, f"{what}: Gemini request failed ({category.value}).")
