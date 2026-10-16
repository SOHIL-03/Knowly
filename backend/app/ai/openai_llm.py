"""OpenAI text generation (gpt-4o-mini by default) behind the same `LLM` protocol the pipeline uses.

Every text call in the app goes through `OpenAILLM`: one lazily created, reused client, one model
(`OPENAI_MODEL`), one key (`OPENAI_API_KEY`). There is deliberately no fallback to another provider.

Structured output uses JSON mode (`response_format={"type": "json_object"}`) with the Pydantic model's
JSON Schema appended to the system prompt, then validates the reply with the same Pydantic model.
Strict `json_schema` mode is not used because it rejects the constraints these models rely on
(min/max lengths, defaults). An invalid reply is retried once with the validation error, as before.

Transient failures (429 rate limit, 5xx, timeouts, connection errors) are retried with bounded
exponential backoff; quota exhaustion, auth, bad-request and not-found errors are raised at once.
The key never appears in logs, exceptions or API responses.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from pydantic import ValidationError

from app.ai import trace
from app.ai.client import (
    INJECTION_GUARD,
    AiOutputInvalid,
    AiOutputTruncated,
    AiQuotaExceeded,
    AiSafetyBlocked,
    AiUnavailable,
    T,
    Tier,
)
from app.config import Settings
from app.logs import Timer, exc_fields, get_logger, log, register_secret

logger = get_logger(__name__)

BACKOFF_SECONDS = (1.0, 3.0, 8.0)
MAX_RETRY_AFTER_SECONDS = 20.0
QUOTA_MESSAGE_OPENAI = (
    "The OpenAI account has no remaining quota or credit. Add billing credit or use another API key."
)


def _message(category: str) -> str:
    return {
        "auth": "OpenAI rejected the API key or permissions (check OPENAI_API_KEY).",
        "not_found": "The configured OpenAI model was not found (check OPENAI_MODEL).",
        "bad_request": "OpenAI rejected the request as invalid.",
        "rate_limited": "OpenAI is rate limiting requests. Try again shortly.",
        "server_unavailable": "OpenAI is temporarily unavailable.",
        "network": "Could not reach OpenAI.",
    }.get(category, "OpenAI request failed.")


def classify(exc: BaseException) -> tuple[str, float | None]:
    """(category, server-requested retry delay) from the SDK's exception types and status codes."""
    import openai

    retry_after: float | None = None
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers:
        try:
            retry_after = float(headers.get("retry-after"))
        except (TypeError, ValueError):
            retry_after = None
    code = getattr(exc, "code", None)
    status = getattr(exc, "status_code", None)
    if isinstance(exc, openai.RateLimitError) or status == 429:
        # 429 also signals an exhausted account ("insufficient_quota"); waiting cannot fix that.
        return ("quota_exhausted" if code == "insufficient_quota" else "rate_limited"), retry_after
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError) or status in (401, 403):
        return "auth", None
    if isinstance(exc, openai.NotFoundError) or status == 404 or code == "model_not_found":
        return "not_found", None
    if isinstance(exc, openai.BadRequestError | openai.UnprocessableEntityError) or status in (400, 422):
        return "bad_request", None
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return "network", None
    if isinstance(exc, openai.InternalServerError) or (isinstance(status, int) and status >= 500):
        return "server_unavailable", retry_after
    return "other", None


class OpenAILLM:
    def __init__(
        self,
        settings: Settings,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.model = (settings.openai_model or "").strip()
        self._client = client
        self._lock = threading.Lock()
        self._sleep = sleep
        self.max_retries = max(0, settings.openai_max_retries)
        key = (settings.openai_api_key or "").strip()
        if key:
            register_secret(key)

    # --- client ------------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool((self.settings.openai_api_key or "").strip()) or self._client is not None

    def _get_client(self) -> Any:
        """One client for the process lifetime; created on first use so importing never needs a key."""
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                key = (self.settings.openai_api_key or "").strip()
                if not key:
                    raise AiUnavailable("OpenAI is not configured: set OPENAI_API_KEY", "auth")
                if not self.model:
                    raise AiUnavailable("OpenAI model is not configured: set OPENAI_MODEL", "not_found")
                from openai import OpenAI

                # Retries are done here (bounded, logged), so the SDK's own are switched off.
                self._client = OpenAI(api_key=key, timeout=self.settings.openai_timeout_seconds, max_retries=0)
                log(logger, logging.INFO, "openai_client_created", model=self.model)
        return self._client

    # --- request with bounded retries -----------------------------------------------------------

    def _call(self, what: str, fn: Callable[[Any], Any]) -> Any:
        client = self._get_client()
        if not self.model:
            raise AiUnavailable("OpenAI model is not configured: set OPENAI_MODEL", "not_found")
        timer = Timer()
        attempt = 0
        while True:
            attempt += 1
            try:
                return fn(client)
            except Exception as exc:
                category, retry_after = classify(exc)
                retryable = category in ("rate_limited", "server_unavailable", "network")
                log(logger, logging.WARNING, "openai_attempt_failed", what=what, model=self.model, attempt=attempt,
                    category=category, retryAfter=retry_after, willRetry=retryable and attempt <= self.max_retries,
                    **exc_fields(exc))
                if retryable and attempt <= self.max_retries:
                    wait = retry_after if retry_after is not None else BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                    self._sleep(min(wait, MAX_RETRY_AFTER_SECONDS) * (0.9 + 0.2 * random.random()))
                    continue
                log(logger, logging.ERROR, "openai_request_failed", what=what, model=self.model, category=category,
                    attempts=attempt, durationMs=timer.ms())
                if category == "quota_exhausted":
                    raise AiQuotaExceeded(QUOTA_MESSAGE_OPENAI, category) from None
                if category == "rate_limited":
                    raise AiQuotaExceeded(_message(category), category) from None
                raise AiUnavailable(_message(category), category) from None

    def _log_usage(self, event: str, res: Any, schema: str, attempt: int, timer: Timer) -> None:
        usage = getattr(res, "usage", None)
        log(logger, logging.INFO, event, model=getattr(res, "model", None) or self.model, schema=schema, attempt=attempt,
            latencyMs=timer.ms(), inputTokens=getattr(usage, "prompt_tokens", None),
            outputTokens=getattr(usage, "completion_tokens", None), totalTokens=getattr(usage, "total_tokens", None))

    # --- structured ------------------------------------------------------------------------------

    def generate_json(self, tier: Tier, system: str, prompt: str, schema: type[T], *, temperature: float = 0.2,
                      max_output_tokens: int | None = None, on_result: Callable[[dict], None] | None = None) -> T:
        """`max_output_tokens` caps the completion; when the model stops on that cap the partial JSON is not
        retried but raised as AiOutputTruncated. `on_result` receives {finish, usage, attempt} per response."""
        # `tier` is kept for the LLM protocol; every tier uses the one configured model.
        schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        system_msg = (
            f"{system}\n\n{INJECTION_GUARD}\n\n"
            f"Respond with a single JSON object (no markdown, no commentary) that conforms to this JSON Schema:\n{schema_json}"
        )
        content = prompt
        last_error: str | None = None
        for attempt in range(2):
            if last_error:
                content = (
                    f"{prompt}\n\nYour previous answer failed validation with this error:\n{last_error}\n"
                    "Return corrected JSON only."
                )
            timer = Timer()
            log(logger, logging.INFO, "openai_request", model=self.model, tier=tier, schema=schema.__name__, attempt=attempt,
                promptChars=len(content), systemChars=len(system), temperature=temperature, retryAfterInvalid=bool(last_error))
            base = {"model": self.model, "attempt": attempt + 1, "promptChars": len(content),
                    "promptPreview": content[:trace.PROMPT_PREVIEW_CHARS]}
            try:
                res = self._call(
                    f"{self.model}:{schema.__name__}",
                    lambda client, c=content: client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "system", "content": system_msg}, {"role": "user", "content": c}],
                        temperature=temperature,
                        response_format={"type": "json_object"},
                        **({"max_completion_tokens": max_output_tokens} if max_output_tokens else {}),
                    ),
                )
            except AiUnavailable as exc:
                trace.record(schema.__name__, **base, status="request_failed", error=str(exc), category=exc.category,
                             durationMs=timer.ms())
                raise
            self._log_usage("openai_call", res, schema.__name__, attempt, timer)
            choice = (getattr(res, "choices", None) or [None])[0]
            message = getattr(choice, "message", None)
            finish = getattr(choice, "finish_reason", None)
            usage = getattr(res, "usage", None)
            base |= {
                "model": getattr(res, "model", None) or self.model,
                "finishReason": finish,
                "durationMs": timer.ms(),
                "usage": None if usage is None else {
                    "input": getattr(usage, "prompt_tokens", None),
                    "output": getattr(usage, "completion_tokens", None),
                    "total": getattr(usage, "total_tokens", None),
                },
            }
            if on_result:
                on_result({"finish": finish, "usage": base["usage"], "attempt": attempt + 1})
            if finish == "length" and max_output_tokens:
                log(logger, logging.WARNING, "openai_output_truncated", model=self.model, schema=schema.__name__,
                    maxOutputTokens=max_output_tokens, outputTokens=(base["usage"] or {}).get("output"))
                trace.record(schema.__name__, **base, rawResponse=getattr(message, "content", None), status="truncated",
                             error="Output hit the token limit.")
                raise AiOutputTruncated(f"{schema.__name__} stopped at the {max_output_tokens}-token output limit")
            if finish == "content_filter" or getattr(message, "refusal", None):
                log(logger, logging.WARNING, "openai_safety_block", model=self.model, schema=schema.__name__, finish=finish)
                trace.record(schema.__name__, **base, status="blocked", rawResponse=getattr(message, "content", None),
                             error="OpenAI declined this request.")
                raise AiSafetyBlocked("OpenAI declined this request.")
            text = getattr(message, "content", None) or ""
            try:
                if not text.strip():
                    raise ValueError("empty response" + (" (output truncated)" if finish == "length" else ""))
                out = schema.model_validate_json(text)
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)[:1500]
                log(logger, logging.WARNING, "openai_output_invalid", model=self.model, schema=schema.__name__, attempt=attempt,
                    finish=finish, responseChars=len(text),
                    errorCount=exc.error_count() if isinstance(exc, ValidationError) else 1, error=last_error[:300])
                trace.record(schema.__name__, **base, rawResponse=text, status="validation_failed" if text.strip() else "empty",
                             error=last_error)
                continue
            trace.record(schema.__name__, **base, rawResponse=text, status="ok", parsed=out.model_dump(mode="json"))
            return out
        log(logger, logging.ERROR, "openai_output_invalid_giving_up", model=self.model, schema=schema.__name__)
        raise AiOutputInvalid(f"{schema.__name__} failed validation twice: {last_error}")

    # --- streaming -------------------------------------------------------------------------------

    def stream_text(self, tier: Tier, system: str, prompt: str, *, temperature: float = 0.3) -> Iterator[str]:
        def first_chunk(client: Any):
            # Retries happen only until the first chunk arrives, so streamed text is never repeated.
            stream = client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": f"{system}\n\n{INJECTION_GUARD}"}, {"role": "user", "content": prompt}],
                temperature=temperature,
                stream=True,
                stream_options={"include_usage": True},
            )
            it = iter(stream)
            return it, next(it, None)

        timer = Timer()
        log(logger, logging.INFO, "openai_stream_request", model=self.model, tier=tier, promptChars=len(prompt))
        it, chunk = self._call(f"{self.model}:stream", first_chunk)
        usage = None
        try:
            while chunk is not None:
                usage = getattr(chunk, "usage", None) or usage
                for choice in getattr(chunk, "choices", None) or []:
                    if getattr(choice, "finish_reason", None) == "content_filter":
                        raise AiSafetyBlocked("OpenAI declined this request.")
                    delta = getattr(getattr(choice, "delta", None), "content", None)
                    if delta:
                        yield delta
                chunk = next(it, None)
        except AiSafetyBlocked:
            raise
        except Exception as exc:
            log(logger, logging.WARNING, "openai_stream_interrupted", model=self.model, error=type(exc).__name__)
            raise AiUnavailable("OpenAI stream was interrupted.", "server_unavailable") from None
        log(logger, logging.INFO, "openai_stream_done", model=self.model, latencyMs=timer.ms(),
            inputTokens=getattr(usage, "prompt_tokens", None), outputTokens=getattr(usage, "completion_tokens", None),
            totalTokens=getattr(usage, "total_tokens", None))


def build_llm(settings: Settings) -> OpenAILLM:
    return OpenAILLM(settings)
