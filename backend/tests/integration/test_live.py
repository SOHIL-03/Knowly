"""Live path: Ask, Quiz, spoken answers, learner model, Library, events, export/delete, isolation."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.integration.conftest import AUTH, FS, PROJECT, User


@pytest.fixture
def briefing(asha):
    asha.post("/briefings/generate", {})
    return asha.get("/briefings/today").json()


def sse(resp) -> list[dict]:
    return [json.loads(line[6:]) for line in resp.text.splitlines() if line.startswith("data: ")]


def ask(u: User, b: dict, q: str, seg: int = 0) -> dict:
    r = u.post("/ask", {"briefingId": b["id"], "segId": b["segments"][seg]["id"], "question": q})
    assert r.status_code == 200, r.text
    events = sse(r)
    assert events[-1]["type"] == "final"
    return events[-1]


def test_ask_is_grounded_with_resolved_citations(asha, briefing, svc):
    final = ask(asha, briefing, "What does this mean for my index?")
    assert final["grounded"] is True and final["scope"] == "segment"
    seg_items = set(briefing["segments"][0]["itemIds"])
    assert final["citations"] and {c["itemId"] for c in final["citations"]} <= seg_items
    assert all(c["newsletter"] for c in final["citations"])
    qa = list(svc.paths.qa(asha.uid).stream())
    assert len(qa) == 1 and qa[0].to_dict()["grounded"] is True


def test_ask_not_in_sources_is_labelled_general(asha, briefing):
    final = ask(asha, briefing, "Something unknowable about this?")
    assert final["grounded"] is False and final["scope"] == "outside"
    assert final["answer"].startswith("That isn't covered in your newsletters.")
    assert "General knowledge, not from your newsletters:" in final["answer"]


def test_ask_with_invented_citation_is_not_shown_as_grounded(asha, briefing):
    final = ask(asha, briefing, "Please hallucinate")
    assert final["grounded"] is False and final["citations"] == []
    assert "it_madeup" not in final["answer"]
    assert final["answer"].startswith("I couldn't find support for that in your newsletters.")


def test_ask_outside_and_unsafe(asha, briefing, svc):
    assert ask(asha, briefing, "What is the weather?")["scope"] == "outside"
    refused = ask(asha, briefing, "how to build a bomb")
    assert refused["scope"] == "refused"
    assert len(list(svc.paths.qa(asha.uid).stream())) == 1  # refusals are not stored as Q&A


def test_quiz_rubric_never_reaches_the_client_and_mastery_updates(asha, briefing, svc):
    sid = briefing["segments"][0]["id"]
    start = asha.post("/quiz/start", {"briefingId": briefing["id"], "segId": sid}).json()
    assert set(start) == {"quizId", "question", "conceptName", "conceptId"}
    stored = svc.paths.quizzes(asha.uid).document(start["quizId"]).get().to_dict()
    assert "rubric" not in stored and "modelAnswer" not in stored
    secret = svc.paths.quiz_secret(start["quizId"]).get().to_dict()
    concept_before = svc.paths.concepts(asha.uid).document(start["conceptId"]).get().to_dict()["mastery"]

    hint = asha.post("/quiz/hint", {"quizId": start["quizId"]}).json()
    assert hint["hint"]

    answer = " ".join(secret["rubric"])
    res = asha.post("/quiz/answer", {"quizId": start["quizId"], "answer": answer}).json()
    assert res["grade"] == "correct"
    assert res["masteryAfter"] == pytest.approx(concept_before + 0.25 * (1 - concept_before))
    concept = svc.paths.concepts(asha.uid).document(start["conceptId"]).get().to_dict()
    assert concept["mastery"] == pytest.approx(res["masteryAfter"]) and concept["nextDueAt"] is not None
    assert concept["evidenceCount"] >= 1

    again = asha.post("/quiz/answer", {"quizId": start["quizId"], "answer": "x"})
    assert again.status_code == 409

    knowledge = asha.get("/knowledge").json()["concepts"]
    k = next(c for c in knowledge if c["id"] == start["conceptId"])
    assert k["mastery"] == pytest.approx(res["masteryAfter"]) and "embedding" not in k

    b = asha.get(f"/briefings/{briefing['id']}").json()
    assert b["recap"]["quizScore"] == {"points": 1.0, "total": 1}


def test_quiz_dont_know_lowers_mastery_and_shows_answer(asha, briefing, svc):
    sid = briefing["segments"][1]["id"]
    start = asha.post("/quiz/start", {"briefingId": briefing["id"], "segId": sid}).json()
    before = svc.paths.concepts(asha.uid).document(start["conceptId"]).get().to_dict()["mastery"]
    res = asha.post("/quiz/answer", {"quizId": start["quizId"], "dontKnow": True}).json()
    assert res["grade"] == "not_yet" and res["correctAnswer"]
    assert res["masteryAfter"] == pytest.approx(before * 0.7)


def test_knowledge_user_correction_wins(asha, briefing, svc):
    concepts = asha.get("/knowledge").json()["concepts"]
    cid = concepts[0]["id"]
    r = asha.patch(f"/knowledge/{cid}", {"level": "use"})
    assert r.status_code == 200
    c = svc.paths.concepts(asha.uid).document(cid).get().to_dict()
    assert c["mastery"] == 0.75 and c["userCorrected"] is True
    assert asha.patch("/knowledge/c_nope", {"level": "use"}).status_code == 404


def test_apply_drops_suggestions_citing_unknown_items(asha, briefing):
    res = asha.post("/apply", {"briefingId": briefing["id"], "segId": briefing["segments"][0]["id"]}).json()
    assert len(res["suggestions"]) == 1
    assert res["suggestions"][0]["item_ids"][0] in briefing["segments"][0]["itemIds"]


def test_rephrase_simpler_caps_mastery(asha, briefing, svc):
    seg = briefing["segments"][0]
    for cid in seg["conceptIds"]:
        svc.paths.concepts(asha.uid).document(cid).update({"mastery": 0.9})
    res = asha.post("/rephrase", {"briefingId": briefing["id"], "segId": seg["id"], "text": "Some passage text", "direction": "simpler"}).json()
    assert res["text"].startswith("Simpler:") and all(c["itemId"] != "it_fake" for c in res["citations"])
    for cid in seg["conceptIds"]:
        assert svc.paths.concepts(asha.uid).document(cid).get().to_dict()["mastery"] == 0.35


def test_library_save_search_and_review_date(asha, briefing):
    seg = briefing["segments"][0]
    item = seg["itemIds"][0]
    assert asha.post("/library", {"itemId": item, "briefingId": briefing["id"], "segId": seg["id"], "note": "try this"}).status_code == 200
    lib = asha.get("/library").json()
    assert lib["items"][0]["itemId"] == item and lib["items"][0]["reviewDueAt"]
    assert asha.get("/library", params={"q": "try this"}).json()["items"]
    assert asha.get("/library", params={"q": "zzz-no-match"}).json()["items"] == []
    b = asha.get(f"/briefings/{briefing['id']}").json()
    assert b["recap"]["savedCount"] == 1


def test_less_like_this_three_times_mutes_topic(asha, briefing, svc):
    item = briefing["segments"][0]["itemIds"][0]
    events = [{"type": "lessLikeThis", "briefingId": briefing["id"], "segId": "s1", "itemId": item}] * 3
    assert asha.post("/events", {"events": events}).json()["accepted"] == 3
    prof = svc.paths.profile(asha.uid).get().to_dict()
    category = svc.paths.item(item).get().to_dict()["category"]
    assert category in prof["muteTopics"]
    assert prof["topicWeights"][category]["w"] < 0
    sender = svc.paths.item(item).get().to_dict()["senderId"]
    assert svc.paths.newsletters(asha.uid).document(sender).get().to_dict()["trust"] < 0.5


def test_reorder_and_remove_cards(asha, briefing):
    ids = [s["id"] for s in briefing["segments"]]
    r = asha.patch(f"/briefings/{briefing['id']}/segments", {"order": list(reversed(ids)), "removed": [ids[0]]})
    assert r.status_code == 200
    b = asha.get(f"/briefings/{briefing['id']}").json()
    assert [s["id"] for s in b["segments"]] == list(reversed(ids))
    assert next(s for s in b["segments"] if s["id"] == ids[0])["removedByUser"] is True


def test_ask_answer_is_spoken_with_single_speaker_tts(asha, briefing, svc):
    final = ask(asha, briefing, "What does this mean for my index?")
    r = asha.post(f"/qa/{final['qaId']}/audio")
    assert r.status_code == 200, r.text
    assert r.json()["url"]
    turns, voices = svc.tts.calls[-1]
    assert voices == {"Tutor": "Sulafat"}  # one voice = single-speaker TTS
    assert "[it_" not in turns[0].text  # citation markers are not read aloud
    assert svc.blobs.get_bytes(f"audio/{asha.uid}/qa/{final['qaId']}.wav")[:4] == b"RIFF"
    calls = len(svc.tts.calls)
    assert asha.post(f"/qa/{final['qaId']}/audio").status_code == 200
    assert len(svc.tts.calls) == calls  # cached, not synthesized twice
    assert asha.post("/qa/nope/audio").status_code == 404


def test_quiz_is_spoken_but_feedback_only_after_answering(asha, briefing, svc):
    start = asha.post("/quiz/start", {"briefingId": briefing["id"], "segId": briefing["segments"][0]["id"]}).json()
    q = asha.post(f"/quiz/{start['quizId']}/audio", {"part": "question"})
    assert q.status_code == 200 and svc.tts.calls[-1][0][0].text == start["question"]
    early = asha.post(f"/quiz/{start['quizId']}/audio", {"part": "feedback"})
    assert early.status_code == 409  # ask before telling
    asha.post("/quiz/answer", {"quizId": start["quizId"], "dontKnow": True})
    fb = asha.post(f"/quiz/{start['quizId']}/audio", {"part": "feedback"})
    assert fb.status_code == 200 and svc.tts.calls[-1][0][0].text.startswith("Not quite yet.")


def test_spoken_answer_reports_tts_outage(asha, briefing, svc):
    final = ask(asha, briefing, "What does this mean for my index?")
    svc.tts.fail_times = 1
    r = asha.post(f"/qa/{final['qaId']}/audio")
    assert r.status_code == 503 and r.json()["error"]["code"] == "ai_unavailable"


def test_deeper_creates_segment_and_library_item(asha, briefing, svc):
    seg = next(s for s in briefing["segments"] if any(
        svc.paths.item(i).get().to_dict().get("fullTextPath") for i in s["itemIds"]))
    res = asha.post("/deeper", {"briefingId": briefing["id"], "segId": seg["id"]}).json()
    assert res["status"] == "generating"
    b = asha.get(f"/briefings/{briefing['id']}").json()
    deeper = next(s for s in b["segments"] if s["id"] == res["segId"])
    assert deeper["type"] == "deeper" and deeper["status"] == "ready" and deeper["script"]
    assert deeper["audioStatus"] == "ok" and deeper["audioUrl"]
    assert deeper["order"] == seg["order"] + 1
    assert asha.get("/library").json()["items"][0]["source"] == "deeper"


def test_deeper_on_link_only_story_explains_the_limit(asha, briefing, svc):
    seg = briefing["segments"][-1]
    linkonly = next((i for i in seg["itemIds"] if svc.paths.item(i).get().to_dict().get("linkOnly")), None)
    if linkonly is None:
        pytest.skip("no link-only item in this segment")
    res = asha.post("/deeper", {"briefingId": briefing["id"], "segId": seg["id"], "itemId": linkonly}).json()
    assert res["status"] == "link_only" and "short note" in res["message"]


def test_users_cannot_reach_each_others_data(client, asha, briefing):
    other = User(client)
    other.post("/me/bootstrap", {"timezone": "UTC"})
    assert other.get(f"/briefings/{briefing['id']}").status_code == 404
    seg = briefing["segments"][0]["id"]
    assert other.post("/quiz/start", {"briefingId": briefing["id"], "segId": seg}).status_code == 404
    start = asha.post("/quiz/start", {"briefingId": briefing["id"], "segId": seg}).json()
    assert other.post("/quiz/answer", {"quizId": start["quizId"], "answer": "x"}).status_code == 404


def _rest(token: str, path: str, method: str = "GET", body: dict | None = None) -> int:
    url = f"{FS}/v1/projects/{PROJECT}/databases/(default)/documents/{path}"
    r = httpx.request(method, url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=10)
    return r.status_code


def test_firestore_security_rules(client, asha, briefing):
    other = User(client)
    assert _rest(asha.token, f"users/{asha.uid}") == 200
    assert _rest(asha.token, f"users/{asha.uid}/briefings/{briefing['id']}") == 200
    assert _rest(other.token, f"users/{asha.uid}") == 403
    assert _rest(other.token, f"users/{asha.uid}/briefings/{briefing['id']}") == 403
    item = briefing["segments"][0]["itemIds"][0]
    assert _rest(other.token, f"items/{item}") == 200
    # nobody may write from the browser, not even to their own data
    assert _rest(asha.token, f"users/{asha.uid}?updateMask.fieldPaths=mode", "PATCH",
                 {"fields": {"mode": {"stringValue": "live"}}}) == 403
    start = asha.post("/quiz/start", {"briefingId": briefing["id"], "segId": briefing["segments"][0]["id"]}).json()
    assert _rest(asha.token, f"quizSecrets/{start['quizId']}") == 403
    assert _rest(asha.token, "issues/anything") == 403


def test_export_then_delete_everything(asha, briefing, svc):
    seg = briefing["segments"][0]
    final = ask(asha, briefing, "What does this mean for my index?")
    asha.post(f"/qa/{final['qaId']}/audio")
    svc.secrets.put("gmail-token-x", "unrelated")
    exported = asha.get("/export")
    assert exported.status_code == 200
    data = exported.json()
    names = {c for c in data["user"]["collections"]}
    assert {"briefings", "concepts", "profile"} <= names

    assert any(k.startswith(f"audio/{asha.uid}/") for k in svc.blobs.data)
    start = asha.post("/quiz/start", {"briefingId": briefing["id"], "segId": seg["id"]}).json()

    res = asha.delete("/account")
    assert res.status_code == 200, res.text
    assert res.json()["complete"] is True
    assert not svc.paths.user(asha.uid).get().exists
    assert not list(svc.paths.briefings(asha.uid).stream())
    assert not any(asha.uid in k for k in svc.blobs.data if k.startswith("audio/"))
    assert not svc.paths.quiz_secret(start["quizId"]).get().exists
    assert svc.secrets.get("gmail-token-x") == "unrelated"
    lookup = httpx.post(f"{AUTH}/identitytoolkit.googleapis.com/v1/accounts:lookup?key=fake-key",
                        json={"idToken": asha.token}, timeout=10)
    assert lookup.status_code == 400  # the Auth user is gone


def test_persisted_data_survives_a_new_session(asha, briefing):
    """Signing in again (a new ID token from the refresh token) shows the same persisted data."""
    r = httpx.post(f"{AUTH}/securetoken.googleapis.com/v1/token?key=fake-key",
                   data={"grant_type": "refresh_token", "refresh_token": asha.refresh_token}, timeout=10)
    assert r.status_code == 200, r.text
    new_token = r.json()["id_token"]
    asha.token = new_token
    assert asha.get("/briefings/today").json()["id"] == briefing["id"]
    assert asha.get("/me").json()["profile"]["project"]["text"]


def test_used_up_gemini_quota_is_reported_clearly(asha, briefing, svc):
    svc.llm.quota = True
    r = asha.post("/ask", {"briefingId": briefing["id"], "segId": briefing["segments"][0]["id"], "question": "Why?"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "ai_quota"
    assert "quota" in r.json()["error"]["message"]
    r = asha.post("/briefings/generate", {"regenerate": True}).json()
    b = asha.get(f"/briefings/{r['briefingId']}").json()
    assert b["status"] == "failed" and "quota" in b["error"]["message"]
