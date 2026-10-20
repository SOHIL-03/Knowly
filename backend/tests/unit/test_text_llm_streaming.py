"""Streaming text generation (app/ai/text_llm.py): Gemini key rotation on a first-token deadline, OpenAI as the final
fallback, cancellation, mid-stream failures and the buffered (JSON) consumers. Every provider is mocked; no network.

Timing is deterministic in outcome: a "hanging" fake never answers until it is cancelled, so it always exceeds the
(short) deadline, and an answering fake responds at once, so it is always well inside it.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
import time
from types import SimpleNamespace

import httpx
import openai
import pytest
from google.genai import errors

from app.ai.client import (
    AiCancelled,
    AiOutputInvalid,
    AiOutputTruncated,
    AiSafetyBlocked,
    AiStreamInterrupted,
    AiUnavailable,
)
from app.ai.gemini_keys import ApiKey, GeminiKeyPool
from app.ai.openai_llm import OpenAILLM
from app.ai.schemas import ConceptCandidates
from app.ai.text_llm import TextLLM, background_loop, read_chunk
from app.config import Settings
from app.logs import JsonFormatter, register_secret

OPENAI_KEY = "sk-test-0123456789abcdefghijklmnop"
GEMINI_SECRETS = [f"AIzaSyTEXTKEY{i:02d}" + "y" * 22 for i in range(1, 5)]
TIMEOUT = 0.3  # first-token deadline used in these tests (production default: 3 s)
REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


# --- Gemini fakes ---------------------------------------------------------------------------------

def response(text: str | None = None, *, finish: str | None = None, usage: tuple | None = None, thought: bool = False,
             blocked: bool = False):
    parts = [SimpleNamespace(text=text, thought=thought)] if text is not None else []
    cand = SimpleNamespace(content=SimpleNamespace(parts=parts),
                           finish_reason=SimpleNamespace(name=finish) if finish else None)
    um = SimpleNamespace(prompt_token_count=usage[0], candidates_token_count=usage[1], total_token_count=sum(usage)) if usage else None
    return SimpleNamespace(candidates=[] if blocked else [cand], usage_metadata=um,
                           prompt_feedback=SimpleNamespace(block_reason="SAFETY") if blocked else None)


class Script:
    """What one fake Gemini key does with a streaming request. Records cancellation and stream closing."""

    def __init__(self, kind: str, *texts: str, error: BaseException | None = None, finish: str = "STOP",
                 ignore_cancel: bool = False) -> None:
        self.kind, self.texts, self.error, self.finish, self.ignore_cancel = kind, texts, error, finish, ignore_cancel
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self.late_emitted = threading.Event()
        self.release = threading.Event()  # lets a "late" script produce text after it was abandoned

    async def stream(self):
        try:
            if self.kind == "answer":
                for t in self.texts:
                    yield response(t)
                yield response(finish=self.finish, usage=(12, 6))
            elif self.kind == "hang":
                await asyncio.sleep(3600)
            elif self.kind == "meta_then_hang":
                yield response(usage=(12, 0))  # metadata only
                yield response("thinking...", thought=True)  # a thought part is not answer text
                yield response("")  # empty text
                await asyncio.sleep(3600)
            elif self.kind == "fail_after":
                for t in self.texts:
                    yield response(t)
                raise self.error
            elif self.kind == "late":
                while not self.release.is_set():
                    try:
                        await asyncio.sleep(0.01)
                    except asyncio.CancelledError:
                        self.cancelled.set()
                        if not self.ignore_cancel:
                            raise
                self.late_emitted.set()
                yield response("LATE TEXT")
            elif self.kind == "blocked":
                yield response(blocked=True)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.closed.set()


class FakeGeminiClient:
    def __init__(self, script: Script | BaseException) -> None:
        self.script = script
        self.requests: list[dict] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self._start))

    async def _start(self, *, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        if isinstance(self.script, BaseException):
            raise self.script
        return self.script.stream()


# --- OpenAI fake --------------------------------------------------------------------------------------

def oa_chunk(text: str | None = None, finish: str | None = None, usage=None):
    choices = [SimpleNamespace(delta=SimpleNamespace(content=text, refusal=None), finish_reason=finish)] if text is not None or finish else []
    return SimpleNamespace(choices=choices, usage=usage, model="gpt-4o-mini")


def oa_stream(*texts: str):
    usage = SimpleNamespace(prompt_tokens=9, completion_tokens=4, total_tokens=13)
    return iter([oa_chunk(""), *[oa_chunk(t) for t in texts], oa_chunk(None, "stop"), oa_chunk(None, usage=usage)])


class FakeOpenAI:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        out = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(out, BaseException):
            raise out
        return out


# --- builders ---------------------------------------------------------------------------------------

def build(*scripts, openai_client=None, **settings):
    s = Settings(openai_api_key=OPENAI_KEY, gemini_first_token_timeout_seconds=TIMEOUT, **settings)
    clients = {f"GEMINI_API_KEY_{i + 1}": FakeGeminiClient(sc) for i, sc in enumerate(scripts)}
    keys = [ApiKey(name, GEMINI_SECRETS[i]) for i, name in enumerate(clients)]
    for k in keys:
        register_secret(k.value)
    pool = GeminiKeyPool(s, keys=keys, client_factory=lambda k: clients[k.name])
    oa = openai_client or FakeOpenAI(oa_stream("from ", "openai"))
    llm = TextLLM(s, openai=OpenAILLM(s, client=oa, sleep=lambda _s: None), pool=pool)
    return llm, list(clients.values()), oa


def requested(clients) -> list[int]:
    return [len(c.requests) for c in clients]


def hang():
    return Script("hang")


def answer(*texts, **kw):
    return Script("answer", *texts, **kw)


def quota_429():
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel", "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}})


def client_error(code: int, status: str):
    return errors.ClientError(code, {"error": {"code": code, "status": status, "message": "nope"}})


@pytest.fixture
def logs():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(old)


def events(buf: io.StringIO, name: str) -> list[dict]:
    out = []
    for line in buf.getvalue().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("message") == name:
            out.append(rec)
    return out


# --- 1-4: rotation on the first-token deadline -------------------------------------------------------

def test_key_1_answering_in_time_streams_from_key_1_only():
    llm, clients, oa = build(answer("Hel", "lo"), hang(), hang(), hang())
    assert list(llm.stream_text("flash", "sys", "prompt")) == ["Hel", "lo"]
    assert requested(clients) == [1, 0, 0, 0] and oa.requests == []
    cfg = clients[0].requests[0]["config"]
    assert clients[0].requests[0]["model"] == "gemini-3.8-flash" and clients[0].requests[0]["contents"] == "prompt"
    assert cfg.system_instruction.startswith("sys") and "untrusted data" in cfg.system_instruction
    assert cfg.response_mime_type is None and cfg.thinking_config.thinking_level.name == "MINIMAL"


def test_key_1_timeout_moves_to_key_2():
    llm, clients, oa = build(hang(), answer("two"), hang(), hang())
    started = time.monotonic()
    assert "".join(llm.stream_text("flash", "s", "p")) == "two"
    assert requested(clients) == [1, 1, 0, 0] and oa.requests == []
    assert TIMEOUT <= time.monotonic() - started < TIMEOUT + 1.0  # one deadline, no extra delay before key 2


def test_keys_1_to_3_time_out_and_key_4_answers():
    llm, clients, oa = build(hang(), hang(), hang(), answer("four"))
    assert "".join(llm.stream_text("flash", "s", "p")) == "four"
    assert requested(clients) == [1, 1, 1, 1] and oa.requests == []


def test_all_gemini_keys_time_out_then_openai_streams(logs):
    llm, clients, oa = build(hang(), hang(), hang(), hang())
    assert "".join(llm.stream_text("flash", "sys", "prompt", temperature=0.2)) == "from openai"
    assert requested(clients) == [1, 1, 1, 1]
    (req,) = oa.requests
    assert req["stream"] is True and req["model"] == "gpt-4o-mini" and req["temperature"] == 0.2
    assert req["messages"][0]["content"].startswith("sys") and req["messages"][1] == {"role": "user", "content": "prompt"}
    attempts = events(logs, "llm_stream_attempt")
    assert [a["status"] for a in attempts] == ["first_token_timeout"] * 4
    assert [a["keyIndex"] for a in attempts] == [1, 2, 3, 4]
    assert [a["fallbackTo"] for a in attempts] == ["GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4", "openai"]
    (fb,) = events(logs, "llm_fallback")
    assert fb["fromProvider"] == "gemini" and fb["toProvider"] == "openai" and fb["reason"] == "all_gemini_keys_exhausted"
    (done,) = events(logs, "llm_stream_done")
    assert done["provider"] == "openai" and done["geminiAttempts"] == 4 and done["status"] == "success"
    assert done["totalTokens"] == 13 and done["ttftMs"] is not None
    assert not any(secret in logs.getvalue() for secret in GEMINI_SECRETS + [OPENAI_KEY])


def test_metadata_empty_and_thought_chunks_do_not_count_as_the_first_token():
    llm, clients, _ = build(Script("meta_then_hang"), answer("real"))
    assert "".join(llm.stream_text("flash", "s", "p")) == "real"
    assert requested(clients) == [1, 1]


# --- 5-6: error handling during rotation -------------------------------------------------------------

def test_rate_limit_rotates_immediately_without_waiting(logs):
    llm, clients, oa = build(quota_429(), answer("ok"))
    started = time.monotonic()
    assert "".join(llm.stream_text("flash", "s", "p")) == "ok"
    assert time.monotonic() - started < TIMEOUT  # no deadline wait and no same-key retry
    assert requested(clients) == [1, 1] and oa.requests == []
    (a,) = events(logs, "llm_stream_attempt")
    assert a["status"] == "rate_limited" and a["fallbackTo"] == "GEMINI_API_KEY_2"


@pytest.mark.parametrize("exc", [client_error(401, "UNAUTHENTICATED"), errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}})])
def test_auth_and_server_errors_rotate(exc):
    llm, clients, _ = build(exc, answer("ok"))
    assert "".join(llm.stream_text("flash", "s", "p")) == "ok" and requested(clients) == [1, 1]


@pytest.mark.parametrize("exc,reason", [
    (client_error(400, "INVALID_ARGUMENT"), "gemini_bad_request"),
    (client_error(404, "NOT_FOUND"), "gemini_not_found"),
])
def test_request_wide_errors_skip_the_other_keys(exc, reason, logs):
    llm, clients, oa = build(exc, answer("never"), answer("never"), answer("never"))
    assert "".join(llm.stream_text("flash", "s", "p")) == "from openai"
    assert requested(clients) == [1, 0, 0, 0] and len(oa.requests) == 1
    (fb,) = events(logs, "llm_fallback")
    assert fb["reason"] == reason


def test_without_gemini_keys_openai_is_used_directly():
    llm, clients, oa = build()
    assert "".join(llm.stream_text("flash", "s", "p")) == "from openai" and len(oa.requests) == 1


def test_the_key_that_worked_is_tried_first_next_time():
    llm, clients, _ = build(quota_429(), answer("ok"), answer("x"), answer("x"))
    "".join(llm.stream_text("flash", "s", "p"))
    "".join(llm.stream_text("flash", "s", "p"))
    assert requested(clients) == [1, 2, 0, 0]


# --- 7-8: cancellation of abandoned attempts ---------------------------------------------------------

def test_timed_out_stream_is_cancelled_and_closed():
    first = hang()
    llm, _, _ = build(first, answer("ok"))
    assert "".join(llm.stream_text("flash", "s", "p")) == "ok"
    assert first.cancelled.wait(2) and first.closed.wait(2)


def test_an_abandoned_attempt_cannot_emit_late_text():
    late = Script("late", ignore_cancel=True)  # ignores cancellation and keeps going
    llm, _, _ = build(late, answer("second ", "key"))
    gen = llm.stream_text("flash", "s", "p")
    received = [next(gen)]
    late.release.set()
    assert late.late_emitted.wait(2)  # the abandoned attempt really produced text after it was dropped
    received += list(gen)
    assert received == ["second ", "key"] and "LATE TEXT" not in "".join(received)


def first_then_hang(script: Script):
    async def stream():
        try:
            yield response("first")
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            script.cancelled.set()
            raise
        finally:
            script.closed.set()

    script.stream = stream
    return script


def test_caller_stopping_early_closes_the_stream():
    script = first_then_hang(Script("custom"))
    llm, _, _ = build(script)
    gen = llm.stream_text("flash", "s", "p")
    assert next(gen) == "first"
    assert not script.closed.is_set()
    gen.close()  # e.g. Ask stops reading once it sees NOT_FOUND
    assert script.cancelled.wait(2) and script.closed.wait(2)


# --- 9: failure after the first token -----------------------------------------------------------------

def test_failure_after_the_first_chunk_is_not_continued_elsewhere(logs):
    broken = Script("fail_after", "Hello ", error=errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}))
    llm, clients, oa = build(broken, answer("other key"))
    got = []
    with pytest.raises(AiStreamInterrupted) as ei:
        for chunk in llm.stream_text("flash", "s", "p"):
            got.append(chunk)
    assert got == ["Hello "] and ei.value.provider == "gemini" and ei.value.partial_chars == 6
    assert isinstance(ei.value, AiUnavailable)  # existing handlers (Ask's error event, 503) still apply
    assert requested(clients) == [1, 0] and oa.requests == []  # no second provider appended to the partial text
    (ev,) = events(logs, "llm_stream_interrupted")
    assert ev["status"] == "mid_stream_failure"
    assert events(logs, "llm_stream_attempt") == []  # not counted as a first-token failure


def test_buffered_json_discards_partial_gemini_text_and_regenerates_once_on_openai(logs):
    broken = Script("fail_after", '{"concepts": ["ra', error=errors.ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}))
    oa = FakeOpenAI(oa_stream('{"concepts": ', '["rag"]}'))
    llm, clients, _ = build(broken, answer("x"), openai_client=oa)
    out = llm.generate_json("flash", "s", "p", ConceptCandidates)
    assert out.concepts == ["rag"]  # the partial Gemini text was not glued to OpenAI's answer
    assert requested(clients) == [1, 0] and len(oa.requests) == 1
    assert [e["reason"] for e in events(logs, "llm_fallback")] == ["gemini_mid_stream_failure"]


# --- 10-11: non-streaming consumers and structured output --------------------------------------------

def test_generate_json_buffers_the_stream_and_validates():
    llm, clients, oa = build(answer('{"conc', 'epts": ["rag", ', '"vectors"]}'))
    seen = []
    out = llm.generate_json("pro", "SYSTEM RULES", "the prompt", ConceptCandidates, temperature=0.4,
                            max_output_tokens=900, on_result=seen.append)
    assert isinstance(out, ConceptCandidates) and out.concepts == ["rag", "vectors"]
    cfg = clients[0].requests[0]["config"]
    assert cfg.response_mime_type == "application/json" and cfg.temperature == 0.4 and cfg.max_output_tokens == 900
    assert cfg.system_instruction.startswith("SYSTEM RULES") and '"concepts"' in cfg.system_instruction
    assert seen == [{"finish": "stop", "usage": {"input": 12, "output": 6, "total": 18}, "attempt": 1}]
    assert oa.requests == []


def test_generate_json_retries_invalid_output_once_with_the_error():
    class TwoAnswers(FakeGeminiClient):
        def __init__(self):
            super().__init__(answer("not json"))
            self.scripts = [answer("not json"), answer('{"concepts": ["rag"]}')]

        async def _start(self, *, model, contents, config):
            self.requests.append({"contents": contents})
            return self.scripts[len(self.requests) - 1].stream()

    s = Settings(openai_api_key=OPENAI_KEY, gemini_first_token_timeout_seconds=TIMEOUT)
    client = TwoAnswers()
    pool = GeminiKeyPool(s, keys=[ApiKey("GEMINI_API_KEY_1", GEMINI_SECRETS[0])], client_factory=lambda k: client)
    llm = TextLLM(s, openai=OpenAILLM(s, client=FakeOpenAI(oa_stream("x"))), pool=pool)
    assert llm.generate_json("flash", "s", "p", ConceptCandidates).concepts == ["rag"]
    assert len(client.requests) == 2 and "failed validation" in client.requests[1]["contents"]


def test_generate_json_raises_output_invalid_after_two_bad_answers():
    llm, _, _ = build(answer("still not json"))
    with pytest.raises(AiOutputInvalid):
        llm.generate_json("flash", "s", "p", ConceptCandidates)


def test_gemini_max_tokens_is_reported_as_truncation():
    llm, clients, oa = build(answer('{"concepts": ["ra', finish="MAX_TOKENS"))
    with pytest.raises(AiOutputTruncated):
        llm.generate_json("pro", "s", "p", ConceptCandidates, max_output_tokens=20)
    assert requested(clients) == [1] and oa.requests == []


def test_gemini_safety_block_is_not_retried_or_sent_to_openai():
    llm, clients, oa = build(Script("blocked"), answer("x"))
    with pytest.raises(AiSafetyBlocked):
        llm.generate_json("flash", "s", "p", ConceptCandidates)
    with pytest.raises(AiSafetyBlocked):
        list(llm.stream_text("flash", "s", "p"))
    assert requested(clients) == [2, 0] and oa.requests == []


def test_json_fallback_to_openai_streams_in_json_mode():
    oa = FakeOpenAI(oa_stream('{"concepts": ["rag"]}'))
    llm, _, _ = build(hang(), openai_client=oa)
    assert llm.generate_json("flash", "s", "p", ConceptCandidates, max_output_tokens=300).concepts == ["rag"]
    (req,) = oa.requests
    assert req["stream"] is True and req["response_format"] == {"type": "json_object"} and req["max_completion_tokens"] == 300


def test_json_fallback_without_openai_streaming_uses_one_plain_completion():
    done = SimpleNamespace(model="gpt-4o-mini", usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                           choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"concepts": ["rag"]}', refusal=None))])
    oa = FakeOpenAI(done)
    llm, _, _ = build(hang(), openai_client=oa, openai_streaming_enabled=False)
    assert llm.generate_json("flash", "s", "p", ConceptCandidates).concepts == ["rag"]
    assert "stream" not in oa.requests[0]


def test_trace_entries_name_the_provider():
    from app.ai import trace

    sink = trace.TraceSink()
    token = trace.start(sink)
    try:
        llm, _, _ = build(answer('{"concepts": ["rag"]}'))
        llm.generate_json("flash", "s", "p", ConceptCandidates)
    finally:
        trace.stop(token)
    (e,) = sink.entries
    assert e["status"] == "ok" and e["provider"] == "gemini" and e["model"] == "gemini-3.8-flash"
    assert e["rawResponse"] == '{"concepts": ["rag"]}'


# --- 12: caller cancellation ---------------------------------------------------------------------------

def test_cancel_while_waiting_stops_without_further_fallbacks():
    llm, clients, oa = build(hang(), answer("x"), answer("x"), answer("x"))
    cancel = threading.Event()
    threading.Timer(TIMEOUT / 3, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(AiCancelled):
        list(llm.stream_text("flash", "s", "p", cancel=cancel))
    assert time.monotonic() - started < TIMEOUT  # stopped before the deadline would have moved on
    assert requested(clients) == [1, 0, 0, 0] and oa.requests == []
    assert clients[0].script.cancelled.wait(2)


def test_cancel_while_streaming_closes_the_stream():
    script = first_then_hang(Script("custom"))
    llm, _, oa = build(script)
    cancel = threading.Event()
    gen = llm.stream_text("flash", "s", "p", cancel=cancel)
    assert next(gen) == "first"
    cancel.set()
    with pytest.raises(AiCancelled):
        next(gen)
    assert script.cancelled.wait(2) and oa.requests == []


def test_cancel_during_openai_fallback_closes_the_openai_stream():
    closed = threading.Event()

    class Stream:
        def __init__(self):
            self.it = oa_stream("a", "b", "c")

        def __iter__(self):
            return self.it

        def close(self):
            closed.set()

    llm, _, oa = build(hang(), openai_client=FakeOpenAI(Stream()))
    cancel = threading.Event()
    gen = llm.stream_text("flash", "s", "p", cancel=cancel)
    assert next(gen) == "a"
    cancel.set()
    with pytest.raises(AiCancelled):
        next(gen)
    assert closed.is_set()


# --- 13: OpenAI fallback failure ------------------------------------------------------------------------

def test_openai_failure_after_gemini_is_a_controlled_error():
    err = openai.AuthenticationError("bad key", response=httpx.Response(401, request=REQ), body={"code": "invalid_api_key"})
    llm, clients, oa = build(hang(), hang(), openai_client=FakeOpenAI(err))
    with pytest.raises(AiUnavailable) as ei:
        llm.generate_json("flash", "s", "p", ConceptCandidates)
    assert ei.value.category == "auth" and OPENAI_KEY not in str(ei.value) and ei.value.__cause__ is None
    assert len(oa.requests) == 1  # no loop back to Gemini
    with pytest.raises(AiUnavailable):
        list(llm.stream_text("flash", "s", "p"))


def test_openai_not_configured_after_gemini_fails_is_a_clear_error():
    s = Settings(openai_api_key="", gemini_first_token_timeout_seconds=TIMEOUT)
    pool = GeminiKeyPool(s, keys=[ApiKey("GEMINI_API_KEY_1", GEMINI_SECRETS[0])], client_factory=lambda k: FakeGeminiClient(hang()))
    llm = TextLLM(s, pool=pool)
    with pytest.raises(AiUnavailable, match="OPENAI_API_KEY"):
        list(llm.stream_text("flash", "s", "p"))


# --- helpers ------------------------------------------------------------------------------------------------

def test_read_chunk_maps_gemini_finish_reasons_and_usage():
    assert read_chunk(response("hi")) == ("hi", None, None)
    assert read_chunk(response(finish="MAX_TOKENS", usage=(3, 2)))[1:] == ("length", {"input": 3, "output": 2, "total": 5})
    assert read_chunk(response(finish="SAFETY"))[1] == "content_filter"
    assert read_chunk(response(blocked=True)) == ("", "content_filter", None)
    assert read_chunk(response("secret plan", thought=True))[0] == ""


def test_background_loop_is_shared():
    assert background_loop() is background_loop()
