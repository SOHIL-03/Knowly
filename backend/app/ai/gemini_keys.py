"""Shared Gemini API key pool with sticky sequential failover.

A request starts with the key that worked last (GEMINI_API_KEY_1 at process start). When that key fails it moves to the
next one (1 -> 2 -> ... -> 10, wrapping around) and STAYS on the first key that works: later requests start there, so a
dead key is not retried on every call. Only a request that exhausts every key raises.

Errors are classified from the provider's structured fields (HTTP status, google.rpc QuotaFailure /
RetryInfo details, Retry-After), not from free-text messages:

  quota_exhausted     429 with a per-day quota, a quota limit of 0, or a long retry delay -> next key
  rate_limited        429 with a short (per-minute) limit -> retry the same key after the server's retry delay
                      (bounded), then next key
  server_unavailable  500/502/503/504 (e.g. "model is experiencing high demand") -> retry the same key with backoff
  network             connection errors and timeouts                              (bounded), then next key
  timeout             no answer within GEMINI_TIMEOUT_SECONDS (a hung key) -> next key at once
  auth                invalid / denied key (401, 403, API_KEY_INVALID) -> next key at once
  bad_request, not_found, other
                      -> raised at once; the request or model is wrong, another key cannot help

Key values never appear in logs, exceptions or API responses: log lines carry only the key's
variable name (e.g. GEMINI_API_KEY_3), and app.logs redacts any registered key value.
"""

from __future__ import annotations

import enum
import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.logs import Timer, exc_fields, get_logger, log, register_secret

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
    TIMEOUT = "timeout"
    BAD_REQUEST = "bad_request"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    OTHER = "other"


FAILOVER = {Category.QUOTA_EXHAUSTED, Category.AUTH, Category.TIMEOUT}  # no same-key retry: a key that hangs is skipped at once
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


class GeminiRequestTimeout(Exception):
    """A Gemini call produced no answer within the deadline (classified as a timeout by its name)."""


def run_with_deadline[R](fn: Callable[[], R], seconds: float) -> R:
    """Runs fn and gives up waiting after `seconds`. The Gemini speech endpoint ignores the SDK's own timeout settings, so a
    key that hangs would otherwise block forever; the hung call is abandoned on a daemon thread (it cannot be killed) and the
    caller can move on to the next key."""
    if seconds <= 0:
        return fn()
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # re-raised in the caller's thread
            box["error"] = exc

    t = threading.Thread(target=work, daemon=True, name="gemini-call")
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise GeminiRequestTimeout(f"no answer within {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


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
    elif "Timeout" in name or "timed out" in str(exc).lower() or code == 408:
        category = Category.TIMEOUT
    elif name in ("ConnectError", "RemoteProtocolError", "APIConnectionError"):
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
        self._current = 0  # index of the key that worked last; the next request starts here

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
        from google.genai import types

        timeout_ms = int(max(1.0, float(getattr(self.settings, "gemini_timeout_seconds", 120.0))) * 1000)
        return genai.Client(api_key=key.value, http_options=types.HttpOptions(timeout=timeout_ms))

    def _client(self, key: ApiKey) -> Any:
        client = self._clients.get(key.name)
        if client is None:
            client = self._factory(key)
            self._clients[key.name] = client
            log(logger, logging.INFO, "gemini_client_created", key=key.name, vertex=self.vertex)
        return client

    def client_for(self, key: ApiKey) -> Any:
        """The cached client for one key, for callers that run their own attempt loop (app/ai/text_llm.py)."""
        return self._client(key)

    def call[R](self, what: str, fn: Callable[[Any], R]) -> R:
        """Runs fn(client) starting at the last working key, moving on (and staying) when a key fails. Returns fn's result
        unchanged; raises GeminiError."""
        if not self.keys:
            raise GeminiError("Gemini is not configured: set GEMINI_API_KEY_1 (or GEMINI_API_KEY)", Category.AUTH)
        timer = Timer()
        total = len(self.keys)
        start = self._current % total
        attempts = 0
        seen: list[Category] = []
        for pos in range(total):
            idx = (start + pos) % total
            key = self.keys[idx]
            retries = 0
            while True:
                attempts += 1
                try:
                    result = run_with_deadline(lambda k=key: fn(self._client(k)), float(getattr(self.settings, "gemini_timeout_seconds", 0) or 0))
                except Exception as exc:
                    c = classify(exc)
                    log(logger, logging.WARNING, "gemini_attempt_failed", what=what, attempt=f"{pos + 1}/{total}",
                        key=key.name, category=c.category.value, code=c.code, status=c.status,
                        quotaIds=c.quota_ids[:3], retryAfter=c.retry_after, sameKeyRetry=retries,
                        reason=REASON.get(c.category), nextAction=_next_action(c.category, retries, pos, total),
                        **exc_fields(exc))
                    if c.category in RETRY_SAME_KEY and retries < MAX_TRANSIENT_RETRIES_PER_KEY:
                        if c.category == Category.RATE_LIMITED and c.retry_after and c.retry_after > MAX_RETRY_AFTER_SECONDS:
                            seen.append(Category.QUOTA_EXHAUSTED)
                            break  # too long to wait inside a request: try the next key
                        wait = c.retry_after if c.retry_after is not None else BACKOFF_SECONDS[retries]
                        wait = min(wait, MAX_RETRY_AFTER_SECONDS) * (0.9 + 0.2 * random.random())
                        retries += 1
                        self._sleep(wait)
                        continue
                    if c.category in FAILOVER or c.category in RETRY_SAME_KEY:
                        seen.append(c.category)
                        break  # retries on this key are used up: next key
                    log(logger, logging.WARNING, "gemini_request_failed", what=what, key=key.name,
                        category=c.category.value, attempts=attempts, durationMs=timer.ms())
                    raise GeminiError(_message(c.category, what), c.category, attempts) from None
                if idx != self._current:
                    log(logger, logging.INFO, "gemini_key_switched", what=what, key=key.name,
                        previous=self.keys[self._current % total].name, attempts=attempts, durationMs=timer.ms())
                    self._current = idx  # stay on the key that works
                else:
                    log(logger, logging.DEBUG, "gemini_request_ok", what=what, key=key.name, attempts=attempts,
                        durationMs=timer.ms())
                return result
        category = (Category.QUOTA_EXHAUSTED if Category.QUOTA_EXHAUSTED in seen
                    else Category.RATE_LIMITED if Category.RATE_LIMITED in seen else seen[-1])
        log(logger, logging.ERROR, "gemini_all_keys_exhausted", what=what, keys=total, attempts=attempts,
            category=category.value, reason=REASON.get(category), durationMs=timer.ms(),
            hint="run `python -m app.ai.check_keys` in backend/ to see which key is out of credits, expired or rate limited")
        if category in (Category.QUOTA_EXHAUSTED, Category.RATE_LIMITED):
            raise GeminiError(
                f"All {total} configured Gemini API key(s) are out of quota or rate limited.", category, attempts
            ) from None
        raise GeminiError(_message(category, what), category, attempts) from None


REASON = {
    Category.QUOTA_EXHAUSTED: "quota or credits used up for this key (daily/monthly limit, or billing not enabled)",
    Category.RATE_LIMITED: "rate limited (too many requests per minute)",
    Category.AUTH: "key invalid, expired, revoked or denied",
    Category.SERVER_UNAVAILABLE: "Gemini overloaded or unavailable (not the key's fault)",
    Category.NETWORK: "network error reaching Gemini",
    Category.TIMEOUT: "no response within the time limit (this key or its project is hanging)",
    Category.NOT_FOUND: "model not found for this key",
    Category.BAD_REQUEST: "request rejected as invalid",
    Category.OTHER: "unknown error",
}


def _next_action(category: Category, retries: int, pos: int, total: int) -> str:
    if category in RETRY_SAME_KEY and retries < MAX_TRANSIENT_RETRIES_PER_KEY:
        return "retrying the same key"
    if category in FAILOVER or category in RETRY_SAME_KEY:
        return "switching to the next key" if pos + 1 < total else "no keys left"
    return "giving up (another key cannot fix this)"


def _message(category: Category, what: str) -> str:
    return {
        Category.SERVER_UNAVAILABLE: f"{what}: Gemini is temporarily unavailable.",
        Category.NETWORK: f"{what}: could not reach Gemini.",
        Category.TIMEOUT: f"{what}: Gemini did not answer in time.",
        Category.BAD_REQUEST: f"{what}: Gemini rejected the request as invalid.",
        Category.AUTH: f"{what}: Gemini rejected the API key or permissions.",
        Category.NOT_FOUND: f"{what}: the Gemini model or resource was not found.",
    }.get(category, f"{what}: Gemini request failed ({category.value}).")
