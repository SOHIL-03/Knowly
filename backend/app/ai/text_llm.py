"""Text generation: streamed from Gemini with a first-token deadline per key, OpenAI as the final fallback.

Every text call (`generate_json` and `stream_text`) goes through one streaming path:

    Gemini key (the last one that worked; GEMINI_API_KEY_1 at process start)
        first text within GEMINI_FIRST_TOKEN_TIMEOUT_SECONDS  -> keep streaming from that key
        no text in time, quota, rate limit, auth, 5xx, network -> cancel that attempt, next key at once
    ... every configured key once ...
        every key failed, or a request-wide error (bad request, model not found, unknown error)
    OpenAI (OPENAI_MODEL), streamed, no first-token deadline, its own bounded retries before the first chunk

The deadline covers connecting, the provider's processing and the wait for the first non-empty text part; response
headers, empty chunks, usage metadata and thought parts do not count. Once text has arrived the deadline is gone. A
failure after that is a different category (AiStreamInterrupted): it is never continued on another key or provider,
because the new text would repeat or contradict what was already sent. Only buffered callers (generate_json, which
has shown nothing to anyone yet) discard the partial text and ask OpenAI for the whole answer once.

Each Gemini attempt runs as a task on one background asyncio loop (the google-genai async client). Its events reach
the caller through a queue owned by that attempt alone, so cancelling an attempt (timeout, the caller stopping early,
or `cancel` being set) closes its HTTP stream and nothing it produces later can reach the caller.

The text path has its own GeminiKeyPool, so a slow text answer never moves the key that embeddings and TTS use.
Key values are never logged: log lines carry the key's variable name and its position.
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from app.ai.client import INJECTION_GUARD, AiCancelled, AiSafetyBlocked, AiStreamInterrupted, T, Tier
from app.ai.gemini_keys import Category, GeminiKeyPool, classify
from app.ai.openai_llm import Completion, OpenAILLM, json_system_message, run_json
from app.config import Settings
from app.logs import Timer, exc_fields, get_logger, log

logger = get_logger(__name__)

POLL_SECONDS = 0.05  # how often a waiting caller checks `cancel`
# Key-specific or transient: the next key may work. Anything else is request-wide and goes straight to OpenAI.
ROTATE = {Category.QUOTA_EXHAUSTED, Category.RATE_LIMITED, Category.AUTH, Category.SERVER_UNAVAILABLE,
          Category.NETWORK, Category.TIMEOUT}
SAFETY_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY", "RECITATION"}


# --- background loop for the async Gemini client ----------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def background_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="llm-stream-loop", daemon=True).start()
            _loop = loop
    return _loop


Event = tuple  # ("text", str) | ("meta", finish, usage) | ("error", exc) | ("done",)


class _Attempt:
    """One provider request running on the background loop. After cancel() nothing more is queued and the caller stops
    reading this attempt's queue, so a late chunk of an abandoned attempt can never reach the caller."""

    def __init__(self, loop: asyncio.AbstractEventLoop, run: Callable[[Callable[[Event], None]], Any]) -> None:
        self._queue: queue.SimpleQueue[Event] = queue.SimpleQueue()
        self._closed = threading.Event()
        self._future = asyncio.run_coroutine_threadsafe(self._run(run), loop)

    def _emit(self, ev: Event) -> None:
        if not self._closed.is_set():
            self._queue.put(ev)

    async def _run(self, run: Callable[[Callable[[Event], None]], Any]) -> None:
        try:
            await run(self._emit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # delivered to the caller as an event, classified there
            self._emit(("error", exc))

    def next(self, timeout: float) -> Event | None:
        try:
            return self._queue.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def cancel(self) -> None:
        """Idempotent. Cancels the task, which closes the HTTP stream (the SDK closes the response in a finally)."""
        self._closed.set()
        self._future.cancel()


# --- Gemini response parsing ------------------------------------------------------------------------

def read_chunk(res: Any) -> tuple[str, str | None, dict[str, Any] | None]:
    """(answer text, finish reason in OpenAI's names, usage) of one streamed GenerateContentResponse. Thought parts are
    not answer text."""
    texts: list[str] = []
    finish: str | None = None
    if getattr(getattr(res, "prompt_feedback", None), "block_reason", None):
        finish = "content_filter"
    candidates = getattr(res, "candidates", None) or []
    if candidates:
        cand = candidates[0]
        for part in getattr(getattr(cand, "content", None), "parts", None) or []:
            text = getattr(part, "text", None)
            if text and not getattr(part, "thought", None):
                texts.append(text)
        reason = getattr(cand, "finish_reason", None)
        name = str(getattr(reason, "name", reason) or "")
        if name and name != "FINISH_REASON_UNSPECIFIED":
            finish = ("stop" if name == "STOP" else "length" if name == "MAX_TOKENS"
                      else "content_filter" if name in SAFETY_FINISH else name.lower())
    um = getattr(res, "usage_metadata", None)
    usage = None if um is None else {"input": getattr(um, "prompt_token_count", None),
                                     "output": getattr(um, "candidates_token_count", None),
                                     "total": getattr(um, "total_token_count", None)}
    return "".join(texts), finish, usage


# --- time-to-first-token statistics -------------------------------------------------------------------

class TtftStats:
    """Rolling p50/p95/p99 time to first text per provider (last `size` requests of this process), for the logs."""

    def __init__(self, size: int = 500) -> None:
        self._data: dict[str, deque[int]] = {}
        self._lock = threading.Lock()
        self.size = size

    def add(self, provider: str, ms: int) -> dict[str, int]:
        with self._lock:
            values = self._data.setdefault(provider, deque(maxlen=self.size))
            values.append(ms)
            ordered = sorted(values)

        def pct(p: float) -> int:
            return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]

        return {"ttftP50": pct(50), "ttftP95": pct(95), "ttftP99": pct(99), "ttftSamples": len(ordered)}


TTFT = TtftStats()


@dataclass
class _Request:
    what: str  # label for logs: the schema name, or "stream"
    system: str  # full system message (injection guard and JSON schema already included)
    prompt: str
    temperature: float
    max_output_tokens: int | None = None
    json_mode: bool = False


@dataclass
class _Outcome:
    provider: str = ""
    model: str = ""
    key: str | None = None
    attempts: int = 0  # Gemini attempts made
    ttft_ms: int | None = None
    finish: str | None = None
    usage: dict[str, Any] | None = None
    refusal: bool = False
    chars: int = 0
    fallback_reason: str = ""


class TextLLM:
    """The app's `LLM`: Gemini streaming with per-key first-token deadlines, then OpenAI."""

    def __init__(self, settings: Settings, *, openai: OpenAILLM | None = None, pool: GeminiKeyPool | None = None,
                 loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.settings = settings
        self.openai = openai or OpenAILLM(settings)
        self.pool = pool if pool is not None else GeminiKeyPool(settings)
        self.model = (settings.gemini_text_model or "").strip()
        self.first_token_timeout = max(0.05, float(settings.gemini_first_token_timeout_seconds))
        self.thinking_level = (settings.gemini_text_thinking_level or "").strip().upper()
        self._loop = loop
        self._current = 0  # index of the Gemini key that worked last; the next request starts there

    @property
    def gemini_configured(self) -> bool:
        return bool(self.pool.keys) and bool(self.model)

    # --- public LLM protocol ------------------------------------------------------------------------

    def generate_json(self, tier: Tier, system: str, prompt: str, schema: type[T], *, temperature: float = 0.2,
                      max_output_tokens: int | None = None, on_result: Callable[[dict], None] | None = None) -> T:
        """Streams internally, buffers the whole reply and validates it exactly as before (app/ai/openai_llm.run_json)."""
        system_msg = json_system_message(system, schema)
        return run_json(
            lambda content: self._complete(_Request(schema.__name__, system_msg, content, temperature, max_output_tokens,
                                                    json_mode=True)),
            schema, system, prompt, tier=tier, temperature=temperature, max_output_tokens=max_output_tokens,
            on_result=on_result, model=self.model if self.gemini_configured else self.openai.model, prefix="llm",
        )

    def stream_text(self, tier: Tier, system: str, prompt: str, *, temperature: float = 0.3,
                    cancel: threading.Event | None = None) -> Iterator[str]:
        out = _Outcome()
        yield from self._stream(_Request("stream", f"{system}\n\n{INJECTION_GUARD}", prompt, temperature), out, cancel)
        if out.finish == "content_filter":
            raise AiSafetyBlocked("The AI provider declined this request.")

    # --- buffered consumers ---------------------------------------------------------------------------

    def _complete(self, req: _Request) -> Completion:
        out = _Outcome()
        try:
            text = "".join(self._stream(req, out, None, buffered=True))
        except AiStreamInterrupted as exc:
            if exc.provider != "gemini":
                raise
            # Nobody has seen the partial text: drop it and generate the whole answer once on OpenAI (never appended).
            out = _Outcome(attempts=out.attempts, fallback_reason="gemini_mid_stream_failure")
            text = "".join(self._stream(req, out, None, buffered=True, use_gemini=False))
        return Completion(text=text, finish=out.finish, usage=out.usage, model=out.model, refusal=out.refusal,
                          provider=out.provider)

    # --- one generation -----------------------------------------------------------------------------

    def _stream(self, req: _Request, out: _Outcome, cancel: threading.Event | None, *, buffered: bool = False,
                use_gemini: bool = True) -> Iterator[str]:
        timer = Timer()
        status = "error"
        try:
            started = self._start_gemini(req, out, cancel) if use_gemini and self.gemini_configured else None
            if started is None:
                if not out.fallback_reason:
                    out.fallback_reason = "gemini_not_configured" if use_gemini else "gemini_skipped"
                yield from self._openai(req, out, cancel, buffered)
            else:
                attempt, first, finished = started
                try:
                    if first:
                        out.chars += len(first)
                        yield first
                    if not finished:
                        yield from self._drain(attempt, out, cancel)
                finally:
                    attempt.cancel()  # no-op when finished; closes the stream when the caller stopped early
            status = "success"
        except AiCancelled:
            status = "cancelled"
            raise
        except GeneratorExit:
            status = "stopped_by_caller"
            raise
        finally:
            usage = out.usage or {}
            log(logger, logging.INFO if status in ("success", "stopped_by_caller") else logging.WARNING,
                "llm_stream_done", what=req.what, provider=out.provider or None, model=out.model or None, key=out.key,
                geminiAttempts=out.attempts, ttftMs=out.ttft_ms, latencyMs=timer.ms(), chars=out.chars,
                finish=out.finish, status=status, fallbackReason=out.fallback_reason or None,
                inputTokens=usage.get("input"), outputTokens=usage.get("output"), totalTokens=usage.get("total"))

    def _start_gemini(self, req: _Request, out: _Outcome,
                      cancel: threading.Event | None) -> tuple[_Attempt, str, bool] | None:
        """Tries each key once, starting at the last one that worked, until one produces text within the deadline.
        Returns (attempt, first text, finished) or None when OpenAI should take over (reason in out.fallback_reason)."""
        keys = self.pool.keys
        total = len(keys)
        start = self._current % total
        for pos in range(total):
            if cancel is not None and cancel.is_set():
                raise AiCancelled("generation cancelled by the caller")
            idx = (start + pos) % total
            key = keys[idx]
            fallback_to = keys[(idx + 1) % total].name if pos + 1 < total else "openai"
            out.attempts += 1
            began = time.monotonic()  # before the client is fetched and the request starts
            deadline = began + self.first_token_timeout
            attempt: _Attempt | None = None
            status, first, error, meta = "first_token_timeout", "", None, (None, None)
            try:
                client = self.pool.client_for(key)
                attempt = _Attempt(self._loop or background_loop(),
                                   lambda emit, c=client: self._gemini_request(c, req, emit))
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    ev = attempt.next(min(remaining, POLL_SECONDS))
                    if cancel is not None and cancel.is_set():
                        attempt.cancel()
                        log(logger, logging.INFO, "llm_stream_cancelled", provider="gemini", key=key.name,
                            keyIndex=idx + 1, what=req.what, phase="before_first_token")
                        raise AiCancelled("generation cancelled by the caller")
                    if ev is None:
                        continue
                    if ev[0] == "text":
                        status, first = "success", ev[1]
                        break
                    if ev[0] == "meta":
                        meta = (ev[1] or meta[0], ev[2] or meta[1])
                    elif ev[0] == "done":
                        status = "completed_without_text"  # e.g. a safety block or an empty answer: not key-specific
                        break
                    elif ev[0] == "error":
                        status, error = "error", ev[1]
                        break
            except AiCancelled:
                raise
            except Exception as exc:  # creating the client failed
                status, error = "error", exc
            ttft_ms = int((time.monotonic() - began) * 1000)

            if status in ("success", "completed_without_text") and attempt is not None:
                if idx != self._current:
                    log(logger, logging.INFO, "gemini_text_key_switched", key=key.name,
                        previous=keys[self._current % total].name)
                    self._current = idx  # stay on the key that works
                out.provider, out.model, out.key = "gemini", self.model, key.name
                out.finish, out.usage = meta
                if status == "success":
                    out.ttft_ms = ttft_ms
                    log(logger, logging.INFO, "llm_stream_success", provider="gemini", model=self.model, key=key.name,
                        keyIndex=idx + 1, what=req.what, ttftMs=ttft_ms, attempts=out.attempts, status="success",
                        **TTFT.add("gemini", ttft_ms))
                return attempt, first, status == "completed_without_text"

            if attempt is not None:
                attempt.cancel()
            if error is None:
                category, rotate = "first_token_timeout", True
                fields: dict[str, Any] = {"timeoutSeconds": self.first_token_timeout}
            else:
                c = classify(error)
                category, rotate = c.category.value, c.category in ROTATE
                fields = {"code": c.code, "providerStatus": c.status, **exc_fields(error)}
            log(logger, logging.WARNING, "llm_stream_attempt", provider="gemini", model=self.model, key=key.name,
                keyIndex=idx + 1, what=req.what, ttftMs=ttft_ms, status=category,
                fallbackTo=fallback_to if rotate else "openai", **fields)
            if not rotate:
                out.fallback_reason = f"gemini_{category}"  # another key would get the same answer
                return None
        out.fallback_reason = "all_gemini_keys_exhausted"
        return None

    async def _gemini_request(self, client: Any, req: _Request, emit: Callable[[Event], None]) -> None:
        from google.genai import types

        config: dict[str, Any] = {"system_instruction": req.system, "temperature": req.temperature}
        if req.max_output_tokens:
            config["max_output_tokens"] = req.max_output_tokens
        if req.json_mode:
            config["response_mime_type"] = "application/json"
        if self.thinking_level:
            config["thinking_config"] = types.ThinkingConfig(thinking_level=self.thinking_level)
        stream = await client.aio.models.generate_content_stream(
            model=self.model, contents=req.prompt, config=types.GenerateContentConfig(**config))
        try:
            async for res in stream:
                text, finish, usage = read_chunk(res)
                if text:
                    emit(("text", text))
                if finish or usage:
                    emit(("meta", finish, usage))
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
        emit(("done",))

    def _drain(self, attempt: _Attempt, out: _Outcome, cancel: threading.Event | None) -> Iterator[str]:
        """The rest of a started Gemini stream. No deadline any more; the SDK's read timeout bounds a stalled stream."""
        while True:
            ev = attempt.next(POLL_SECONDS)
            if cancel is not None and cancel.is_set():
                log(logger, logging.INFO, "llm_stream_cancelled", provider="gemini", key=out.key, phase="streaming",
                    chars=out.chars)
                raise AiCancelled("generation cancelled by the caller")
            if ev is None:
                continue
            if ev[0] == "text":
                out.chars += len(ev[1])
                yield ev[1]
            elif ev[0] == "meta":
                out.finish = ev[1] or out.finish
                out.usage = ev[2] or out.usage
            elif ev[0] == "done":
                return
            elif ev[0] == "error":
                c = classify(ev[1])
                log(logger, logging.WARNING, "llm_stream_interrupted", provider="gemini", model=self.model, key=out.key,
                    category=c.category.value, status="mid_stream_failure", chars=out.chars, **exc_fields(ev[1]))
                raise AiStreamInterrupted("Gemini stream was interrupted.", provider="gemini", partial_chars=out.chars)

    def _openai(self, req: _Request, out: _Outcome, cancel: threading.Event | None, buffered: bool) -> Iterator[str]:
        if cancel is not None and cancel.is_set():
            raise AiCancelled("generation cancelled by the caller")
        log(logger, logging.WARNING if out.attempts else logging.INFO, "llm_fallback", fromProvider="gemini",
            toProvider="openai", reason=out.fallback_reason, what=req.what, geminiAttempts=out.attempts)
        out.provider, out.model, out.key = "openai", self.openai.model, None
        timer = Timer()
        if buffered and not self.settings.openai_streaming_enabled:
            res = self.openai.complete(req.what, req.system, req.prompt, temperature=req.temperature,
                                       json_mode=req.json_mode, max_output_tokens=req.max_output_tokens)
            out.ttft_ms, out.finish, out.usage, out.model, out.refusal = timer.ms(), res.finish, res.usage, res.model, res.refusal
            out.chars = len(res.text)
            if res.text:
                yield res.text
            return
        meta: dict[str, Any] = {}
        for text in self.openai.stream_chat(req.what, req.system, req.prompt, temperature=req.temperature,
                                            json_mode=req.json_mode, max_output_tokens=req.max_output_tokens,
                                            meta=meta, cancel=cancel):
            if out.ttft_ms is None:
                out.ttft_ms = timer.ms()
                log(logger, logging.INFO, "llm_stream_success", provider="openai", model=self.openai.model,
                    what=req.what, ttftMs=out.ttft_ms, attempts=out.attempts + 1, status="success",
                    **TTFT.add("openai", out.ttft_ms))
            out.chars += len(text)
            yield text
        out.finish, out.usage = meta.get("finish"), meta.get("usage")
        out.model, out.refusal = meta.get("model") or out.model, bool(meta.get("refusal"))


def build_text_llm(settings: Settings) -> TextLLM | OpenAILLM:
    """LLM_STREAMING_ENABLED=false keeps the previous OpenAI-only text generation."""
    return TextLLM(settings) if settings.llm_streaming_enabled else OpenAILLM(settings)
