"""OpenAI text generation (gpt-4o-mini by default) behind the same `LLM` protocol the pipeline uses.

`OpenAILLM` is the final fallback of app/ai/text_llm.py (after every Gemini key), and the only text provider when
LLM_STREAMING_ENABLED=false: one lazily created, reused client, one model (`OPENAI_MODEL`), one key (`OPENAI_API_KEY`).

Structured output uses JSON mode (`response_format={"type": "json_object"}`) with the Pydantic model's
JSON Schema appended to the system prompt, then validates the reply with the same Pydantic model.
Strict `json_schema` mode is not used because it rejects the constraints these models rely on
(min/max lengths, defaults). An invalid reply is retried once with the validation error, as before.
`run_json` holds that loop so the Gemini path in text_llm.py validates exactly the same way.

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
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.ai import trace
from app.ai.client import (
    INJECTION_GUARD,
    AiCancelled,
    AiOutputInvalid,
    AiOutputTruncated,
    AiQuotaExceeded,
    AiSafetyBlocked,
    AiStreamInterrupted,
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


def usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {"input": getattr(usage, "prompt_tokens", None), "output": getattr(usage, "completion_tokens", None),
            "total": getattr(usage, "total_tokens", None)}


# --- provider-neutral structured output ----------------------------------------------------------

@dataclass
class Completion:
    """One finished text response. `finish` uses OpenAI's names ("stop", "length", "content_filter")."""

    text: str
    finish: str | None
    usage: dict[str, Any] | None  # {"input", "output", "total"} token counts
    model: str
    refusal: bool = False
    provider: str = "openai"


def json_system_message(system: str, schema: type[T]) -> str:
    schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
    return (
        f"{system}\n\n{INJECTION_GUARD}\n\n"
        f"Respond with a single JSON object (no markdown, no commentary) that conforms to this JSON Schema:\n{schema_json}"
    )


def run_json(complete: Callable[[str], Completion], schema: type[T], system: str, prompt: str, *, tier: Tier,
             temperature: float, max_output_tokens: int | None, on_result: Callable[[dict], None] | None,
             model: str, prefix: str) -> T:
    """Asks `complete(user_content)` for the JSON reply, validates it with `schema` and retries once with the
    validation error. `max_output_tokens` caps the completion; when the model stops on that cap the partial JSON is not
    retried but raised as AiOutputTruncated. `on_result` receives {finish, usage, attempt} per response. Log events are
    named `{prefix}_request`, `{prefix}_call`, ..."""
    content = prompt
    last_error: str | None = None
    for attempt in range(2):
        if last_error:
            content = (
                f"{prompt}\n\nYour previous answer failed validation with this error:\n{last_error}\n"
                "Return corrected JSON only."
            )
        timer = Timer()
        log(logger, logging.INFO, f"{prefix}_request", model=model, tier=tier, schema=schema.__name__, attempt=attempt,
            promptChars=len(content), systemChars=len(system), temperature=temperature, retryAfterInvalid=bool(last_error))
        base = {"model": model, "attempt": attempt + 1, "promptChars": len(content),
                "promptPreview": content[:trace.PROMPT_PREVIEW_CHARS]}
        try:
            res = complete(content)
        except AiUnavailable as exc:
            trace.record(schema.__name__, **base, status="request_failed", error=str(exc), category=exc.category,
                         durationMs=timer.ms())
            raise
        usage = res.usage or {}
        log(logger, logging.INFO, f"{prefix}_call", model=res.model, provider=res.provider, schema=schema.__name__,
            attempt=attempt, latencyMs=timer.ms(), inputTokens=usage.get("input"), outputTokens=usage.get("output"),
            totalTokens=usage.get("total"))
        finish = res.finish
        base |= {"model": res.model, "provider": res.provider, "finishReason": finish, "durationMs": timer.ms(),
                 "usage": res.usage}
        if on_result:
            on_result({"finish": finish, "usage": base["usage"], "attempt": attempt + 1})
        if finish == "length" and max_output_tokens:
            log(logger, logging.WARNING, f"{prefix}_output_truncated", model=res.model, schema=schema.__name__,
                maxOutputTokens=max_output_tokens, outputTokens=usage.get("output"))
            trace.record(schema.__name__, **base, rawResponse=res.text, status="truncated",
                         error="Output hit the token limit.")
            raise AiOutputTruncated(f"{schema.__name__} stopped at the {max_output_tokens}-token output limit")
        if finish == "content_filter" or res.refusal:
            log(logger, logging.WARNING, f"{prefix}_safety_block", model=res.model, schema=schema.__name__, finish=finish)
            trace.record(schema.__name__, **base, status="blocked", rawResponse=res.text,
                         error="The AI provider declined this request.")
            raise AiSafetyBlocked("The AI provider declined this request.")
        text = res.text or ""
        try:
            if not text.strip():
                raise ValueError("empty response" + (" (output truncated)" if finish == "length" else ""))
            out = schema.model_validate_json(text)
        except (ValidationError, ValueError) as exc:
            last_error = str(exc)[:1500]
            log(logger, logging.WARNING, f"{prefix}_output_invalid", model=res.model, schema=schema.__name__,
                attempt=attempt, finish=finish, responseChars=len(text),
                errorCount=exc.error_count() if isinstance(exc, ValidationError) else 1, error=last_error[:300])
            trace.record(schema.__name__, **base, rawResponse=text, status="validation_failed" if text.strip() else "empty",
                         error=last_error)
            continue
        trace.record(schema.__name__, **base, rawResponse=text, status="ok", parsed=out.model_dump(mode="json"))
        return out
    log(logger, logging.ERROR, f"{prefix}_output_invalid_giving_up", model=model, schema=schema.__name__)
    raise AiOutputInvalid(f"{schema.__name__} failed validation twice: {last_error}")


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

    def _messages(self, system_msg: str, content: str) -> list[dict[str, str]]:
        return [{"role": "system", "content": system_msg}, {"role": "user", "content": content}]

    # --- structured ------------------------------------------------------------------------------

    def complete(self, what: str, system_msg: str, content: str, *, temperature: float, json_mode: bool = False,
                 max_output_tokens: int | None = None) -> Completion:
        """One non-streamed chat completion (with the bounded retries of _call)."""
        res = self._call(
            f"{self.model}:{what}",
            lambda client: client.chat.completions.create(
                model=self.model,
                messages=self._messages(system_msg, content),
                temperature=temperature,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
                **({"max_completion_tokens": max_output_tokens} if max_output_tokens else {}),
            ),
        )
        choice = (getattr(res, "choices", None) or [None])[0]
        message = getattr(choice, "message", None)
        return Completion(text=getattr(message, "content", None) or "", finish=getattr(choice, "finish_reason", None),
                          usage=usage_dict(getattr(res, "usage", None)), model=getattr(res, "model", None) or self.model,
                          refusal=bool(getattr(message, "refusal", None)))

    def generate_json(self, tier: Tier, system: str, prompt: str, schema: type[T], *, temperature: float = 0.2,
                      max_output_tokens: int | None = None, on_result: Callable[[dict], None] | None = None) -> T:
        """`max_output_tokens` caps the completion; when the model stops on that cap the partial JSON is not
        retried but raised as AiOutputTruncated. `on_result` receives {finish, usage, attempt} per response."""
        # `tier` is kept for the LLM protocol; every tier uses the one configured model.
        system_msg = json_system_message(system, schema)
        return run_json(
            lambda content: self.complete(schema.__name__, system_msg, content, temperature=temperature, json_mode=True,
                                          max_output_tokens=max_output_tokens),
            schema, system, prompt, tier=tier, temperature=temperature, max_output_tokens=max_output_tokens,
            on_result=on_result, model=self.model, prefix="openai",
        )

    # --- streaming -------------------------------------------------------------------------------

    def stream_chat(self, what: str, system_msg: str, content: str, *, temperature: float, json_mode: bool = False,
                    max_output_tokens: int | None = None, meta: dict[str, Any] | None = None,
                    cancel: threading.Event | None = None) -> Iterator[str]:
        """Streams one chat completion. Retries happen only until the first chunk arrives, so streamed text is never
        repeated. `meta` receives model, ttftMs, finish, usage and refusal. A failure after the first chunk raises
        AiStreamInterrupted; setting `cancel` closes the stream and raises AiCancelled."""
        meta = meta if meta is not None else {}
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(system_msg, content),
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_output_tokens:
            kwargs["max_completion_tokens"] = max_output_tokens

        def first_chunk(client: Any):
            stream = client.chat.completions.create(**kwargs)
            it = iter(stream)
            return stream, it, next(it, None)

        timer = Timer()
        log(logger, logging.INFO, "openai_stream_request", model=self.model, what=what, promptChars=len(content),
            jsonMode=json_mode)
        stream, it, chunk = self._call(f"{self.model}:{what}", first_chunk)
        meta.update(model=self.model, ttftMs=timer.ms())
        usage = None
        chars = 0
        try:
            while chunk is not None:
                if cancel is not None and cancel.is_set():
                    raise AiCancelled("generation cancelled by the caller")
                usage = getattr(chunk, "usage", None) or usage
                meta["model"] = getattr(chunk, "model", None) or meta["model"]
                for choice in getattr(chunk, "choices", None) or []:
                    finish = getattr(choice, "finish_reason", None)
                    if finish:
                        meta["finish"] = finish
                    delta = getattr(choice, "delta", None)
                    if getattr(delta, "refusal", None):
                        meta["refusal"] = True
                    text = getattr(delta, "content", None)
                    if text:
                        chars += len(text)
                        yield text
                chunk = next(it, None)
        except AiCancelled:
            log(logger, logging.INFO, "openai_stream_cancelled", model=self.model, chars=chars, latencyMs=timer.ms())
            raise
        except Exception as exc:
            log(logger, logging.WARNING, "openai_stream_interrupted", model=self.model, error=type(exc).__name__, chars=chars)
            raise AiStreamInterrupted("OpenAI stream was interrupted.", provider="openai", partial_chars=chars) from None
        finally:
            close = getattr(stream, "close", None)  # releases the HTTP response when the caller stops early
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        meta["usage"] = usage_dict(usage)
        log(logger, logging.INFO, "openai_stream_done", model=self.model, latencyMs=timer.ms(),
            inputTokens=getattr(usage, "prompt_tokens", None), outputTokens=getattr(usage, "completion_tokens", None),
            totalTokens=getattr(usage, "total_tokens", None))

    def stream_text(self, tier: Tier, system: str, prompt: str, *, temperature: float = 0.3,
                    cancel: threading.Event | None = None) -> Iterator[str]:
        meta: dict[str, Any] = {}
        yield from self.stream_chat("stream", f"{system}\n\n{INJECTION_GUARD}", prompt, temperature=temperature,
                                    meta=meta, cancel=cancel)
        if meta.get("finish") == "content_filter":
            raise AiSafetyBlocked("OpenAI declined this request.")


def build_llm(settings: Settings) -> OpenAILLM:
    return OpenAILLM(settings)
