"""Sample-mode briefing through the real B1-B10 stages (Firestore emulator, fake Gemini)."""

from __future__ import annotations

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
    assert "Gemini is unavailable" in b["error"]["message"]
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
