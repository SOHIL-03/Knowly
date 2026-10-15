"""OpenAI text generation (app/ai/openai_llm.py). The OpenAI client is mocked; no network, no credits."""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.ai.client import AiOutputInvalid, AiQuotaExceeded, AiSafetyBlocked, AiUnavailable
from app.ai.openai_llm import OpenAILLM
from app.ai.schemas import ConceptCandidates, PodcastScript
from app.config import Settings
from app.logs import JsonFormatter

KEY = "sk-test-0123456789abcdefghijklmnop"
REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def api_error(cls, status: int, code: str | None = None, headers: dict | None = None):
    resp = httpx.Response(status, request=REQ, headers=headers or {})
    return cls("failure", response=resp, body={"code": code} if code else None)


def completion(content: str | None, finish: str = "stop", refusal: str | None = None):
    return SimpleNamespace(
        model="gpt-4o-mini",
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content, refusal=refusal))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
    )


class FakeOpenAI:
    """Scripted outcomes consumed in order (the last one repeats); exceptions are raised."""

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


def llm(client, **settings) -> tuple[OpenAILLM, list[float]]:
    sleeps: list[float] = []
    s = Settings(openai_api_key=KEY, **settings)
    return OpenAILLM(s, client=client, sleep=sleeps.append), sleeps


OK = completion(json.dumps({"concepts": ["rag"]}))


# --- configuration and client lifecycle ---------------------------------------------------------

def test_default_model_is_gpt_4o_mini():
    assert Settings().openai_model == "gpt-4o-mini"


def test_all_tiers_use_the_configured_model():
    client = FakeOpenAI(OK)
    m, _ = llm(client)
    m.generate_json("flash", "s", "p", ConceptCandidates)
    m.generate_json("pro", "s", "p", ConceptCandidates)
    assert [r["model"] for r in client.requests] == ["gpt-4o-mini", "gpt-4o-mini"]


def test_missing_key_is_a_clear_configuration_error():
    m = OpenAILLM(Settings(openai_api_key=""))
    with pytest.raises(AiUnavailable, match="OPENAI_API_KEY") as ei:
        m.generate_json("flash", "s", "p", ConceptCandidates)
    assert ei.value.category == "auth"


def test_client_is_created_once_from_the_key_and_reused(monkeypatch):
    created = []

    class Client(FakeOpenAI):
        def __init__(self, **kwargs):
            super().__init__(OK)
            created.append(kwargs)

    monkeypatch.setattr(openai, "OpenAI", Client)
    m = OpenAILLM(Settings(openai_api_key=KEY, openai_timeout_seconds=12))
    m.generate_json("flash", "s", "p", ConceptCandidates)
    m.generate_json("pro", "s", "p", ConceptCandidates)
    assert created == [{"api_key": KEY, "timeout": 12, "max_retries": 0}]


def test_key_is_not_in_settings_repr():
    assert KEY not in repr(Settings(openai_api_key=KEY))


# --- request shape and structured output --------------------------------------------------------

def test_request_uses_json_mode_schema_and_injection_guard():
    client = FakeOpenAI(OK)
    m, _ = llm(client)
    out = m.generate_json("flash", "SYSTEM RULES", "the prompt", ConceptCandidates, temperature=0.4)
    assert out.concepts == ["rag"]
    req = client.requests[0]
    assert req["response_format"] == {"type": "json_object"} and req["temperature"] == 0.4
    system, user = req["messages"]
    assert system["role"] == "system" and system["content"].startswith("SYSTEM RULES")
    assert "untrusted data" in system["content"] and '"concepts"' in system["content"] and "JSON" in system["content"]
    assert user == {"role": "user", "content": "the prompt"}


def test_podcast_script_contract_round_trips():
    script = {"title": "T", "opening_question": "Q?", "takeaways": ["x"],
              "lines": [{"speaker": "A", "text": "Hello", "item_ids": []}, {"speaker": "B", "text": "Hi", "style": "calm"}]}
    m, _ = llm(FakeOpenAI(completion(json.dumps(script))))
    out = m.generate_json("pro", "s", "p", PodcastScript)
    assert [line.speaker for line in out.lines] == ["A", "B"]  # the speaker labels the TTS stage maps to voices


def test_invalid_json_is_retried_once_with_the_validation_error():
    client = FakeOpenAI(completion("not json"), OK)
    m, sleeps = llm(client)
    assert m.generate_json("flash", "s", "p", ConceptCandidates).concepts == ["rag"]
    assert len(client.requests) == 2 and sleeps == []
    assert "failed validation" in client.requests[1]["messages"][1]["content"]


def test_schema_violation_twice_raises_output_invalid():
    bad = completion(json.dumps({"concepts": [str(i) for i in range(20)]}))  # max_length=8
    client = FakeOpenAI(bad)
    m, _ = llm(client)
    with pytest.raises(AiOutputInvalid):
        m.generate_json("flash", "s", "p", ConceptCandidates)
    assert len(client.requests) == 2


@pytest.mark.parametrize("content,finish", [(None, "stop"), ("", "stop"), ("  ", "stop"), ('{"concepts": [', "length")])
def test_empty_or_truncated_responses_never_return_data(content, finish):
    client = FakeOpenAI(completion(content, finish))
    m, _ = llm(client)
    with pytest.raises(AiOutputInvalid):
        m.generate_json("flash", "s", "p", ConceptCandidates)
    assert len(client.requests) == 2


def test_refusal_and_content_filter_are_safety_blocks_without_retry():
    for res in (completion(None, "stop", refusal="I can't help"), completion("", "content_filter")):
        client = FakeOpenAI(res)
        m, _ = llm(client)
        with pytest.raises(AiSafetyBlocked):
            m.generate_json("flash", "s", "p", ConceptCandidates)
        assert len(client.requests) == 1


# --- errors and bounded retries -----------------------------------------------------------------

def test_rate_limit_is_retried_with_backoff_then_succeeds():
    client = FakeOpenAI(api_error(openai.RateLimitError, 429, "rate_limit_exceeded"), OK)
    m, sleeps = llm(client)
    assert m.generate_json("flash", "s", "p", ConceptCandidates).concepts == ["rag"]
    assert len(client.requests) == 2 and len(sleeps) == 1


def test_retry_after_header_is_honoured():
    client = FakeOpenAI(api_error(openai.RateLimitError, 429, headers={"retry-after": "5"}), OK)
    m, sleeps = llm(client)
    m.generate_json("flash", "s", "p", ConceptCandidates)
    assert 4.0 <= sleeps[0] <= 6.0


def test_persistent_rate_limit_is_bounded():
    client = FakeOpenAI(api_error(openai.RateLimitError, 429))
    m, sleeps = llm(client, openai_max_retries=2)
    with pytest.raises(AiQuotaExceeded) as ei:
        m.generate_json("flash", "s", "p", ConceptCandidates)
    assert len(client.requests) == 3 and len(sleeps) == 2 and ei.value.category == "rate_limited"


@pytest.mark.parametrize("exc", [
    api_error(openai.InternalServerError, 503),
    openai.APITimeoutError(request=REQ),
    openai.APIConnectionError(request=REQ),
])
def test_transient_errors_are_retried_a_bounded_number_of_times(exc):
    client = FakeOpenAI(exc)
    m, sleeps = llm(client, openai_max_retries=2)
    with pytest.raises(AiUnavailable):
        m.generate_json("flash", "s", "p", ConceptCandidates)
    assert len(client.requests) == 3 and len(sleeps) == 2


def test_insufficient_quota_is_not_retried():
    client = FakeOpenAI(api_error(openai.RateLimitError, 429, "insufficient_quota"))
    m, sleeps = llm(client)
    with pytest.raises(AiQuotaExceeded) as ei:
        m.generate_json("flash", "s", "p", ConceptCandidates)
    assert len(client.requests) == 1 and sleeps == [] and ei.value.category == "quota_exhausted"


@pytest.mark.parametrize("exc,category", [
    (api_error(openai.AuthenticationError, 401, "invalid_api_key"), "auth"),
    (api_error(openai.PermissionDeniedError, 403), "auth"),
    (api_error(openai.NotFoundError, 404, "model_not_found"), "not_found"),
    (api_error(openai.BadRequestError, 400), "bad_request"),
])
def test_non_retryable_errors_surface_at_once_with_sanitized_messages(exc, category):
    client = FakeOpenAI(exc)
    m, sleeps = llm(client)
    with pytest.raises(AiUnavailable) as ei:
        m.generate_json("flash", "s", "p", ConceptCandidates)
    assert ei.value.category == category and len(client.requests) == 1 and sleeps == []
    assert KEY not in str(ei.value) and ei.value.__cause__ is None


def test_no_fallback_to_another_provider(monkeypatch):
    """An OpenAI failure surfaces as an error; google.genai is never touched."""
    import app.ai.gemini_keys as gk

    monkeypatch.setattr(gk.GeminiKeyPool, "call", lambda *a, **k: pytest.fail("Gemini was called for text generation"))
    m, _ = llm(FakeOpenAI(api_error(openai.InternalServerError, 500)), openai_max_retries=0)
    with pytest.raises(AiUnavailable):
        m.generate_json("flash", "s", "p", ConceptCandidates)


# --- usage logging and secrecy ------------------------------------------------------------------

def test_usage_model_duration_and_retries_are_logged_without_the_key():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        err = api_error(openai.RateLimitError, 429)
        err.args = (f"bad key {KEY}",)
        m, _ = llm(FakeOpenAI(err, OK))
        m.generate_json("flash", "s", "p", ConceptCandidates)
    finally:
        root.removeHandler(handler)
        root.setLevel(old)
    text = buf.getvalue()
    for needle in ("openai_call", "openai_attempt_failed", "latencyMs", "inputTokens", "outputTokens", "totalTokens", "gpt-4o-mini"):
        assert needle in text
    assert KEY not in text


# --- streaming ------------------------------------------------------------------------------------

def chunk(text: str | None, finish: str | None = None, usage=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish)] if text is not None or finish else [],
                           usage=usage)


def test_stream_yields_text_and_requests_usage():
    usage = SimpleNamespace(prompt_tokens=5, completion_tokens=3, total_tokens=8)
    client = FakeOpenAI(iter([chunk("Hel"), chunk("lo"), chunk(None, "stop"), chunk(None, usage=usage)]))
    m, _ = llm(client)
    assert "".join(m.stream_text("flash", "sys", "prompt")) == "Hello"
    req = client.requests[0]
    assert req["stream"] is True and req["stream_options"] == {"include_usage": True} and req["model"] == "gpt-4o-mini"


def test_stream_retries_only_before_the_first_chunk():
    client = FakeOpenAI(api_error(openai.InternalServerError, 503), iter([chunk("ok")]))
    m, sleeps = llm(client)
    assert "".join(m.stream_text("flash", "s", "p")) == "ok" and len(client.requests) == 2 and len(sleeps) == 1


def test_stream_failure_mid_way_is_not_replayed():
    def gen():
        yield chunk("Hello ")
        raise api_error(openai.InternalServerError, 503)

    client = FakeOpenAI(gen())
    m, _ = llm(client)
    out = []
    with pytest.raises(AiUnavailable):
        for c in m.stream_text("flash", "s", "p"):
            out.append(c)
    assert out == ["Hello "] and len(client.requests) == 1


def test_stream_without_key_is_a_configuration_error():
    with pytest.raises(AiUnavailable, match="OPENAI_API_KEY"):
        list(OpenAILLM(Settings(openai_api_key="")).stream_text("flash", "s", "p"))


# --- migration completeness -----------------------------------------------------------------------

def test_no_gemini_text_generation_remains_in_the_app():
    """Gemini is allowed only for embeddings (client.py) and TTS (tts.py); never generate_content."""
    from pathlib import Path

    app = Path(__file__).resolve().parents[2] / "app"
    users = {}
    for f in app.rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "generate_content" not in text, f"Gemini text generation call in {f.name}"
        assert "GeminiLLM" not in text, f"GeminiLLM referenced in {f.name}"
        if "google.genai" in text or "from google import genai" in text:
            users[f.name] = True
    assert set(users) == {"gemini_keys.py", "client.py"}  # key pool factory + embedding config types only


def test_services_wire_openai_for_text_and_leave_embeddings_and_tts_on_gemini(monkeypatch):
    from app import services
    from app.ai.client import GeminiEmbedder
    from app.audio.tts import GeminiSpeaker

    for mod, name in (("app.infra.firebase", "firestore_client"), ("app.infra.blobs", "GcsBlobStore"),
                      ("app.infra.secrets", "default_secret_store"), ("app.notify", "FcmNotifier"),
                      ("app.dispatch", "build_dispatcher")):
        monkeypatch.setattr(f"{mod}.{name}", lambda *a, **k: object())
    svc = services.build_services(Settings(openai_api_key=KEY))
    assert isinstance(svc.llm, OpenAILLM) and svc.llm.model == "gpt-4o-mini"
    assert isinstance(svc.embedder, GeminiEmbedder) and isinstance(svc.tts, GeminiSpeaker)
    assert svc.embedder.pool is svc.tts.pool  # TTS keeps the same shared Gemini key pool as before


# --- response capture for the review preview ----------------------------------------------------------

def traced(client, **settings):
    from app.ai import trace

    sink = trace.TraceSink()
    token = trace.start(sink)
    m, sleeps = llm(client, **settings)
    return m, sink, lambda: trace.stop(token)


def test_trace_records_exact_raw_response_parsed_usage_and_model():
    raw = '{"concepts":["rag"]}'
    client = FakeOpenAI(completion(raw))
    m, sink, stop = traced(client)
    try:
        m.generate_json("flash", "s", "the prompt text", ConceptCandidates)
    finally:
        stop()
    (e,) = sink.entries
    assert len(client.requests) == 1  # capturing never makes another API call
    assert e["rawResponse"] == raw and e["parsed"] == {"concepts": ["rag"]} and e["status"] == "ok"
    assert e["model"] == "gpt-4o-mini" and e["usage"] == {"input": 11, "output": 7, "total": 18}
    assert e["step"] == "ConceptCandidates" and e["promptPreview"] == "the prompt text" and e["durationMs"] >= 0


def test_trace_records_validation_failures_without_correcting_the_raw_text():
    bad = '{"concepts": "nope"}'
    client = FakeOpenAI(completion(bad), completion(bad))
    m, sink, stop = traced(client)
    try:
        with pytest.raises(AiOutputInvalid):
            m.generate_json("flash", "s", "p", ConceptCandidates)
    finally:
        stop()
    assert [e["status"] for e in sink.entries] == ["validation_failed"] * 2
    assert all(e["rawResponse"] == bad for e in sink.entries)
    assert all("parsed" not in e and e["error"] for e in sink.entries) and [e["attempt"] for e in sink.entries] == [1, 2]


def test_trace_records_empty_and_request_failures_with_sanitized_errors():
    m, sink, stop = traced(FakeOpenAI(completion("")))
    try:
        with pytest.raises(AiOutputInvalid):
            m.generate_json("flash", "s", "p", ConceptCandidates)
    finally:
        stop()
    assert {e["status"] for e in sink.entries} == {"empty"}

    m, sink, stop = traced(FakeOpenAI(api_error(openai.AuthenticationError, 401, "invalid_api_key")))
    try:
        with pytest.raises(AiUnavailable):
            m.generate_json("flash", "s", "p", ConceptCandidates)
    finally:
        stop()
    (e,) = sink.entries
    assert e["status"] == "request_failed" and e["category"] == "auth" and "OPENAI_API_KEY" in e["error"]
    assert KEY not in json.dumps(sink.entries)


def test_nothing_is_captured_without_an_active_sink():
    from app.ai import trace

    assert trace.active() is None
    m, _ = llm(FakeOpenAI(OK))
    m.generate_json("flash", "s", "p", ConceptCandidates)  # no sink: must simply work
    assert trace.active() is None


def test_trace_prompt_preview_is_truncated():
    m, sink, stop = traced(FakeOpenAI(OK))
    try:
        m.generate_json("flash", "s", "x" * 5000, ConceptCandidates)
    finally:
        stop()
    assert len(sink.entries[0]["promptPreview"]) == 1500 and sink.entries[0]["promptChars"] == 5000
