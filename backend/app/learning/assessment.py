"""Assessments of a podcast's concepts.

users/{uid}/assessments/{aid}     status, questions (no answers), answers, results, score, timestamps
assessmentSecrets/{aid}           server only: correct option and rubric per question (like quizSecrets)

Lifecycle:  created -> in_progress (first saved answer) -> submitted -> evaluating -> completed | failed
Answers are saved one by one before anything is evaluated. Submitting is idempotent: a completed assessment returns its
stored results, and every piece of evidence has a deterministic id (see profile.apply_evidence), so a repeated or
concurrent submit cannot double count. A failed evaluation leaves the saved answers intact and can be submitted again.
Previous attempts are never overwritten; a retake is a new assessment document.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta
from typing import Any

from google.cloud import firestore

from app.ai import prompts
from app.ai.client import AiOutputInvalid, AiUnavailable
from app.ai.schemas import AnswerEvaluations, AssessmentSet
from app.errors import ApiError, conflict, not_found
from app.learning import policy, profile
from app.logs import get_logger, log
from app.services import Services
from app.store import now

logger = get_logger(__name__)

OPEN_STATES = ("created", "in_progress", "submitted", "evaluating", "failed")
EVALUATING_STALE = timedelta(minutes=3)  # an "evaluating" mark older than this is a dead worker
MAX_ANSWER_CHARS = 2000


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _public(aid: str, a: dict[str, Any]) -> dict[str, Any]:
    return {"id": aid, "podcastId": a["podcastId"], "status": a["status"], "questions": a["questions"],
            "answers": {q: v["answer"] for q, v in (a.get("answers") or {}).items()},
            "results": a.get("results"), "score": a.get("score"), "message": a.get("message"),
            "attempt": a.get("attempt", 1), "createdAt": _iso(a.get("createdAt")), "completedAt": _iso(a.get("completedAt"))}


def _load(svc: Services, uid: str, aid: str) -> dict[str, Any]:
    snap = svc.paths.assessments(uid).document(aid).get()
    if not snap.exists:
        raise not_found("Assessment")  # also the answer for another user's id: the path is scoped to the caller
    return snap.to_dict() or {}


def list_for(svc: Services, uid: str, pid: str) -> list[dict[str, Any]]:
    rows = [(s.id, s.to_dict() or {}) for s in svc.paths.assessments(uid).where("podcastId", "==", pid).stream()]
    rows.sort(key=lambda r: r[1].get("createdAt") or now(), reverse=True)
    return [_public(i, a) for i, a in rows]


def get(svc: Services, uid: str, aid: str) -> dict[str, Any]:
    return _public(aid, _load(svc, uid, aid))


# --- creation ------------------------------------------------------------------------------------------------------------

def _validated(out: AssessmentSet, concepts: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Public questions and the server-only secrets; drops malformed questions rather than guessing."""
    by_name = {c["name"].strip().lower(): c for c in concepts}
    questions, secrets = [], {}
    for q in out.questions:
        cs = [by_name[n.strip().lower()] for n in q.concept_names if n.strip().lower() in by_name]
        if not cs:
            continue  # not mapped to a concept the podcast covers
        # Multiple choice only: exact grading, no model judgement. Anything else the model returns is dropped.
        if q.type != "mcq" or not (3 <= len(q.options) <= 4) or q.correct_index is None or not 0 <= q.correct_index < len(q.options):
            continue
        qid = f"q{len(questions) + 1}"
        questions.append({"id": qid, "type": q.type, "prompt": q.prompt, "options": q.options if q.type == "mcq" else [],
                          "conceptIds": [c["concept_id"] for c in cs], "conceptNames": [c["name"] for c in cs]})
        secrets[qid] = {"correctIndex": q.correct_index, "rubric": q.rubric, "modelAnswer": q.model_answer}
    return questions, secrets


def create(svc: Services, uid: str, pid: str, concepts: list[dict[str, Any]], lines: list[dict[str, Any]], *, retake: bool = False) -> dict[str, Any]:
    """The open assessment of this podcast, or a new one. Calling it again returns the same open assessment
    (no duplicates); a new attempt starts only after the last one is completed and `retake` is set."""
    existing = list_for(svc, uid, pid)
    open_ones = [a for a in existing if a["status"] in OPEN_STATES]
    if open_ones:
        return open_ones[0]
    if existing and not retake:
        return existing[0]
    if not concepts:
        raise ApiError(409, "no_concepts", "This podcast has no extracted concepts yet to test you on.")
    words = sum(len(ln["text"].split()) for ln in lines)
    major = sum(1 for c in concepts if c["importance"] == "major")
    n = policy.questions_for(words, major)
    listing = "\n".join(f"- {c['name']} ({c['importance']}): {c['description']}" for c in concepts)
    plan = "\n".join(f'Question {i}: test the concept "{name}"' for i, name in enumerate(policy.question_targets(concepts, n), start=1))
    script = "\n".join(f"{i}. {ln['speaker']}: {ln['text']}" for i, ln in enumerate(lines))[:12000]
    try:
        out = svc.llm.generate_json("flash", prompts.ASSESSMENT_SYSTEM,
                                    f"Write {n} questions, each on a different angle of its concept:\n{plan}\n"
                                    f"CONCEPTS:\n{listing}\n\n<script>\n{script}\n</script>", AssessmentSet,
                                    temperature=0.4, max_output_tokens=3500)
    except AiUnavailable:
        raise ApiError(503, "ai_unavailable", "The assessment could not be prepared right now. Try again shortly.")
    except AiOutputInvalid:
        raise ApiError(502, "ai_invalid", "The assessment could not be prepared. Try again.")
    questions, secrets = _validated(out, concepts)
    if not questions:
        raise ApiError(502, "ai_invalid", "The assessment could not be prepared. Try again.")
    aid = f"a_{uuid.uuid4().hex[:18]}"
    at = now()
    doc = {"podcastId": pid, "status": "created", "questions": questions, "answers": {}, "attempt": len(existing) + 1,
           "createdAt": at, "updatedAt": at, "results": None, "score": None}
    svc.paths.assessment_secret(aid).set({"uid": uid, "secrets": secrets})
    svc.paths.assessments(uid).document(aid).set(doc)
    log(logger, logging.INFO, "assessment_created", assessmentId=aid, podcastId=pid, questions=len(questions))
    return _public(aid, doc)


# --- answers -------------------------------------------------------------------------------------------------------------

def save_answer(svc: Services, uid: str, aid: str, qid: str, answer: str) -> dict[str, Any]:
    """Persists one answer before evaluation. Saving the same text again is a no-op; blank text clears the answer."""
    ref = svc.paths.assessments(uid).document(aid)
    tx = svc.db.transaction()

    @firestore.transactional
    def txn(t) -> dict[str, Any]:
        snap = ref.get(transaction=t)
        if not snap.exists:
            raise not_found("Assessment")
        a = snap.to_dict() or {}
        if a["status"] not in ("created", "in_progress"):
            raise conflict("This assessment is already submitted")
        q = next((x for x in a["questions"] if x["id"] == qid), None)
        if q is None:
            raise not_found("Question")
        text = (answer or "").strip()[:MAX_ANSWER_CHARS]
        if q["type"] == "mcq" and text and (not text.isdigit() or int(text) >= len(q["options"])):
            raise ApiError(422, "invalid", "Choose one of the listed options")
        answers = dict(a.get("answers") or {})
        if text:
            answers[qid] = {"answer": text, "savedAt": now()}
        else:
            answers.pop(qid, None)
        t.update(ref, {"answers": answers, "status": "in_progress" if answers else "created", "updatedAt": now()})
        return {"status": "in_progress" if answers else "created", "answers": {k: v["answer"] for k, v in answers.items()}}

    return txn(tx)


# --- evaluation ----------------------------------------------------------------------------------------------------------

def _claim(svc: Services, uid: str, aid: str) -> dict[str, Any] | None:
    """Marks the assessment as evaluating. Returns the stored result when it is already completed."""
    ref = svc.paths.assessments(uid).document(aid)
    tx = svc.db.transaction()

    @firestore.transactional
    def txn(t) -> dict[str, Any] | None:
        snap = ref.get(transaction=t)
        if not snap.exists:
            raise not_found("Assessment")
        a = snap.to_dict() or {}
        if a["status"] == "completed":
            return _public(aid, a)
        if a["status"] == "evaluating" and a.get("evaluatingSince") and now() - a["evaluatingSince"] < EVALUATING_STALE:
            raise conflict("This assessment is being evaluated")
        if not a.get("answers"):
            raise ApiError(422, "no_answers", "Answer at least one question before you submit.")
        t.update(ref, {"status": "evaluating", "evaluatingSince": now(), "submittedAt": a.get("submittedAt") or now()})
        return None

    return txn(tx)


def evaluate_answers(svc: Services, questions: list[dict[str, Any]], secrets: dict[str, Any], answers: dict[str, Any]) -> list[dict[str, Any]]:
    """One result per question: deterministic for multiple choice, rubric-graded by the LLM for free text. The model only
    supplies a score, a confidence and an evidence quote; `policy.classify` decides what the answer shows."""
    results: dict[str, dict[str, Any]] = {}
    open_items = []
    for q in questions:
        a = (answers.get(q["id"]) or {}).get("answer", "")
        base = {"question_id": q["id"], "concept_ids": q["conceptIds"], "answer": a, "evidence": "", "misconception": None,
                "correctness_score": 0.0, "feedback": "", "evaluated_at": now().isoformat()}
        sec = secrets.get(q["id"], {})
        if not a.strip():
            results[q["id"]] = {**base, "evaluation_status": "not_assessed", "feedback": "You skipped this question, so it does not count."}
        elif q["type"] == "mcq":
            ok = a.strip() == str(sec.get("correctIndex"))
            status = policy.classify(1.0 if ok else 0.0, 1.0, a, objective=True)
            right = q["options"][sec["correctIndex"]]
            chosen = q["options"][int(a)]
            results[q["id"]] = {**base, "answer": chosen, "evaluation_status": status, "correctness_score": 1.0 if ok else 0.0,
                                "feedback": "Correct." if ok else f"Not quite. The correct answer is: {right}", "evidence": chosen,
                                "misconception": None if ok else f"Chose \"{chosen}\" instead of \"{right}\""[:300]}
        else:
            open_items.append((q, a, sec, base))
    if open_items:
        payload = [{"question_id": q["id"], "question": q["prompt"], "rubric": sec.get("rubric", []),
                    "model_answer": sec.get("modelAnswer", "")} for q, _, sec, _ in open_items]
        answers_xml = "\n".join(f"<answer id=\"{q['id']}\">\n{a}\n</answer>" for q, a, _, _ in open_items)
        out = svc.llm.generate_json("flash", prompts.EVALUATE_SYSTEM, f"QUESTIONS:\n{json.dumps(payload)}\n\nLEARNER ANSWERS:\n{answers_xml}",
                                    AnswerEvaluations, temperature=0.0, max_output_tokens=3000)
        by_id = {e.question_id: e for e in out.evaluations}
        for q, a, _sec, base in open_items:
            ev = by_id.get(q["id"])
            if ev is None:  # the evaluator skipped it: no evidence either way
                results[q["id"]] = {**base, "evaluation_status": "insufficient", "feedback": "This answer could not be evaluated. Try again."}
                continue
            status = policy.classify(ev.correctness_score, ev.confidence, a, ev.evidence)
            results[q["id"]] = {**base, "evaluation_status": status, "correctness_score": round(ev.correctness_score, 2),
                                "feedback": ev.feedback, "evidence": ev.evidence if status != "insufficient" else "",
                                "misconception": ev.misconception, "confidence": round(ev.confidence, 2)}
    return [results[q["id"]] for q in questions]


def submit(svc: Services, uid: str, aid: str, concepts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Evaluates every saved answer once, records the evidence and updates the verified profile. Safe to call again."""
    done = _claim(svc, uid, aid)
    if done is not None:
        return done
    ref = svc.paths.assessments(uid).document(aid)
    a = ref.get().to_dict() or {}
    secrets = (svc.paths.assessment_secret(aid).get().to_dict() or {}).get("secrets", {})
    try:
        results = a.get("pendingResults") or evaluate_answers(svc, a["questions"], secrets, a.get("answers") or {})
    except (AiUnavailable, AiOutputInvalid) as exc:
        ref.update({"status": "failed", "message": "Your answers are saved, but they could not be evaluated yet. Submit again.", "updatedAt": now()})
        log(logger, logging.WARNING, "assessment_eval_failed", assessmentId=aid, error=type(exc).__name__)
        raise ApiError(503, "ai_unavailable", "Your answers are saved, but they could not be evaluated yet. Try again shortly.")
    # Graded results are stored before the profile is touched, so a retry after a crash re-uses them (no second, different
    # grading); each piece of evidence is also idempotent by id.
    ref.update({"pendingResults": results})
    for r in results:
        q = next(x for x in a["questions"] if x["id"] == r["question_id"])
        for cid, cname in zip(q["conceptIds"], q["conceptNames"], strict=True):
            r.setdefault("mastery", {})[cid] = None
            if r["evaluation_status"] == "not_assessed":
                continue
            concept = concepts.get(cid, {"concept_id": cid, "name": cname})
            res = profile.apply_evidence(svc, uid, {"concept_id": cid, "name": concept.get("name", cname)}, {
                "id": f"{aid}__{r['question_id']}__{cid}", "assessment_id": aid, "question_id": r["question_id"],
                "podcast_id": a["podcastId"], "status": r["evaluation_status"], "score": r["correctness_score"],
                "feedback": r["feedback"], "answer": r["answer"], "quote": r["evidence"], "misconception": r["misconception"]})
            r["mastery"][cid] = {"after": res.get("after"), "reason": res.get("reason")} if res.get("applied") else {"after": None, "reason": "already recorded"}
    assessed = [r for r in results if r["evaluation_status"] != "not_assessed"]
    score = round(sum(r["correctness_score"] for r in assessed if r["evaluation_status"] != "insufficient") / len(results), 3)
    ref.update({"pendingResults": firestore.DELETE_FIELD, "status": "completed", "results": results, "score": score, "completedAt": now(), "updatedAt": now(), "message": None})
    log(logger, logging.INFO, "assessment_completed", assessmentId=aid, questions=len(results),
        statuses={s: sum(1 for r in results if r["evaluation_status"] == s) for s in
                  ("demonstrated", "partial", "incorrect", "insufficient", "not_assessed")})
    return get(svc, uid, aid)
