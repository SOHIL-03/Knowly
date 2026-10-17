"""The verified learning profile: one mastery record per user and concept, changed ONLY by assessment evidence.

users/{uid}/verifiedMastery/{conceptKey}             the current record (status, counts, recent evidence)
users/{uid}/verifiedMastery/{conceptKey}/evidence/{assessmentId__questionId__conceptKey}   the permanent trail
users/{uid}/learning/summary                         {rev}: bumped by every applied piece of evidence
users/{uid}/learningSignals/{id}                     UNVERIFIED signals (questions asked); never read as mastery

This module is separate from the chat service on purpose: nothing in app/learning/chat.py can call `apply_evidence`.
Writes go through a transaction and the evidence document id is deterministic, so re-submitting the same answers
cannot count twice.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from google.cloud import firestore

from app.learning import policy
from app.logs import get_logger, log
from app.services import Services
from app.store import now

logger = get_logger(__name__)

MAX_PER_BUCKET = 8
MAX_RECORDS = 400
STOP = set("the a an and or of to in for on with is are be by as at it this that from how what why can your you we our vs".split())


def apply_evidence(svc: Services, uid: str, concept: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Records one piece of evidence for one concept and moves its status by the policy. Idempotent per evidence id.

    concept: {concept_id, name}; evidence: {id, assessment_id, question_id, podcast_id, status, score, feedback, answer, quote,
    misconception}. Returns {"applied": bool, "before", "after", "reason"}."""
    cid = concept["concept_id"]
    rec_ref = svc.paths.mastery(uid).document(cid)
    ev_ref = svc.paths.mastery_evidence(uid, cid).document(evidence["id"])
    summary_ref = svc.paths.learning_summary(uid)
    tx = svc.db.transaction()

    @firestore.transactional
    def txn(t) -> dict[str, Any]:
        if ev_ref.get(transaction=t).exists:
            return {"applied": False}
        data = rec_ref.get(transaction=t).to_dict() or {}
        summary = summary_ref.get(transaction=t).to_dict() or {}
        status = evidence["status"]
        before = data.get("status", "unknown")
        qkey = f"{evidence['assessment_id']}:{evidence['question_id']}"
        after, reason = policy.next_status(
            policy.Record(before, list(data.get("recent") or []), list(data.get("demonstratedKeys") or [])), status, qkey)
        at = now()
        counts = {k: int(data.get(k, 0)) for k in ("correctEvidenceCount", "partialEvidenceCount", "incorrectEvidenceCount",
                                                    "insufficientEvidenceCount", "evidenceCount")}
        if status == "demonstrated":
            counts["correctEvidenceCount"] += 1
        elif status == "partial":
            counts["partialEvidenceCount"] += 1
        elif status == "incorrect":
            counts["incorrectEvidenceCount"] += 1
        elif status == "insufficient":
            counts["insufficientEvidenceCount"] += 1
        counting = status in policy.COUNTING
        if counting:
            counts["evidenceCount"] += 1
        recent = ([*(data.get("recent") or []), status][-policy.RECENT_WINDOW:]) if counting else list(data.get("recent") or [])
        keys = list(data.get("demonstratedKeys") or [])
        if status == "demonstrated" and qkey not in keys:
            keys.append(qkey)
        miscs = list(data.get("misconceptions") or [])
        if evidence.get("misconception") and status in ("partial", "incorrect"):
            miscs = [*miscs, evidence["misconception"]][-policy.MAX_MISCONCEPTIONS:]
        doc = {
            "conceptId": cid, "name": data.get("name") or concept["name"], "status": after,
            "masteryScore": policy.mastery_score(counts["correctEvidenceCount"], counts["partialEvidenceCount"], counts["incorrectEvidenceCount"]),
            **counts, "recent": recent, "demonstratedKeys": keys[-20:], "misconceptions": miscs,
            "lastAssessedAt": at, "lastPodcastId": evidence["podcast_id"], "updatedAt": at,
            "lastDemonstratedAt": at if status == "demonstrated" else data.get("lastDemonstratedAt"),
            "createdAt": data.get("createdAt") or at,
        }
        t.set(rec_ref, doc)
        t.set(ev_ref, {
            "assessmentId": evidence["assessment_id"], "questionId": evidence["question_id"], "podcastId": evidence["podcast_id"],
            "status": status, "score": evidence.get("score"), "answer": (evidence.get("answer") or "")[:2000],
            "quote": evidence.get("quote", ""), "feedback": evidence.get("feedback", ""), "misconception": evidence.get("misconception"),
            "statusBefore": before, "statusAfter": after, "reason": reason, "at": at,
        })
        t.set(summary_ref, {"rev": int(summary.get("rev", 0)) + 1, "updatedAt": at}, merge=True)
        return {"applied": True, "before": before, "after": after, "reason": reason}

    return txn(tx)


def revision(svc: Services, uid: str) -> int:
    return int((svc.paths.learning_summary(uid).get().to_dict() or {}).get("rev", 0))


def has_evidence(svc: Services, uid: str) -> bool:
    """True once any assessment answer has been recorded for the user (a verified profile exists)."""
    return revision(svc, uid) > 0


def records(svc: Services, uid: str) -> list[dict[str, Any]]:
    return [{"id": s.id, **(s.to_dict() or {})} for s in svc.paths.mastery(uid).limit(MAX_RECORDS).stream()]


def public_record(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("conceptId", "name", "status", "masteryScore", "evidenceCount", "correctEvidenceCount", "partialEvidenceCount",
            "incorrectEvidenceCount", "insufficientEvidenceCount", "lastAssessedAt", "lastDemonstratedAt", "lastPodcastId",
            "updatedAt", "misconceptions")
    out = {k: r.get(k) for k in keep}
    for k in ("lastAssessedAt", "lastDemonstratedAt", "updatedAt"):
        out[k] = out[k].isoformat() if out[k] else None
    return out


def get_profile(svc: Services, uid: str) -> dict[str, Any]:
    recs = sorted(records(svc, uid), key=lambda r: (r.get("name") or "").lower())
    counts = {s: sum(1 for r in recs if r.get("status") == s) for s in ("unknown", "learning", "understood", "needs_review")}
    signals = [s.to_dict() or {} for s in svc.paths.learning_signals(uid).order_by("createdAt", direction=firestore.Query.DESCENDING).limit(10).stream()]
    return {"concepts": [public_record(r) for r in recs], "counts": counts, "revision": revision(svc, uid),
            "unverifiedSignals": [{"type": s.get("type"), "text": s.get("text"), "podcastId": s.get("podcastId"),
                                   "createdAt": s["createdAt"].isoformat() if s.get("createdAt") else None} for s in signals],
            "note": "Status comes only from assessment answers. Questions you ask are listed as unverified signals."}


def record_signal(svc: Services, uid: str, podcast_id: str, text: str) -> None:
    """An unverified hint that the listener is curious about something. Never a mastery change."""
    svc.paths.learning_signals(uid).document().set({"type": "question", "podcastId": podcast_id, "text": text[:300], "createdAt": now()})


# --- context for the next podcast ------------------------------------------------------------------------------------

def tokens(text: str) -> set[str]:
    return {w.rstrip("s") if len(w) > 3 else w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1}


def is_relevant(name: str, haystack: set[str]) -> bool:
    """A recorded concept is relevant to a topic when most of its own words occur in the topic's text."""
    want = tokens(name)
    return bool(want) and len(want & haystack) / len(want) >= 0.6


def build_context(svc: Services, uid: str, topics: list[str], source_text: str) -> dict[str, Any] | None:
    """Bounded, topic-relevant learning context read from the database (never from the client).
    topics: the topics the new podcast will cover; source_text: the material (topics + key points) used for relevance.
    Returns None when the user has no verified history that relates to the topic."""
    recs = records(svc, uid)
    if not recs:
        return None
    hay = tokens(source_text) | tokens(" ".join(topics))
    rel = [r for r in recs if is_relevant(r.get("name", ""), hay)]
    if not rel:
        return None
    understood = [{"name": r["name"]} for r in rel if r["status"] == "understood"][:MAX_PER_BUCKET]
    reinforce = [{"name": r["name"], "status": r["status"], "misconception": (r.get("misconceptions") or [None])[-1]}
                 for r in rel if r["status"] in ("learning", "needs_review")][:MAX_PER_BUCKET]
    unknown = [r["name"] for r in rel if r["status"] == "unknown"][:MAX_PER_BUCKET]
    known = {r["name"] for r in rel}
    new = [t for t in dict.fromkeys(topics) if not any(is_relevant(k, tokens(t)) or is_relevant(t, tokens(k)) for k in known)][:MAX_PER_BUCKET]
    evidence = [f"{r['name']}: {r['correctEvidenceCount']} demonstrated, {r['partialEvidenceCount']} partial, "
                f"{r['incorrectEvidenceCount']} incorrect answer(s)" for r in rel if r.get("evidenceCount")][:MAX_PER_BUCKET]
    log(logger, logging.INFO, "learning_context", understood=len(understood), reinforce=len(reinforce), unknown=len(unknown), new=len(new))
    return {"understood": understood, "reinforce": reinforce, "unknown": unknown, "new": new, "evidence": evidence}
