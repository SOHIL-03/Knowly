"""Gemini API key failover (app/ai/gemini_keys.py), used by embeddings and TTS. Every client is mocked; no network.

Errors are real google.genai.errors objects built in the shapes the Gemini API returns
(QuotaFailure + RetryInfo details), including the ones observed on a free-tier key.
"""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.ai.client import AiQuotaExceeded, GeminiEmbedder
from app.ai.gemini_keys import (
    MAX_TRANSIENT_RETRIES_PER_KEY,
    ApiKey,
    Category,
    GeminiError,
    GeminiKeyPool,
    classify,
    load_keys,
)
from app.config import Settings
from app.logs import JsonFormatter, register_secret

SECRETS = [f"AIzaSyTESTKEY{i:02d}" + "x" * 22 for i in range(1, 11)]  # fake, key-shaped values


# --- error builders (shapes from the Gemini API) ------------------------------------------------

def quota_error(per_day: bool = True, limit_zero: bool = False, retry: str = "51191s") -> errors.ClientError:
    qid = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else "GenerateRequestsPerMinutePerProjectPerModel"
    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                             "quotaId": qid, "quotaValue": "0" if limit_zero else "20"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
        ]}})


def rate_limit_error(retry: str = "7s") -> errors.ClientError:
    return quota_error(per_day=False, retry=retry)


def server_error(code: int = 503) -> errors.ServerError:
    return errors.ServerError(code, {"error": {"code": code, "status": "UNAVAILABLE",
                                               "message": "This model is currently experiencing high demand."}})


def client_error(code: int, status: str, reason: str | None = None) -> errors.ClientError:
    details = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason}] if reason else []
    return errors.ClientError(code, {"error": {"code": code, "status": status, "message": "nope", "details": details}})


# --- fake clients ----------------------------------------------------------------------------

class Script:
    """Per-key scripted outcomes: a list of exceptions/results consumed in order for each key."""

    def __init__(self, plan: dict[str, list]) -> None:
        self.plan = {k: list(v) for k, v in plan.items()}
        self.calls: list[str] = []

    def factory(self, key: ApiKey):
        def call(*_args, **_kwargs):
            self.calls.append(key.name)
            outcomes = self.plan.get(key.name) or ["ok"]
            out = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            if isinstance(out, BaseException):
                raise out
            return SimpleNamespace(text=json.dumps({"concepts": [key.name]}), prompt_feedback=None, candidates=[],
                                   usage_metadata=None, value=out, embeddings=[SimpleNamespace(values=[3.0, 4.0])])

        models = SimpleNamespace(generate_content=call, embed_content=call)
        return SimpleNamespace(models=models, interactions=SimpleNamespace(create=call), key=key.name)


def keys(n: int = 10, missing: tuple[int, ...] = ()) -> list[ApiKey]:
    return [ApiKey(f"GEMINI_API_KEY_{i}", SECRETS[i - 1]) for i in range(1, n + 1) if i not in missing]


def pool(script: Script, ks: list[ApiKey] | None = None, sleeps: list[float] | None = None) -> GeminiKeyPool:
    sleeps = sleeps if sleeps is not None else []
    return GeminiKeyPool(Settings(), keys=ks if ks is not None else keys(), client_factory=script.factory,
                         sleep=sleeps.append)


def run(p: GeminiKeyPool):
    return p.call("test", lambda client: client.models.generate_content(model="m", contents="c", config=None))


# --- 1-6: key selection ------------------------------------------------------------------------

def test_1_key1_succeeds_without_fallback():
    s = Script({})
    assert run(pool(s)).value == "ok"
    assert s.calls == ["GEMINI_API_KEY_1"]


def test_2_key1_quota_exhausted_then_key2_succeeds():
    s = Script({"GEMINI_API_KEY_1": [quota_error()]})
    res = run(pool(s))
    assert s.calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2"] and res.value == "ok"


def test_3_keys_1_to_4_exhausted_then_key5_succeeds():
    s = Script({f"GEMINI_API_KEY_{i}": [quota_error()] for i in range(1, 5)})
    run(pool(s))
    assert s.calls == [f"GEMINI_API_KEY_{i}" for i in range(1, 6)]


def test_4_all_ten_keys_exhausted_raises_sanitized_error():
    s = Script({f"GEMINI_API_KEY_{i}": [quota_error()] for i in range(1, 11)})
    with pytest.raises(GeminiError) as ei:
        run(pool(s))
    err = ei.value
    assert err.category == Category.QUOTA_EXHAUSTED and err.attempts == 10
    assert "All 10 configured Gemini API key(s)" in str(err)
    assert not any(secret in str(err) for secret in SECRETS)
    assert err.__cause__ is None and err.__suppress_context__  # provider error is not attached
    assert s.calls == [f"GEMINI_API_KEY_{i}" for i in range(1, 11)]


def test_5_a_working_key_is_kept_and_a_dead_key_is_not_retried_on_every_request():
    s = Script({"GEMINI_API_KEY_1": [quota_error()]})  # key 1 is exhausted from now on
    p = pool(s)
    run(p)  # key 1 fails -> key 2 works, and the pool stays on key 2
    run(p)
    run(p)
    assert s.calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY_2", "GEMINI_API_KEY_2"]


def test_5b_when_the_current_key_later_fails_it_moves_on_and_wraps_around_to_earlier_keys():
    s = Script({"GEMINI_API_KEY_1": [quota_error(), "ok"], "GEMINI_API_KEY_2": ["ok", quota_error()]})
    p = pool(s, keys(2))
    run(p)  # 1 fails -> 2 ok (now on 2)
    run(p)  # 2 fails -> wraps to 1, which works again
    run(p)  # stays on 1
    assert s.calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY_2", "GEMINI_API_KEY_1", "GEMINI_API_KEY_1"]


def test_6_missing_keys_are_skipped_in_order():
    settings = Settings(gemini_api_key_1=SECRETS[0], gemini_api_key_3=SECRETS[2], gemini_api_key_4="   ",
                        gemini_api_key_7=SECRETS[6])
    ks = load_keys(settings)
    assert [k.name for k in ks] == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_3", "GEMINI_API_KEY_7"]
    s = Script({"GEMINI_API_KEY_1": [quota_error()], "GEMINI_API_KEY_3": [quota_error()]})
    run(pool(s, ks))
    assert s.calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_3", "GEMINI_API_KEY_7"]


def test_legacy_single_key_is_used_when_no_numbered_keys():
    ks = load_keys(Settings(gemini_api_key=SECRETS[0]))
    assert [k.name for k in ks] == ["GEMINI_API_KEY"]
    assert load_keys(Settings()) == []
    with pytest.raises(GeminiError) as ei:
        GeminiKeyPool(Settings(), keys=[]).call("x", lambda c: None)
    assert ei.value.category == Category.AUTH


# --- 7-9: no key fallback ------------------------------------------------------------------------

@pytest.mark.parametrize("exc,category", [
    (client_error(400, "INVALID_ARGUMENT"), Category.BAD_REQUEST),
    (client_error(404, "NOT_FOUND"), Category.NOT_FOUND),
])
def test_7_8_request_or_model_errors_do_not_switch_keys(exc, category):
    s = Script({"GEMINI_API_KEY_1": [exc]})
    sleeps: list[float] = []
    with pytest.raises(GeminiError) as ei:
        run(pool(s, sleeps=sleeps))
    assert ei.value.category == category
    assert s.calls == ["GEMINI_API_KEY_1"] and sleeps == []


@pytest.mark.parametrize("exc", [
    client_error(400, "INVALID_ARGUMENT", reason="API_KEY_INVALID"),
    client_error(401, "UNAUTHENTICATED"),
    client_error(403, "PERMISSION_DENIED"),
])
def test_9_a_rejected_key_moves_on_to_the_next_key_and_stays_there(exc):
    s = Script({"GEMINI_API_KEY_1": [exc]})
    p = pool(s)
    run(p)
    run(p)
    assert s.calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY_2"]


# --- 10-11: transient errors and bounded retries -----------------------------------------------

def test_10_transient_429_retries_same_key_honouring_retry_delay():
    s = Script({"GEMINI_API_KEY_1": [rate_limit_error("7s"), "ok"]})
    sleeps: list[float] = []
    run(pool(s, sleeps=sleeps))
    assert s.calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_1"]
    assert len(sleeps) == 1 and 6.0 <= sleeps[0] <= 8.0  # server's RetryInfo, with small jitter


def test_10b_persistent_rate_limit_moves_on_after_bounded_retries():
    s = Script({"GEMINI_API_KEY_1": [rate_limit_error("2s")]})  # key 1 always rate limited
    sleeps: list[float] = []
    run(pool(s, sleeps=sleeps))
    assert s.calls == ["GEMINI_API_KEY_1"] * (MAX_TRANSIENT_RETRIES_PER_KEY + 1) + ["GEMINI_API_KEY_2"]
    assert len(sleeps) == MAX_TRANSIENT_RETRIES_PER_KEY


def test_10c_long_retry_after_is_not_waited_inside_a_request():
    s = Script({"GEMINI_API_KEY_1": [rate_limit_error("90s")]})
    sleeps: list[float] = []
    run(pool(s, sleeps=sleeps))
    assert s.calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2"] and sleeps == []


def test_10d_server_overload_retries_same_key_then_moves_to_the_next_key():
    s = Script({"GEMINI_API_KEY_1": [server_error(503)]})
    sleeps: list[float] = []
    run(pool(s, sleeps=sleeps))
    assert s.calls == ["GEMINI_API_KEY_1"] * (MAX_TRANSIENT_RETRIES_PER_KEY + 1) + ["GEMINI_API_KEY_2"]
    assert len(sleeps) == MAX_TRANSIENT_RETRIES_PER_KEY and sleeps[0] < sleeps[1]


def test_10e_overload_on_every_key_ends_as_a_sanitized_unavailable_error():
    s = Script({f"GEMINI_API_KEY_{i}": [server_error(503)] for i in range(1, 4)})
    with pytest.raises(GeminiError) as ei:
        run(pool(s, keys(3)))
    assert ei.value.category == Category.SERVER_UNAVAILABLE and "temporarily unavailable" in str(ei.value)
    assert len(s.calls) == 3 * (MAX_TRANSIENT_RETRIES_PER_KEY + 1)


def test_11_no_infinite_retries_and_each_key_once_for_quota():
    s = Script({f"GEMINI_API_KEY_{i}": [rate_limit_error("1s")] for i in range(1, 11)})
    with pytest.raises(GeminiError):
        run(pool(s))
    assert len(s.calls) == 10 * (MAX_TRANSIENT_RETRIES_PER_KEY + 1)  # hard upper bound
    q = Script({f"GEMINI_API_KEY_{i}": [quota_error()] for i in range(1, 11)})
    with pytest.raises(GeminiError):
        run(pool(q))
    assert len(q.calls) == len(set(q.calls)) == 10  # each key exactly once


# --- 12: secrets ---------------------------------------------------------------------------------

def test_12_no_secrets_in_logs_or_exceptions():
    for secret in SECRETS:
        register_secret(secret)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        leaky = client_error(400, "INVALID_ARGUMENT")
        leaky.args = (f"bad request for key {SECRETS[0]}",)
        s = Script({f"GEMINI_API_KEY_{i}": [quota_error()] for i in range(1, 10)} | {"GEMINI_API_KEY_10": [leaky]})
        with pytest.raises(GeminiError) as ei:
            run(pool(s))
        logging.getLogger("x").error("oops %s", SECRETS[3])  # even an accidental log of a key is redacted
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    text = buf.getvalue()
    assert "GEMINI_API_KEY_3" in text and "attempt" in text and "durationMs" in text
    assert '"1/10"' in text and '"10/10"' in text
    for secret in SECRETS:
        assert secret not in text and secret not in str(ei.value) and secret not in repr(ei.value)
    assert "[REDACTED]" in text
    assert SECRETS[0] not in repr(ApiKey("GEMINI_API_KEY_1", SECRETS[0]))
    assert SECRETS[0] not in repr(Settings(gemini_api_key_1=SECRETS[0]))


# --- classification of the real error shapes ---------------------------------------------------

def test_classification_of_observed_errors():
    assert classify(quota_error()).category == Category.QUOTA_EXHAUSTED  # per day, retryDelay 51191s
    assert classify(quota_error(per_day=False, limit_zero=True, retry="5s")).category == Category.QUOTA_EXHAUSTED
    rl = classify(rate_limit_error("7s"))
    assert rl.category == Category.RATE_LIMITED and rl.retry_after == 7.0
    assert classify(server_error(503)).category == Category.SERVER_UNAVAILABLE
    assert classify(server_error(500)).category == Category.SERVER_UNAVAILABLE

    class ConnectError(Exception):
        pass

    assert classify(ConnectError()).category == Category.NETWORK
    assert classify(ValueError("x")).category == Category.OTHER


def test_retry_after_header_is_respected():
    import httpx

    exc = errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
                             response=httpx.Response(429, headers={"Retry-After": "4"}))
    c = classify(exc)
    assert c.category == Category.RATE_LIMITED and c.retry_after == 4.0


def test_interactions_style_errors_are_classified():
    """The TTS Interactions client raises errors with status_code/body instead of code/details."""

    class APIStatusError(Exception):
        def __init__(self):
            super().__init__("429")
            self.status_code = 429
            self.body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}

    assert classify(APIStatusError()).category == Category.QUOTA_EXHAUSTED


# --- integration with the app's clients ---------------------------------------------------------

def test_embedder_maps_exhausted_keys_to_quota_error():
    q = Script({f"GEMINI_API_KEY_{i}": [quota_error()] for i in range(1, 4)})
    with pytest.raises(AiQuotaExceeded) as ei:
        GeminiEmbedder(Settings(), pool(q, keys(3))).embed(["x"])
    assert ei.value.category == "quota_exhausted" and ei.value.__cause__ is None


def test_embeddings_and_tts_use_the_pool():
    from app.audio.tts import GeminiSpeaker, Turn, wav_from_pcm

    s = Script({"GEMINI_API_KEY_1": [quota_error()]})
    p = pool(s, keys(2))
    emb = GeminiEmbedder(Settings(), p)
    assert emb.embed(["hello"]) == [[0.6, 0.8]]
    assert s.calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2"]

    import base64

    wav = wav_from_pcm(bytes(4800))
    tts_calls = []

    def factory(key: ApiKey):
        def create(**kwargs):
            tts_calls.append(key.name)
            if key.name == "GEMINI_API_KEY_1":
                raise quota_error()
            return SimpleNamespace(output_audio=SimpleNamespace(data=base64.b64encode(wav).decode()))

        return SimpleNamespace(interactions=SimpleNamespace(create=create))

    tp = GeminiKeyPool(Settings(), keys=keys(2), client_factory=factory, sleep=lambda s: None)
    speech = GeminiSpeaker(Settings(), tp).synthesize([Turn("", "Hi")], {"Tutor": "Kore"})
    assert speech.duration_sec == pytest.approx(0.1)
    assert tts_calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2"]


class ReadTimeout(Exception):
    """Stands in for httpx.ReadTimeout (classified by class name)."""


def test_a_call_that_never_returns_is_abandoned_after_the_deadline_and_the_next_key_is_used():
    import threading

    never = threading.Event()
    calls: list[str] = []

    def factory(key):
        def call(*_a, **_k):
            calls.append(key.name)
            if key.name == "GEMINI_API_KEY_1":
                never.wait(30)  # hangs, like the speech endpoint on a bad key
            return SimpleNamespace(value="ok")

        return SimpleNamespace(models=SimpleNamespace(generate_content=call))

    p = GeminiKeyPool(Settings(gemini_timeout_seconds=0.3), keys=keys(3), client_factory=factory, sleep=lambda s: None)
    assert run(p).value == "ok"
    assert run(p).value == "ok"  # stays on key 2: the hung key is not tried again
    assert calls == ["GEMINI_API_KEY_1", "GEMINI_API_KEY_2", "GEMINI_API_KEY_2"]
    never.set()


def test_every_key_hanging_ends_as_a_timeout_error_not_a_forever_wait():
    import threading

    never = threading.Event()
    factory = lambda key: SimpleNamespace(models=SimpleNamespace(generate_content=lambda *a, **k: never.wait(30)))  # noqa: E731
    p = GeminiKeyPool(Settings(gemini_timeout_seconds=0.2), keys=keys(2), client_factory=factory, sleep=lambda s: None)
    with pytest.raises(GeminiError) as ei:
        run(p)
    assert ei.value.category == Category.TIMEOUT and "did not answer in time" in str(ei.value)
    never.set()


def test_timeouts_are_classified_separately_from_network_errors():
    assert classify(ReadTimeout("x")).category == Category.TIMEOUT
    assert classify(TimeoutError("Operation timed out")).category == Category.TIMEOUT


def test_the_real_client_is_created_with_a_request_timeout(monkeypatch):
    from google import genai

    seen = {}
    monkeypatch.setattr(genai.Client, "__init__", lambda self, **kw: seen.update(kw))
    GeminiKeyPool(Settings(gemini_timeout_seconds=33.0), keys=keys(1))._default_factory(keys(1)[0])
    assert seen["http_options"].timeout == 33_000
