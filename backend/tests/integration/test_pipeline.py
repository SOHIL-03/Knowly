"""Sample-mode briefing through the real B1-B10 stages (Firestore emulator, fake Gemini)."""

from __future__ import annotations

import json

from app.pipeline.runner import run_briefing
from tests.integration.conftest import User


def today(u: User) -> dict:
    r = u.get("/briefings/today")
    assert r.status_code == 200, r.text
    return r.json()


def test_auth_is_required(client):
    assert client.get("/api/me").status_code == 401
    r = client.get("/api/me", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401 and r.json()["error"]["code"] in ("unauthenticated", "session_expired")


def test_sample_briefing_end_to_end(asha, svc):
    r = asha.post("/briefings/generate", {"kind": "daily"})
    assert r.status_code == 202 and r.json()["created"] is True
    b = today(asha)
    assert b["status"] == "ready", b.get("error")
    assert b["mode"] == "sample"
    segs = b["segments"]
    assert 3 <= len(segs) <= 5
    assert [s["type"] for s in segs][:3] == ["project", "learning", "quickhits"]

    # progress counts are real, not hardcoded
    assert b["progress"]["issues"] == 3
    assert b["progress"]["storiesFound"] >= 10
    assert set(b["timings"]) >= {"fetch", "parse", "enrich", "dedupe", "rank", "plan", "brief"}

    item_ids = set(b["itemIds"])
    for s in segs:
        assert s["itemIds"] and set(s["itemIds"]) <= item_ids
        assert s["sourceMap"] and {m["itemId"] for m in s["sourceMap"]} <= set(s["itemIds"])
        assert s["whyChosen"] and s["openingQuestion"]
        # grounded two-host script, fact-checked before audio
        assert {line["speaker"] for line in s["script"]} == {"A", "B"}
        assert {line["speakerName"] for line in s["script"]} == {"Maya", "Theo"}
        assert s["faithfulness"] == "pass" and s["scriptMode"] == "model"
        # Gemini multi-speaker TTS produced the audio; timings cover every line
        assert s["audioStatus"] == "ok" and s["audioUrl"] and s["durationSec"] > 0
        assert len(s["lineTimings"]) == len(s["script"])
        assert s["voices"] == {"Maya": "Kore", "Theo": "Puck"}
        assert svc.blobs.get_bytes(f"audio/{asha.uid}/{b['id']}/{s['id']}.wav")[:4] == b"RIFF"
        for src in s["sources"]:
            assert src["newsletter"].endswith("(sample)")
    assert len(svc.tts.calls) == len(segs)
    assert all(len(voices) == 2 for _, voices in svc.tts.calls)

    # sponsors never reach a segment
    for iid in item_ids:
        assert not svc.paths.item(iid).get().to_dict()["isSponsor"]

    # every stored URL appears in its issue (none invented)
    for it in svc.paths.items().stream():
        d = it.to_dict()
        if d.get("url"):
            assert d["url"].startswith("https://") and "utm_" not in d["url"]

    # duplicate stories across newsletters are clustered (vector search URL appears in two issues)
    vs = [it.to_dict() for it in svc.paths.items().stream() if (it.to_dict().get("url") or "").endswith("vector-search")]
    assert len(vs) == 2 and vs[0]["clusterId"] == vs[1]["clusterId"] and vs[0]["coveredByCount"] == 2

    # wildcard placed in quick hits
    quick = next(s for s in segs if s["type"] == "quickhits")
    assert quick["wildcardItemId"] in quick["itemIds"]
    assert b["alsoInInbox"] is not None


def test_paywall_honesty_link_only_items_say_so(asha, svc):
    asha.post("/briefings/generate", {})
    b = today(asha)
    linkonly = {it.id for it in svc.paths.items().stream() if it.to_dict().get("linkOnly")}
    fetched = {it.id for it in svc.paths.items().stream() if it.to_dict().get("fullTextPath")}
    assert fetched, "the registered article should have been fetched"
    assert linkonly
    for s in b["segments"]:
        for line in s["script"]:
            if set(line["item_ids"]) & linkonly and not set(line["item_ids"]) & fetched:
                assert "Only the newsletter's short note was available" in line["text"]


def test_issue_parsing_is_cached_across_users(client, asha, svc):
    asha.post("/briefings/generate", {})
    extractions = sum(1 for _, name in svc.llm.calls if name == "ExtractionResult")
    assert extractions == 3
    ravi = User(client)
    ravi.onboard("Mobile app for a grocery store using Flutter", ["python web apis"])
    ravi.post("/briefings/generate", {})
    assert today(ravi)["status"] == "ready"
    assert sum(1 for _, name in svc.llm.calls if name == "ExtractionResult") == 3


def test_two_personas_get_different_briefings(client, asha):
    asha.post("/briefings/generate", {})
    ravi = User(client)
    ravi.onboard("Python web API with Pydantic models and browser speech input", ["fastapi dependencies"])
    ravi.post("/briefings/generate", {})
    a, r = today(asha), today(ravi)
    assert a["segments"][0]["itemIds"] != r["segments"][0]["itemIds"]


def test_generation_is_idempotent(asha, svc):
    first = asha.post("/briefings/generate", {}).json()
    second = asha.post("/briefings/generate", {}).json()
    assert first["briefingId"] == second["briefingId"] and second["created"] is False
    assert run_briefing(svc, asha.uid, first["briefingId"]) == "skipped"
    regen = asha.post("/briefings/generate", {"regenerate": True}).json()
    assert regen["briefingId"].endswith("-r2") and regen["created"]


def test_ai_outage_fails_honestly(asha, svc):
    svc.llm.unavailable = True
    asha.post("/briefings/generate", {})
    b = today(asha)
    assert b["status"] == "failed"
    assert "The AI service is unavailable" in b["error"]["message"]
    assert b["segments"] == []


def test_invalid_model_output_falls_back_but_stays_grounded(asha, svc):
    svc.llm.invalid = {"SegmentPlan", "PodcastScript"}
    asha.post("/briefings/generate", {})
    b = today(asha)
    assert b["status"] == "ready" and b["planMode"] == "fallback"
    for s in b["segments"]:
        assert s["scriptMode"] == "fallback"
        assert all(set(line["item_ids"]) <= set(s["itemIds"]) for line in s["script"])
        assert s["audioStatus"] == "ok"


def test_mute_topics_are_excluded(client, svc):
    u = User(client)
    u.onboard("Backend on Cloud Run", ["serverless"], mutes=["Pydantic", "Web Speech"])
    u.post("/briefings/generate", {})
    b = today(u)
    for s in b["segments"]:
        for src in s["sources"]:
            assert "Pydantic" not in src["headline"] and "Speech" not in src["headline"]


def test_empty_profile_still_produces_a_briefing(client):
    u = User(client)
    u.post("/me/bootstrap", {"timezone": "UTC"})
    u.post("/briefings/generate", {})
    assert today(u)["status"] == "ready"


def test_review_only_briefing_uses_saved_items(asha, svc):
    asha.post("/briefings/generate", {})
    b = today(asha)
    first_item = b["segments"][0]["itemIds"][0]
    assert asha.post("/library", {"itemId": first_item, "briefingId": b["id"], "segId": "s1"}).status_code == 200
    r = asha.post("/briefings/generate", {"kind": "review"}).json()
    rb = asha.get(f"/briefings/{r['briefingId']}").json()
    assert rb["status"] == "ready"
    assert [s["type"] for s in rb["segments"]] == ["review"]
    assert rb["segments"][0]["itemIds"] == [first_item]


def test_script_failing_the_faithfulness_check_is_not_voiced(asha, svc):
    svc.llm.script_unsupported = True  # every model script contains an unsupported claim
    asha.post("/briefings/generate", {})
    b = today(asha)
    assert b["status"] == "ready"
    for s in b["segments"]:
        assert s["scriptMode"] == "fallback"
        assert not any("UNSUPPORTED" in line["text"] for line in s["script"])
    for turns, _ in svc.tts.calls:
        assert not any("UNSUPPORTED" in t.text for t in turns)


def test_brief_style_uses_one_narrator_voice(client, svc):
    u = User(client)
    u.onboard("Backend on Cloud Run", ["serverless"])
    prefs = {"lengthMin": 5, "style": "brief", "speed": 1.0, "notify": "none",
             "schedule": {"mode": "daily", "time": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]}, "wildcardOn": True, "endQuizOn": True}
    assert u.put("/prefs", {"prefs": prefs}).status_code == 200
    u.post("/briefings/generate", {"regenerate": True})
    b = today(u)
    for s in b["segments"]:
        assert {line["speaker"] for line in s["script"]} == {"A"}
        assert s["voices"] == {"Maya": "Kore"}


def test_tts_outage_retries_then_falls_back_to_text(asha, svc):
    svc.tts.fail_times = 99
    asha.post("/briefings/generate", {})
    b = today(asha)
    assert b["status"] == "ready"  # the text briefing is published regardless of audio
    s = b["segments"][0]
    assert s["audioStatus"] == "failed" and s["audioUrl"] is None and s["audioAttempts"] == 2
    assert "text version" in s["audioMessage"]
    svc.tts.fail_times = 0
    assert asha.post(f"/briefings/{b['id']}/segments/{s['id']}/audio/retry").status_code == 202
    s = today(asha)["segments"][0]
    assert s["audioStatus"] == "ok" and s["audioUrl"]


def test_briefing_runs_through_openai_text_generation_into_the_unchanged_tts(asha, svc):
    """Real OpenAILLM (mocked OpenAI client) -> real B1-B10 stages -> the existing Gemini TTS boundary."""
    from app.ai.openai_llm import OpenAILLM
    from app.config import Settings
    from tests.fakes import FakeOpenAIClient

    client = FakeOpenAIClient()
    svc.llm = OpenAILLM(Settings(openai_api_key="sk-test-0123456789abcdef"), client=client)
    assert asha.post("/briefings/generate", {"kind": "daily"}).status_code == 202
    b = today(asha)
    assert b["status"] == "ready", b.get("error")

    # every text-generation request used the configured model
    assert client.requests and {r["model"] for r in client.requests} == {"gpt-4o-mini"}
    assert all(r["messages"][0]["role"] == "system" for r in client.requests)

    # the OpenAI-written scripts feed the existing TTS contract: speakers A/B -> Maya/Theo, Kore/Puck voices
    for s in b["segments"]:
        assert {line["speaker"] for line in s["script"]} == {"A", "B"}
        assert s["audioStatus"] == "ok" and s["voices"] == {"Maya": "Kore", "Theo": "Puck"}
    assert len(svc.tts.calls) == len(b["segments"])
    for turns, voices in svc.tts.calls:
        assert voices == {"Maya": "Kore", "Theo": "Puck"} and {t.speaker for t in turns} <= {"Maya", "Theo"}


def _text_llm(svc, *gemini_clients, **settings):
    from app.ai.gemini_keys import ApiKey, GeminiKeyPool
    from app.ai.openai_llm import OpenAILLM
    from app.ai.text_llm import TextLLM
    from app.config import Settings
    from tests.fakes import FakeOpenAIClient

    s = Settings(openai_api_key="sk-test-0123456789abcdef", openai_max_retries=0, **settings)
    clients = {f"GEMINI_API_KEY_{i + 1}": c for i, c in enumerate(gemini_clients)}
    pool = GeminiKeyPool(s, keys=[ApiKey(n, f"AIzaSyPIPE{i}" + "z" * 24) for i, n in enumerate(clients)],
                         client_factory=lambda k: clients[k.name])
    oa = FakeOpenAIClient()
    svc.llm = TextLLM(s, openai=OpenAILLM(s, client=oa), pool=pool)
    return oa


def _ask(u, b) -> dict:
    r = u.post("/ask", {"briefingId": b["id"], "segId": b["segments"][0]["id"], "question": "What does this mean for my index?"})
    assert r.status_code == 200, r.text
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "final"
    return events[-1]


def test_briefing_and_ask_run_on_streamed_gemini_text_with_openai_unused(asha, svc):
    """Real TextLLM (mocked Gemini streams) -> real B1-B10 stages -> the existing TTS, then a streamed Ask answer."""
    from tests.fakes import FakeGeminiTextClient

    gemini = FakeGeminiTextClient()
    oa = _text_llm(svc, gemini, FakeGeminiTextClient())
    assert asha.post("/briefings/generate", {"kind": "daily"}).status_code == 202
    b = today(asha)
    assert b["status"] == "ready", b.get("error")
    assert gemini.requests and {r["model"] for r in gemini.requests} == {"gemini-3.8-flash"}
    assert oa.requests == []  # no fallback was needed
    for s in b["segments"]:
        assert {line["speaker"] for line in s["script"]} == {"A", "B"} and s["audioStatus"] == "ok"

    final = _ask(asha, b)
    assert final["grounded"] is True and final["citations"]
    assert oa.requests == []


def test_briefing_and_ask_fall_back_to_streamed_openai_when_every_gemini_key_stalls(asha, svc):
    from tests.fakes import FakeGeminiTextClient

    stalled = [FakeGeminiTextClient(hang=True) for _ in range(4)]
    oa = _text_llm(svc, *stalled, gemini_first_token_timeout_seconds=0.05)
    assert asha.post("/briefings/generate", {"kind": "daily"}).status_code == 202
    b = today(asha)
    assert b["status"] == "ready", b.get("error")
    assert all(c.requests for c in stalled)  # every key was tried
    assert oa.requests and all(r["stream"] is True for r in oa.requests)
    assert {r["response_format"]["type"] for r in oa.requests if "response_format" in r} == {"json_object"}

    final = _ask(asha, b)
    assert final["grounded"] is True and final["citations"]


# --- review workflow: see the OpenAI responses, then start TTS by hand ---------------------------------

def _review_setup(svc):
    from app.ai.openai_llm import OpenAILLM
    from app.config import Settings
    from tests.fakes import FakeOpenAIClient

    client = FakeOpenAIClient()
    svc.llm = OpenAILLM(Settings(openai_api_key="sk-test-0123456789abcdef", openai_max_retries=0), client=client)
    return client


def _generate_for_review(asha, **extra):
    assert asha.post("/briefings/generate", {"kind": "daily", "reviewBeforeAudio": True, **extra}).status_code == 202
    return today(asha)


def test_review_mode_holds_tts_and_shows_the_exact_openai_responses(asha, svc):
    client = _review_setup(svc)
    b = _generate_for_review(asha)
    assert b["status"] == "ready" and b["reviewBeforeAudio"] is True
    # nothing was spoken: every segment waits for approval
    assert svc.tts.calls == [] and {s["audioStatus"] for s in b["segments"]} == {"review"}
    assert all(s["audioUrl"] is None and s["script"] for s in b["segments"])

    t = asha.get(f"/briefings/{b['id']}/llm-trace").json()
    assert t["enabled"] is True and t["model"] == "gpt-4o-mini"
    entries = t["entries"]
    # exactly one captured entry per API request: the preview made no extra call
    assert len(entries) == len(client.requests) == len(client.responses)
    assert [e["rawResponse"] for e in entries] == client.responses  # byte-for-byte what the API returned
    assert all(e["status"] == "ok" and e["parsed"] is not None for e in entries)
    assert all(e["usage"] == {"input": 100, "output": 50, "total": 150} and e["model"] == "gpt-4o-mini" for e in entries)
    steps = {e["step"] for e in entries}
    assert {"ExtractionResult", "SegmentPlan", "PodcastScript", "FaithfulnessResult"} <= steps
    scripts = [e for e in entries if e["step"] == "PodcastScript"]
    assert len(scripts) == len(b["segments"]) and all(e["appValidation"] == {"passed": True, "problems": []} for e in scripts)
    assert {e["segId"] for e in scripts} == {s["id"] for s in b["segments"]}
    assert "sk-test" not in str(t)


def test_generate_audio_sends_the_exact_reviewed_script_to_the_existing_tts(asha, svc):
    _review_setup(svc)
    b = _generate_for_review(asha)
    seg = b["segments"][0]
    r = asha.post(f"/briefings/{b['id']}/segments/{seg['id']}/audio/generate", {})
    assert r.status_code == 202 and r.json() == {"ok": True, "edited": False}
    after = next(s for s in today(asha)["segments"] if s["id"] == seg["id"])
    assert after["audioStatus"] == "ok" and after["audioUrl"] and not after.get("scriptEdited")
    assert len(svc.tts.calls) == 1  # only the approved segment was spoken
    turns, voices = svc.tts.calls[0]
    assert [t.text for t in turns] == [line["text"] for line in seg["script"]]
    assert voices == {"Maya": "Kore", "Theo": "Puck"}
    # the other segments are still waiting
    assert {s["audioStatus"] for s in today(asha)["segments"] if s["id"] != seg["id"]} == {"review"}


def test_edited_script_is_marked_modified_and_the_edit_is_what_gets_spoken(asha, svc):
    _review_setup(svc)
    b = _generate_for_review(asha)
    seg = b["segments"][0]
    texts = [line["text"] for line in seg["script"]]
    texts[1] = "A line I rewrote during review."
    r = asha.post(f"/briefings/{b['id']}/segments/{seg['id']}/audio/generate", {"texts": texts})
    assert r.status_code == 202 and r.json()["edited"] is True
    after = next(s for s in today(asha)["segments"] if s["id"] == seg["id"])
    assert after["scriptEdited"] is True and after["audioStatus"] == "ok"
    assert [line["text"] for line in after["script"]] == texts
    assert [line["text"] for line in after["scriptOriginal"]] == [line["text"] for line in seg["script"]]  # kept for comparison
    assert [t.text for t in svc.tts.calls[0][0]] == texts
    # speaker labels are untouched by a text edit
    assert [line["speaker"] for line in after["script"]] == [line["speaker"] for line in seg["script"]]


def test_generate_audio_rejects_bad_requests(asha, svc):
    _review_setup(svc)
    b = _generate_for_review(asha)
    seg = b["segments"][0]
    url = f"/briefings/{b['id']}/segments/{seg['id']}/audio/generate"
    assert asha.post(url, {"texts": ["only one"]}).status_code == 400
    assert asha.post(url, {"texts": [""] * len(seg["script"])}).status_code == 422
    assert svc.tts.calls == []
    assert asha.post(url, {}).status_code == 202
    assert asha.post(url, {}).status_code == 409  # already approved: no duplicate TTS
    assert len(svc.tts.calls) == 1


def test_default_briefing_is_unchanged_and_has_no_trace(asha, svc):
    assert asha.post("/briefings/generate", {"kind": "daily"}).status_code == 202
    b = today(asha)
    assert b["reviewBeforeAudio"] is False and {s["audioStatus"] for s in b["segments"]} == {"ok"}
    t = asha.get(f"/briefings/{b['id']}/llm-trace").json()
    assert t == {"enabled": False, "model": "gpt-4o-mini", "entries": []}
    assert asha.post(f"/briefings/{b['id']}/segments/s1/audio/generate", {}).status_code == 409


def test_review_mode_can_come_from_the_server_default(asha, svc):
    _review_setup(svc)
    svc.settings.review_before_audio = True
    assert asha.post("/briefings/generate", {"kind": "daily"}).status_code == 202
    assert today(asha)["reviewBeforeAudio"] is True and svc.tts.calls == []


def test_openai_failure_is_visible_in_the_trace_without_secrets(asha, svc):
    import httpx
    import openai

    client = _review_setup(svc)
    client.fail = openai.AuthenticationError(
        "Incorrect API key provided: sk-test-0123456789abcdef",
        response=httpx.Response(401, request=httpx.Request("POST", "https://api.openai.com")), body={"code": "invalid_api_key"})
    b = _generate_for_review(asha)
    assert b["status"] == "failed"
    t = asha.get(f"/briefings/{b['id']}/llm-trace").json()
    (e,) = t["entries"]
    assert e["status"] == "request_failed" and e["category"] == "auth" and "OPENAI_API_KEY" in e["error"]
    assert "sk-test" not in str(t) and "Traceback" not in str(t)
    assert svc.tts.calls == []
