"""Podcast page, chat (text and voice), concepts, assessments and the verified learning profile.

Real API app + Firestore/Auth emulators. OpenAI text, Gemini STT and Gemini TTS are deterministic fakes (tests/fakes.py),
so these tests prove OUR routing, persistence and policy, not the real providers.
"""

from __future__ import annotations

import pytest

from app.learning import profile
from app.tldr import podcast
from tests.fakes import FakeSTT
from tests.integration.conftest import User

CAT, DATE, SID = "tech", "2026-10-16", "big-tech"
PID = f"{CAT}.{DATE}.{SID}"
LINES = [
    {"speaker": "Maya", "role": "question", "text": "What is vector search?", "ids": []},
    {"speaker": "Theo", "role": "answer", "text": "Vector search finds the nearest vectors to a query embedding by similarity.", "ids": ["k1"]},
    {"speaker": "Maya", "role": "question", "text": "Why do embeddings matter?", "ids": []},
    {"speaker": "Theo", "role": "answer", "text": "Embeddings turn text into numbers so that similar meaning sits close together.", "ids": ["k2"]},
]
RIGHT, WRONG = "1", "0"  # option index: every question is multiple choice, option 1 is correct in the fake


def seed(svc, variant: str | None = None, owner: str | None = None) -> str:
    key = f"{SID}.{variant}" if variant else SID
    podcast._save(svc, CAT, DATE, key, status="ready", title="Vectors", sectionName="Big Tech", lines=LINES, sources=[],
                  hosts={"A": "Maya", "B": "Theo"}, takeaways=[], audioPath=f"podcasts/tldr/{CAT}/{DATE}/{key}.wav",
                  durationSec=30, lineTimings=[{"s": i * 5.0, "e": i * 5.0 + 5.0} for i in range(4)],
                  **({"ownerUid": owner} if owner else {}))
    svc.blobs.put_text(f"podcasts/tldr/{CAT}/{DATE}/{key}.source.txt", "Source article about vector search and embeddings.")
    return podcast.make_pid(CAT, DATE, SID, variant)


@pytest.fixture
def ready(svc):
    svc.stt = FakeSTT()
    return seed(svc)


@pytest.fixture
def ann(client):
    return User(client, anonymous=True)


@pytest.fixture
def bo(client):
    return User(client, anonymous=True)


def learning_calls(svc, name):
    return [p for p in svc.llm.prompts if p[0] == name]


def mastery(svc, uid):
    return {r["name"]: r for r in profile.records(svc, uid)}


# --- the persistent podcast ----------------------------------------------------------------------------------------------

def test_podcast_is_served_by_its_persistent_id_with_transcript_and_audio(ready, client):
    got = client.get(f"/api/podcasts/{ready}").json()
    assert got["status"] == "ready" and got["podcastId"] == ready and got["audioUrl"].endswith(f"{SID}.wav")
    assert [ln["speaker"] for ln in got["lines"]] == ["Maya", "Theo", "Maya", "Theo"] and len(got["lineTimings"]) == 4
    assert client.get(f"/api/podcasts/{CAT}.{DATE}.nope-section").status_code == 404
    assert client.get("/api/podcasts/not-an-id").status_code == 404


def test_a_private_podcast_is_visible_only_to_its_owner(svc, client, ann, bo):
    pid = seed(svc, "u0123456789", owner=ann.uid)
    assert client.get(f"/api/podcasts/{pid}", headers=ann.h).json()["status"] == "ready"
    assert client.get(f"/api/podcasts/{pid}", headers=bo.h).status_code == 404
    assert client.get(f"/api/podcasts/{pid}").status_code == 404  # signed out
    assert bo.get(f"/podcasts/{pid}/chat").status_code == 404
    assert bo.post(f"/podcasts/{pid}/chat", {"clientId": "client-0001", "text": "hello there"}).status_code == 404


# --- typed and voice questions -------------------------------------------------------------------------------------------

def ask(user, pid, text, cid, mode="text"):
    return user.post(f"/podcasts/{pid}/chat", {"clientId": cid, "text": text, "inputMode": mode})


def test_typed_question_goes_to_openai_with_transcript_and_never_to_stt(ready, svc, ann):
    r = ask(ann, ready, "How does vector search rank results?", "client-0001")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "user" and body["user"]["inputMode"] == "text"
    assert body["assistant"]["content"] == "Here is an answer about: How does vector search rank results?"
    (_, system, prompt), = learning_calls(svc, "PodcastAnswer")
    assert "Vector search finds the nearest vectors" in prompt and "Source article about vector search" in prompt
    assert "<question>\nHow does vector search rank results?\n</question>" in prompt
    assert "never claim or imply the listener understands" in system.lower()
    assert svc.stt.calls == []  # text input is never sent through STT


def test_follow_up_questions_keep_the_conversation_and_history_survives_a_reload(ready, svc, ann):
    ask(ann, ready, "What is an embedding?", "client-0001")
    ask(ann, ready, "And how is it stored?", "client-0002")
    second = learning_calls(svc, "PodcastAnswer")[1][2]
    assert "Listener: What is an embedding?" in second and "Assistant: Here is an answer about: What is an embedding?" in second
    msgs = ann.get(f"/podcasts/{ready}/chat").json()["messages"]
    assert [(m["role"], m["content"]) for m in msgs] == [
        ("user", "What is an embedding?"), ("assistant", "Here is an answer about: What is an embedding?"),
        ("user", "And how is it stored?"), ("assistant", "Here is an answer about: And how is it stored?")]


def test_a_repeated_client_id_returns_the_stored_exchange_without_asking_the_model_again(ready, svc, ann):
    a = ask(ann, ready, "What is an embedding?", "client-0001").json()
    b = ask(ann, ready, "What is an embedding?", "client-0001").json()
    assert a["assistant"]["id"] == b["assistant"]["id"] and len(learning_calls(svc, "PodcastAnswer")) == 1
    assert len(ann.get(f"/podcasts/{ready}/chat").json()["messages"]) == 2


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_blank_questions_are_rejected_before_any_model_call(ready, svc, ann, text):
    assert ask(ann, ready, text, "client-0001").status_code == 422
    assert learning_calls(svc, "PodcastAnswer") == []


def test_model_failure_keeps_the_question_and_a_retry_only_runs_the_model(ready, svc, ann):
    svc.llm.unavailable = True
    assert ask(ann, ready, "What is an embedding?", "client-0001").status_code == 503
    svc.llm.unavailable = False
    assert ask(ann, ready, "What is an embedding?", "client-0001").status_code == 200
    msgs = ann.get(f"/podcasts/{ready}/chat").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]  # one question, not two


def test_voice_is_transcribed_by_stt_and_nothing_reaches_the_model_until_confirmed(ready, svc, ann):
    r = ann.client.post(f"/api/podcasts/{ready}/transcribe", headers={**ann.h, "Content-Type": "audio/webm;codecs=opus"}, content=b"\x1a\x45" * 600)
    assert r.status_code == 200 and r.json() == {"text": "What is a vector database"}
    assert svc.stt.calls == [(1200, "audio/webm")] and learning_calls(svc, "PodcastAnswer") == []
    assert ann.get(f"/podcasts/{ready}/chat").json()["messages"] == []  # the review step stores nothing
    sent = ask(ann, ready, r.json()["text"], "client-0001", "voice").json()  # the listener confirmed (or edited) the text
    assert sent["user"]["inputMode"] == "voice" and sent["user"]["content"] == "What is a vector database"
    assert learning_calls(svc, "PodcastAnswer")[0][2].count("What is a vector database") >= 1  # same Q&A service as typed


def test_failed_empty_or_invalid_transcription_never_triggers_the_model(ready, svc, ann):
    def send(audio=b"\x00" * 1000, mime="audio/webm"):
        return ann.client.post(f"/api/podcasts/{ready}/transcribe", headers={**ann.h, "Content-Type": mime}, content=audio)

    svc.stt.fail = True
    assert send().status_code == 503
    svc.stt.fail = False
    svc.stt.text = "   "
    assert send().status_code == 422 and send().json()["error"]["code"] == "empty_transcription"
    assert send(b"\x00" * 10).status_code == 422  # empty recording is not even sent to Gemini
    assert send(mime="text/plain").status_code == 415
    assert send(b"\x00" * (svc.settings.stt_max_bytes + 1)).status_code == 413
    assert learning_calls(svc, "PodcastAnswer") == []


def test_spoken_answer_uses_tts_and_text_survives_a_tts_failure_and_retry(ready, svc, ann):
    mid = ask(ann, ready, "What is an embedding?", "client-0001").json()["assistant"]["id"]
    svc.tts.fail_times = 1
    assert ann.post(f"/podcasts/{ready}/chat/{mid}/audio").status_code == 503
    assert ann.get(f"/podcasts/{ready}/chat").json()["messages"][1]["content"].startswith("Here is an answer")  # text still there
    ok = ann.post(f"/podcasts/{ready}/chat/{mid}/audio")
    assert ok.status_code == 200 and ok.json()["url"].endswith(f"{mid}.wav")
    assert svc.tts.calls[-1][0][0].text == "Here is an answer about: What is an embedding?"  # the answer text is what is spoken
    assert len(learning_calls(svc, "PodcastAnswer")) == 1  # retrying TTS never re-ran the language model


def test_chat_never_changes_mastery_and_only_records_an_unverified_signal(ready, svc, ann, monkeypatch):
    monkeypatch.setattr(profile, "apply_evidence", lambda *a, **k: pytest.fail("chat must not write mastery"))
    mid = ask(ann, ready, "What is an embedding?", "client-0001").json()["assistant"]["id"]
    ann.post(f"/podcasts/{ready}/chat/{mid}/audio")
    p = ann.get("/learning/profile").json()
    assert p["concepts"] == [] and p["revision"] == 0
    assert [s["text"] for s in p["unverifiedSignals"]] == ["What is an embedding?"]


# --- concepts and assessments --------------------------------------------------------------------------------------------

def test_concepts_come_from_the_script_and_extraction_failure_leaves_the_podcast_playable(ready, svc, client):
    svc.llm.invalid.add("PodcastConcepts")
    assert client.get(f"/api/podcasts/{ready}/concepts").json() == {"status": "none", "concepts": []}
    assert client.get(f"/api/podcasts/{ready}").json()["status"] == "ready"  # podcast unaffected
    svc.llm.invalid.clear()  # only this step is retried; nothing else is regenerated
    got = client.get(f"/api/podcasts/{ready}/concepts").json()
    assert [c["name"] for c in got["concepts"]] == ["vector search", "embeddings"]
    assert all(c["podcast_id"] == ready and c["evidence_spans"] for c in got["concepts"])
    assert "Maya: What is vector search?" in learning_calls(svc, "PodcastConcepts")[0][2]


def start(user, pid):
    r = user.post(f"/podcasts/{pid}/assessments", {})
    assert r.status_code == 200, r.text
    return r.json()


def answer_all(user, a, mapping: dict[str, str]):
    for qid, text in mapping.items():
        assert user.put(f"/assessments/{a['id']}/answers/{qid}", {"answer": text}).status_code == 200


def test_assessment_is_generated_from_podcast_concepts_and_hides_the_answers(ready, svc, ann):
    a = start(ann, ready)
    assert a["status"] == "created" and len(a["questions"]) == 3
    known = {"vector_search", "embedding"}
    assert all(set(q["conceptIds"]) <= known and q["conceptIds"] for q in a["questions"])
    assert "correct_index" not in str(a) and "rubric" not in str(a) and "Right idea" in str(a)  # options visible, key is not
    assert start(ann, ready)["id"] == a["id"]  # asking again never creates a duplicate


def test_assessment_failure_does_not_touch_the_podcast(ready, svc, ann):
    svc.llm.invalid.add("AssessmentSet")
    assert ann.post(f"/podcasts/{ready}/assessments", {}).status_code == 502
    assert ann.get(f"/podcasts/{ready}/assessments").json() == {"assessments": []}
    svc.llm.invalid.clear()
    assert start(ann, ready)["status"] == "created"


def test_answers_are_saved_before_evaluation_and_survive_a_reload(ready, ann):
    a = start(ann, ready)
    answer_all(ann, a, {"q1": RIGHT, "q2": RIGHT})
    again = ann.get(f"/assessments/{a['id']}").json()
    assert again["status"] == "in_progress" and again["answers"] == {"q1": RIGHT, "q2": RIGHT} and again["results"] is None
    assert ann.put(f"/assessments/{a['id']}/answers/q1", {"answer": "9"}).status_code == 422  # not an option
    assert ann.put(f"/assessments/{a['id']}/answers/q9", {"answer": "x"}).status_code == 404


def test_submit_needs_an_answer_and_blank_or_skipped_answers_do_not_count(ready, svc, ann):
    a = start(ann, ready)
    assert ann.post(f"/assessments/{a['id']}/submit").status_code == 422
    answer_all(ann, a, {"q1": "1"})
    done = ann.post(f"/assessments/{a['id']}/submit").json()
    st = {r["question_id"]: r["evaluation_status"] for r in done["results"]}
    assert st == {"q1": "demonstrated", "q2": "not_assessed", "q3": "not_assessed"}
    assert done["status"] == "completed" and done["score"] == pytest.approx(1 / 3, abs=0.01)  # skipped questions lower the score
    m = mastery(svc, ann.uid)
    assert set(m) == {"vector search"}  # skipped questions created no records for their concepts
    assert m["vector search"]["status"] == "learning"  # one correct answer is evidence, not mastery


def test_only_multiple_choice_questions_are_kept_and_grading_needs_no_language_model(ready, svc, ann):
    svc.llm.open_question_extra = True  # the generator also returns a free-text question: it must be dropped
    a = start(ann, ready)
    assert {q["type"] for q in a["questions"]} == {"mcq"} and all(len(q["options"]) == 4 for q in a["questions"])
    answer_all(ann, a, {"q1": RIGHT, "q2": WRONG})
    done = ann.post(f"/assessments/{a['id']}/submit").json()
    assert [r["evaluation_status"] for r in done["results"][:2]] == ["demonstrated", "incorrect"]
    assert learning_calls(svc, "AnswerEvaluations") == []  # exact grading, no model judgement involved


def test_two_different_demonstrated_answers_make_a_concept_understood_with_an_evidence_trail(ready, svc, ann):
    a = start(ann, ready)
    answer_all(ann, a, {"q1": RIGHT, "q3": RIGHT})  # q1 and q3 are two different questions on "vector search"
    done = ann.post(f"/assessments/{a['id']}/submit").json()
    assert [r["evaluation_status"] for r in done["results"] if r["answer"]] == ["demonstrated", "demonstrated"]
    rec = mastery(svc, ann.uid)["vector search"]
    assert rec["status"] == "understood" and rec["correctEvidenceCount"] == 2 and rec["lastPodcastId"] == ready
    trail = [d.to_dict() for d in svc.paths.mastery_evidence(ann.uid, "vector_search").order_by("at").stream()]
    assert [(t["statusBefore"], t["statusAfter"]) for t in trail] == [("unknown", "learning"), ("learning", "understood")]
    assert all(t["reason"] and t["assessmentId"] == a["id"] and t["answer"] for t in trail)


def test_submitting_twice_or_concurrently_never_counts_evidence_twice(ready, svc, ann):
    a = start(ann, ready)
    answer_all(ann, a, {"q1": RIGHT, "q3": RIGHT})
    first = ann.post(f"/assessments/{a['id']}/submit").json()
    rev, calls = profile.revision(svc, ann.uid), len(learning_calls(svc, "AnswerEvaluations"))
    second = ann.post(f"/assessments/{a['id']}/submit").json()
    assert second["results"] == first["results"] and profile.revision(svc, ann.uid) == rev
    assert len(learning_calls(svc, "AnswerEvaluations")) == calls
    assert mastery(svc, ann.uid)["vector search"]["evidenceCount"] == 2
    assert ann.put(f"/assessments/{a['id']}/answers/q2", {"answer": RIGHT}).status_code == 409  # locked after submit


def test_evidence_application_is_idempotent_per_evidence_id(svc, ann):
    ev = {"id": "a1__q1__c1", "assessment_id": "a1", "question_id": "q1", "podcast_id": "p", "status": "demonstrated", "score": 1.0}
    c = {"concept_id": "c1", "name": "c1"}
    assert profile.apply_evidence(svc, ann.uid, c, ev)["applied"] is True
    assert profile.apply_evidence(svc, ann.uid, c, ev)["applied"] is False
    assert mastery(svc, ann.uid)["c1"]["evidenceCount"] == 1 and profile.revision(svc, ann.uid) == 1


def test_evaluation_failure_keeps_answers_and_can_be_submitted_again(ready, svc, ann, monkeypatch):
    from app.ai.client import AiUnavailable
    from app.learning import assessment

    a = start(ann, ready)
    answer_all(ann, a, {"q2": RIGHT})
    real = assessment.evaluate_answers
    monkeypatch.setattr(assessment, "evaluate_answers", lambda *x, **k: (_ for _ in ()).throw(AiUnavailable("down")))
    assert ann.post(f"/assessments/{a['id']}/submit").status_code == 503
    state = ann.get(f"/assessments/{a['id']}").json()
    assert state["status"] == "failed" and state["answers"] == {"q2": RIGHT} and mastery(svc, ann.uid) == {}
    monkeypatch.setattr(assessment, "evaluate_answers", real)
    assert ann.post(f"/assessments/{a['id']}/submit").json()["status"] == "completed"


def test_contradicting_evidence_asks_for_review_without_erasing_history(ready, svc, ann):
    a = start(ann, ready)
    answer_all(ann, a, {"q1": RIGHT, "q3": RIGHT})
    ann.post(f"/assessments/{a['id']}/submit")
    retake = ann.post(f"/podcasts/{ready}/assessments", {"retake": True}).json()
    assert retake["id"] != a["id"] and retake["attempt"] == 2
    answer_all(ann, retake, {"q1": "0"})  # clearly wrong multiple-choice answer
    ann.post(f"/assessments/{retake['id']}/submit")
    rec = mastery(svc, ann.uid)["vector search"]
    assert rec["status"] == "needs_review" and rec["correctEvidenceCount"] == 2 and rec["incorrectEvidenceCount"] == 1
    attempts = ann.get(f"/podcasts/{ready}/assessments").json()["assessments"]
    assert [x["attempt"] for x in attempts] == [2, 1] and all(x["status"] == "completed" for x in attempts)  # history kept


def test_misconceptions_are_recorded_as_evidence_not_as_mastery(ready, svc, ann):
    a = start(ann, ready)
    answer_all(ann, a, {"q2": WRONG})
    ann.post(f"/assessments/{a['id']}/submit")
    rec = mastery(svc, ann.uid)["embeddings"]
    assert rec["status"] == "learning" and rec["misconceptions"] == ['Chose "Wrong idea" instead of "Right idea about embeddings"'] and rec["incorrectEvidenceCount"] == 1


def test_assessments_and_profiles_are_private_to_their_owner(ready, svc, ann, bo):
    a = start(ann, ready)
    answer_all(ann, a, {"q1": "1"})
    ann.post(f"/assessments/{a['id']}/submit")
    assert bo.get(f"/assessments/{a['id']}").status_code == 404
    assert bo.put(f"/assessments/{a['id']}/answers/q1", {"answer": "1"}).status_code == 404
    assert bo.post(f"/assessments/{a['id']}/submit").status_code == 404
    assert bo.get("/learning/profile").json()["concepts"] == []
    assert [c["name"] for c in ann.get("/learning/profile").json()["concepts"]] == ["vector search"]
    assert ann.client.get("/api/learning/profile").status_code == 401


# --- personalization from the verified history ----------------------------------------------------------------------------

def test_exposure_without_assessment_adds_nothing_to_the_profile(ready, svc, ann):
    ann.get(f"/podcasts/{ready}")
    ann.get(f"/podcasts/{ready}/concepts")
    assert ann.get("/learning/profile").json()["concepts"] == [] and not profile.has_evidence(svc, ann.uid)


def verified_user(svc, ann, ready):
    a = start(ann, ready)
    answer_all(ann, a, {"q1": RIGHT, "q3": RIGHT, "q2": WRONG})
    ann.post(f"/assessments/{a['id']}/submit")


def test_learning_context_is_read_from_the_database_and_split_into_verified_buckets(ready, svc, ann, bo):
    verified_user(svc, ann, ready)
    ctx = profile.build_context(svc, ann.uid, ["Vector search basics", "Reranking models"], "vector search nearest embeddings reranking")
    assert [c["name"] for c in ctx["understood"]] == ["vector search"]
    assert [c["name"] for c in ctx["reinforce"]] == ["embeddings"] and ctx["reinforce"][0]["misconception"].startswith('Chose "Wrong idea"')
    assert ctx["new"] == ["Reranking models"]  # never seen by this listener: new, not assumed known
    assert profile.build_context(svc, ann.uid, ["Kubernetes autoscaling"], "kubernetes pods autoscaling cluster") is None  # unrelated: no transfer
    assert profile.build_context(svc, bo.uid, ["Vector search basics"], "vector search") is None  # another user has no history


def test_next_podcast_is_private_and_its_prompts_carry_the_verified_profile(ready, svc, ann, bo, monkeypatch):
    from app.pipeline.enrich import EnrichResult
    from app.tldr import audio
    from tests.unit.test_tldr_podcast import ISSUE, TTS, ScriptedLLM, make_points

    verified_user(svc, ann, ready)
    monkeypatch.setattr(podcast.scraper, "get_issue", lambda date, category="tech": ISSUE)
    monkeypatch.setattr(podcast, "fetch_article", lambda url, **kw: EnrichResult("Article text. " * 30, False, "ok", []))
    monkeypatch.setattr(audio.time, "sleep", lambda s: None)
    queued = []
    svc.dispatcher = type("D", (), {"task": lambda self, name, payload, **kw: queued.append(payload)})()
    points = make_points(3)
    points[0]["topic"] = "Vector search basics"
    points[1]["topic"] = "Embeddings explained"
    points[2]["topic"] = "Reranking models"
    svc.llm = ScriptedLLM(points)
    svc.tts = TTS()  # speaks at a realistic rate, which the audio length check expects
    svc.settings.tldr_max_repairs = 1

    req = podcast.request(svc, CAT, DATE, SID, None, ann.uid)  # NO preference: still shaped by the verified history
    assert req["variant"].startswith("u") and req["podcastId"] == f"{PID}.{req['variant']}"
    (payload,) = queued
    assert payload["uid"] == ann.uid and payload["variant"] == req["variant"] and "preferences" not in payload
    assert podcast.generate(svc, CAT, DATE, SID, None, payload["uid"], payload["variant"]) == "ok"

    parts = [p for s, p in svc.llm.prompts if s == "ScriptPart"]
    assert parts and all("<learning_profile>" in p for p in parts)
    block = parts[0].split("<learning_profile>")[1].split("</learning_profile>")[0]
    assert "DEMONSTRATED UNDERSTANDING: vector search" in block and "NEEDS REINFORCEMENT: embeddings (recorded misconception: Chose \"Wrong idea\" instead of \"Right idea about embeddings\")" in block
    assert "NEW IN THIS SOURCE (never covered for this listener): Reranking models" in block

    pid = req["podcastId"]
    assert ann.client.get(f"/api/podcasts/{pid}", headers=ann.h).json()["status"] == "ready"
    assert ann.client.get(f"/api/podcasts/{pid}", headers=bo.h).status_code == 404  # private to the learner
    assert podcast.request(svc, CAT, DATE, SID, None, bo.uid)["podcastId"] == PID  # a user with no history gets the shared default
    default = ann.client.get(f"/api/tldr/podcast?category={CAT}&date={DATE}&section={SID}", headers=ann.h).json()
    assert default["title"] == "Vectors" and default["lines"] == LINES  # the shared default episode was not overwritten


def test_a_user_without_history_gets_the_default_flow_and_a_changed_profile_gets_a_new_episode(ready, svc, ann, monkeypatch):
    from tests.unit.test_tldr_podcast import ISSUE

    monkeypatch.setattr(podcast.scraper, "get_issue", lambda date, category="tech": ISSUE)
    svc.dispatcher = type("D", (), {"task": lambda self, name, payload, **kw: None})()
    assert podcast.request(svc, CAT, DATE, "quick", None, ann.uid) == {"status": "processing", "podcastId": f"{CAT}.{DATE}.quick"}
    prefs_only = podcast.request(svc, CAT, DATE, SID, "Focus on cost", ann.uid)["variant"]
    verified_user(svc, ann, ready)
    assert podcast.request(svc, CAT, DATE, SID, "Focus on cost", ann.uid)["variant"] != prefs_only  # new evidence -> a new episode


def test_two_requests_in_the_same_moment_start_one_generation_only(svc, monkeypatch):
    from tests.unit.test_tldr_podcast import ISSUE

    monkeypatch.setattr(podcast.scraper, "get_issue", lambda date, category="tech": ISSUE)
    queued = []
    svc.dispatcher = type("D", (), {"task": lambda self, name, payload, **kw: queued.append(payload)})()
    # Simulate the race: both requests passed the "not started yet" check before either saved its state.
    real_load = podcast._load
    monkeypatch.setattr(podcast, "_load", lambda *a, **k: {"status": "none"})
    first = podcast.request(svc, CAT, DATE, SID)
    second = podcast.request(svc, CAT, DATE, SID)
    monkeypatch.setattr(podcast, "_load", real_load)
    assert len(queued) == 1 and first["status"] == second["status"] == "processing"
    assert first["podcastId"] == second["podcastId"] == PID
    podcast._release(svc, CAT, DATE, SID)  # when generation ends the claim is freed, so a retry can start
    monkeypatch.setattr(podcast, "_load", lambda *a, **k: {"status": "none"})
    podcast.request(svc, CAT, DATE, SID)
    assert len(queued) == 2
