"""Checkpoint actions on the live path: Ask, Quiz me, Apply, Simpler, Go deeper, Save
(Architecture v3 sections 4.2-4.4 and 9.4-9.6)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.ai import prompts
from app.ai.client import AiOutputInvalid, AiUnavailable
from app.ai.schemas import ApplyResult, AskClassification, PlannedSegment, QuizGrade, QuizQuestion, Rephrased
from app.errors import ApiError, conflict, not_found
from app.learner import model as lm
from app.learner import service as learner
from app.logs import get_logger, log
from app.pipeline import personal
from app.pipeline.enrich import fetch_article
from app.pipeline.validate import extract_citations
from app.services import Services
from app.store import local_date, now

logger = get_logger(__name__)


# --- shared helpers ------------------------------------------------------------------------

def check_limit(svc: Services, uid: str, kind: str, limit: int) -> None:
    ref = svc.paths.usage(uid, local_date("UTC"))
    transaction = svc.db.transaction()

    @firestore.transactional
    def txn(tx) -> int:
        data = ref.get(transaction=tx).to_dict() or {}
        used = int(data.get(kind, 0))
        if used >= limit:
            return -1
        tx.set(ref, {kind: used + 1}, merge=True)
        return used + 1

    if txn(transaction) < 0:
        raise ApiError(429, "daily_limit", f"Daily limit reached for {kind}. It resets tomorrow.")


def load_segment(svc: Services, uid: str, bid: str, sid: str) -> dict[str, Any]:
    snap = svc.paths.segment(uid, bid, sid).get()
    if not snap.exists:
        raise not_found("Segment")
    return {"id": sid, **snap.to_dict()}


def load_items(svc: Services, ids: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for iid in ids:
        snap = svc.paths.item(iid).get()
        if snap.exists:
            out[iid] = {"id": iid, **snap.to_dict()}
    return out


def sources_for(svc: Services, seg: dict[str, Any], items: dict[str, dict[str, Any]], *, with_articles: bool) -> list[dict[str, Any]]:
    out = []
    for iid in seg.get("itemIds", []):
        it = items.get(iid)
        if not it:
            continue
        src = {"id": iid, "newsletter": it.get("senderName"), "headline": it["headline"], "note": it["blurb"],
               "link_only": bool(it.get("linkOnly"))}
        if with_articles and it.get("fullTextPath") and not it.get("linkOnly"):
            src["article"] = (svc.blobs.get_text(it["fullTextPath"]) or "")[:6000]
        out.append(src)
    for line in seg.get("script") or []:
        if line.get("item_ids"):
            out.append({"script_line": line.get("text"), "ids": line.get("item_ids")})
    return out


def citation_details(items: dict[str, dict[str, Any]], ids: list[str]) -> list[dict[str, Any]]:
    return [{"itemId": i, "newsletter": items[i].get("senderName"), "headline": items[i]["headline"],
             "url": items[i].get("url")} for i in ids if i in items]


def record_events(svc: Services, uid: str, events: list[dict[str, Any]]) -> None:
    batch = svc.db.batch()
    for ev in events:
        batch.set(svc.paths.events(uid).document(), {**ev, "ts": now()})
    batch.commit()
    learner.apply_events(svc, uid, events)


# --- Ask (4.3, 9.4) ---------------------------------------------------------------------------

def ask_stream(svc: Services, uid: str, bid: str, sid: str, question: str) -> Iterator[dict[str, Any]]:
    check_limit(svc, uid, "ask", svc.settings.daily_limit_ask)
    seg = load_segment(svc, uid, bid, sid)
    others = [{"id": s.id, "title": (s.to_dict() or {}).get("title")} for s in svc.paths.segments(uid, bid).stream() if s.id != sid]
    try:
        cls = svc.llm.generate_json(
            "flash", prompts.ASK_CLASSIFY_SYSTEM,
            json.dumps({"question": question, "current_segment": seg.get("title"), "other_segments": others}),
            AskClassification, temperature=0.0,
        )
    except AiOutputInvalid:
        cls = AskClassification(scope="segment")

    if cls.unsafe:
        answer = "I can't help with that. Ask me about the stories in this briefing instead."
        yield {"type": "final", "answer": answer, "citations": [], "scope": "refused", "grounded": False}
        log(logger, logging.INFO, "ask_refused", uid=uid)
        return

    target = seg
    if cls.scope == "earlier" and cls.earlier_segment_id and any(o["id"] == cls.earlier_segment_id for o in others):
        target = load_segment(svc, uid, bid, cls.earlier_segment_id)
    items = load_items(svc, target.get("itemIds", []))
    answer, citations, grounded, scope = "", [], False, cls.scope

    if cls.scope != "outside":
        sources = sources_for(svc, target, items, with_articles=True)
        prompt = f"SOURCES:\n<source>\n{json.dumps(sources)}\n</source>\n\nQUESTION: {question}"
        buf = ""
        emitted = False
        for chunk in svc.llm.stream_text("flash", prompts.ASK_SYSTEM, prompt, temperature=0.2):
            buf += chunk
            if not emitted and len(buf.strip()) < len(prompts.NOT_FOUND) and prompts.NOT_FOUND.startswith(buf.strip()):
                continue  # hold back until we know it is not NOT_FOUND
            if buf.strip().startswith(prompts.NOT_FOUND):
                break
            yield {"type": "delta", "text": buf if not emitted else chunk}
            emitted = True
        if buf.strip().startswith(prompts.NOT_FOUND) or not buf.strip():
            scope = "outside"
            answer = prompts.NOT_IN_SOURCES + " "
        else:
            cleaned, valid, invalid = extract_citations(buf, set(items))
            if valid:
                answer, citations, grounded = cleaned, valid, True
            else:
                # No valid citation: we cannot show it as grounded (Rule 1).
                log(logger, logging.INFO, "ask_ungrounded", invalidCitations=invalid[:5])
                scope = "outside"
                answer = "I couldn't find support for that in your newsletters. "

    if scope == "outside":
        yield {"type": "reset", "text": answer}
        general = ""
        for chunk in svc.llm.stream_text("flash", prompts.ASK_GENERAL_SYSTEM, f"QUESTION: {question}", temperature=0.3):
            general += chunk
            yield {"type": "delta", "text": chunk if general != chunk else prompts.GENERAL_LABEL + chunk}
        answer = f"{answer}{prompts.GENERAL_LABEL}{general.strip()}".strip()

    qa_ref = svc.paths.qa(uid).document()
    cite = citation_details(items, citations)
    qa_ref.set({"briefingId": bid, "segId": sid, "question": question[:1000], "answer": answer, "citations": cite,
                "scope": scope, "kind": "ask", "grounded": grounded, "ts": now()})
    concept_id = (seg.get("conceptIds") or [None])[0]
    record_events(svc, uid, [{"type": "ask", "briefingId": bid, "segId": sid, "itemId": (citations or seg.get("itemIds") or [None])[0],
                              "conceptId": concept_id, "payload": {"scope": scope}}])
    yield {"type": "final", "answer": answer, "citations": cite, "scope": scope, "grounded": grounded, "qaId": qa_ref.id}


# --- Quiz (4.4, 9.5) ----------------------------------------------------------------------------

def pick_concept(svc: Services, uid: str, seg: dict[str, Any]) -> dict[str, Any] | None:
    concepts = []
    for cid in seg.get("conceptIds") or []:
        snap = svc.paths.concepts(uid).document(cid).get()
        if snap.exists:
            concepts.append({"id": cid, **snap.to_dict()})
    if not concepts:
        return None
    at = now()

    def key(c: dict[str, Any]) -> tuple[int, float]:
        due = c.get("nextDueAt")
        return (0 if due and due <= at else 1, float(c.get("mastery", 0.1)))

    return sorted(concepts, key=key)[0]


def quiz_start(svc: Services, uid: str, bid: str, sid: str) -> dict[str, Any]:
    seg = load_segment(svc, uid, bid, sid)
    items = load_items(svc, seg.get("itemIds", []))
    concept = pick_concept(svc, uid, seg)
    if concept is None:
        from app.ai.schemas import ConceptCandidates

        names = svc.llm.generate_json(
            "flash", prompts.CONCEPTS_SYSTEM, f"<source>\n{json.dumps(sources_for(svc, seg, items, with_articles=False))}\n</source>",
            ConceptCandidates, temperature=0.1,
        ).concepts[:1]
        ids = learner.match_or_create(svc, uid, names, seg.get("itemIds", []))
        if not ids:
            raise ApiError(422, "no_concept", "There is nothing in this segment to quiz you on.")
        svc.paths.segment(uid, bid, sid).update({"conceptIds": firestore.ArrayUnion(ids)})
        snap = svc.paths.concepts(uid).document(ids[0]).get()
        concept = {"id": ids[0], **snap.to_dict()}
    q = svc.llm.generate_json(
        "flash", prompts.QUIZ_SYSTEM,
        f"CONCEPT: {concept['name']}\nSOURCES:\n<source>\n{json.dumps(sources_for(svc, seg, items, with_articles=False))}\n</source>",
        QuizQuestion, temperature=0.3,
    )
    src_ids = [i for i in q.source_item_ids if i in items] or list(items)[:1]
    qid = uuid.uuid4().hex[:20]
    svc.paths.quizzes(uid).document(qid).set({
        "briefingId": bid, "segId": sid, "conceptId": concept["id"], "conceptName": concept["name"],
        "question": q.question, "status": "open", "hintShown": False, "ts": now(),
    })
    # The rubric, model answer and hint stay on the server (security rules deny all client access).
    svc.paths.quiz_secret(qid).set({"uid": uid, "rubric": q.rubric, "modelAnswer": q.model_answer, "hint": q.hint,
                                   "sourceItemIds": src_ids})
    return {"quizId": qid, "question": q.question, "conceptName": concept["name"], "conceptId": concept["id"]}


def quiz_hint(svc: Services, uid: str, qid: str) -> dict[str, Any]:
    quiz_ref = svc.paths.quizzes(uid).document(qid)
    if not quiz_ref.get().exists:
        raise not_found("Quiz")
    secret = svc.paths.quiz_secret(qid).get().to_dict() or {}
    if secret.get("uid") != uid:
        raise not_found("Quiz")
    quiz_ref.update({"hintShown": True})
    return {"hint": secret.get("hint")}


def quiz_answer(svc: Services, uid: str, qid: str, answer: str | None, dont_know: bool) -> dict[str, Any]:
    quiz_ref = svc.paths.quizzes(uid).document(qid)
    quiz = quiz_ref.get().to_dict()
    secret = svc.paths.quiz_secret(qid).get().to_dict() or {}
    if not quiz or secret.get("uid") != uid:
        raise not_found("Quiz")
    if quiz.get("status") == "graded":
        raise conflict("This question was already answered")
    items = load_items(svc, secret.get("sourceItemIds", []))
    if dont_know or not (answer or "").strip():
        grade = QuizGrade(grade="not_yet", feedback=secret["modelAnswer"], missing_points=secret.get("rubric", []))
    else:
        grade = svc.llm.generate_json(
            "flash", prompts.GRADE_SYSTEM,
            json.dumps({"question": quiz["question"], "rubric": secret["rubric"], "model_answer": secret["modelAnswer"],
                        "listener_answer": answer[:2000]}),
            QuizGrade, temperature=0.0,
        )
    change = learner.apply_quiz_result(svc, uid, quiz["conceptId"], grade.grade)
    quiz_ref.update({"status": "graded", "userAnswer": (answer or "")[:2000], "dontKnow": dont_know, "grade": grade.grade,
                     "feedback": grade.feedback, "missingPoints": grade.missing_points, "correctAnswer": secret["modelAnswer"],
                     "masteryBefore": change["before"], "masteryAfter": change["after"], "gradedAt": now()})
    record_events(svc, uid, [{"type": "quiz", "briefingId": quiz["briefingId"], "segId": quiz["segId"],
                              "conceptId": quiz["conceptId"], "payload": {"grade": grade.grade}}])
    _update_quiz_score(svc, uid, quiz["briefingId"])
    return {
        "grade": grade.grade, "feedback": grade.feedback, "correctAnswer": secret["modelAnswer"],
        "missingPoints": grade.missing_points, "citations": citation_details(items, list(items)),
        "masteryBefore": change["before"], "masteryAfter": change["after"], "nextDueAt": change["nextDueAt"].isoformat(),
        "conceptName": quiz["conceptName"],
    }


def _update_quiz_score(svc: Services, uid: str, bid: str) -> None:
    graded = [q.to_dict() for q in svc.paths.quizzes(uid).where(filter=FieldFilter("briefingId", "==", bid)).stream()]
    graded = [g for g in graded if g.get("status") == "graded"]
    if graded:
        points = sum(1.0 if g["grade"] == "correct" else 0.5 if g["grade"] == "partly" else 0.0 for g in graded)
        svc.paths.briefing(uid, bid).update({"recap.quizScore": {"points": points, "total": len(graded)}})


# --- Apply (9.6) ----------------------------------------------------------------------------------

def apply_to_project(svc: Services, uid: str, bid: str, sid: str) -> dict[str, Any]:
    seg = load_segment(svc, uid, bid, sid)
    items = load_items(svc, seg.get("itemIds", []))
    project = (svc.paths.profile(uid).get().to_dict() or {}).get("project") or {}
    if not (project.get("text") or "").strip():
        raise ApiError(422, "no_project", "Add your project in Settings first.")
    res = svc.llm.generate_json(
        "pro", prompts.APPLY_SYSTEM,
        f"PROJECT:\n{project.get('text')}\n{project.get('summary') or ''}\n\nSOURCES:\n<source>\n"
        f"{json.dumps(sources_for(svc, seg, items, with_articles=True))}\n</source>",
        ApplyResult, temperature=0.4,
    )
    suggestions = []
    for s in res.suggestions:
        ids = [i for i in s.item_ids if i in items]
        if ids:  # each suggestion must cite a real item of this segment
            suggestions.append({**s.model_dump(), "item_ids": ids, "citations": citation_details(items, ids)})
    if not suggestions:
        raise ApiError(502, "ungrounded", "Could not produce suggestions grounded in this segment. Try again.")
    svc.paths.qa(uid).document().set({"briefingId": bid, "segId": sid, "question": "Apply to my project",
                                      "answer": json.dumps(suggestions), "suggestions": suggestions, "kind": "apply",
                                      "scope": "segment", "citations": citation_details(items, list({i for s in suggestions for i in s["item_ids"]})),
                                      "ts": now()})
    record_events(svc, uid, [{"type": "apply", "briefingId": bid, "segId": sid, "itemId": suggestions[0]["item_ids"][0]}])
    return {"suggestions": suggestions}


# --- Rephrase ----------------------------------------------------------------------------------------

def rephrase(svc: Services, uid: str, bid: str, sid: str, text: str, direction: str) -> dict[str, Any]:
    seg = load_segment(svc, uid, bid, sid)
    items = load_items(svc, seg.get("itemIds", []))
    level = "simpler, for a beginner, with one analogy" if direction == "simpler" else "more technical, for an experienced engineer"
    res = svc.llm.generate_json(
        "flash", prompts.REPHRASE_SYSTEM,
        f"LEVEL: {level}\nPASSAGE: {text[:3000]}\nSOURCES:\n<source>\n{json.dumps(sources_for(svc, seg, items, with_articles=False))}\n</source>",
        Rephrased, temperature=0.3,
    )
    ids = [i for i in res.item_ids if i in items]
    if direction == "simpler":
        learner.apply_simpler(svc, uid, seg.get("conceptIds") or [])
    record_events(svc, uid, [{"type": "simpler" if direction == "simpler" else "moreTechnical", "briefingId": bid,
                              "segId": sid, "conceptId": (seg.get("conceptIds") or [None])[0]}])
    return {"text": res.text, "citations": citation_details(items, ids)}


# --- Save (Library) ----------------------------------------------------------------------------

def save_item(svc: Services, uid: str, item_id: str, note: str | None, source: str, bid: str | None, sid: str | None) -> dict[str, Any]:
    snap = svc.paths.item(item_id).get()
    if not snap.exists:
        raise not_found("Item")
    it = snap.to_dict() or {}
    lib_ref = svc.paths.library(uid).document(item_id)
    existing = lib_ref.get()
    at = now()
    doc = {
        "itemId": item_id, "url": it.get("url"), "headline": it["headline"], "blurb": it["blurb"],
        "senderId": it.get("senderId"), "senderName": it.get("senderName"), "note": (note or "")[:2000],
        "tags": [t for t in [it.get("category"), *(it.get("entities") or [])] if t][:10],
        "issueDate": it.get("issueDate"), "source": source, "briefingId": bid, "segId": sid,
    }
    if not existing.exists:
        doc.update({"savedAt": at, "reviewDueAt": lm.saved_item_first_review(at), "reviewStep": 0})
        if bid:
            svc.paths.briefing(uid, bid).update({"recap.savedCount": firestore.Increment(1)})
        record_events(svc, uid, [{"type": "save", "briefingId": bid, "segId": sid, "itemId": item_id}])
    lib_ref.set(doc, merge=True)
    return {"id": item_id, **{k: v for k, v in doc.items() if k not in ("savedAt", "reviewDueAt")}}


# --- Go deeper ----------------------------------------------------------------------------------

def request_deeper(svc: Services, uid: str, bid: str, sid: str, item_id: str | None) -> dict[str, Any]:
    seg = load_segment(svc, uid, bid, sid)
    items = load_items(svc, seg.get("itemIds", []))
    if item_id and item_id not in items:
        raise not_found("Item in this segment")
    candidates = [items[item_id]] if item_id else [items[i] for i in seg.get("itemIds", []) if i in items]
    target = next((it for it in candidates if not it.get("linkOnly") and it.get("fullTextPath")), None)
    if target is None:
        it = candidates[0] if candidates else None
        return {"status": "link_only",
                "message": "Only the newsletter's short note is available for this story (the article could not be "
                           "opened, for example because of a paywall), so there is nothing deeper to add.",
                "url": it.get("url") if it else None}
    check_limit(svc, uid, "deeper", svc.settings.daily_limit_deeper)
    new_sid = f"d{uuid.uuid4().hex[:6]}"
    svc.paths.segment(uid, bid, new_sid).set({
        "order": float(seg["order"]) + 0.5, "type": "deeper", "title": f"Deeper: {target['headline'][:60]}",
        "whyChosen": "You asked to go deeper", "itemIds": [target["id"]], "conceptIds": seg.get("conceptIds", []),
        "status": "generating", "audioStatus": "pending", "transcriptStatus": "pending", "removedByUser": False,
        "parentSegId": sid, "createdAt": now(), "durationSec": 150, "targetSeconds": 150,
    })
    _renumber(svc, uid, bid)
    svc.dispatcher.task("deeper-segment", {"uid": uid, "briefingId": bid, "segId": sid, "itemId": target["id"],
                                           "newSegId": new_sid})
    record_events(svc, uid, [{"type": "deeper", "briefingId": bid, "segId": sid, "itemId": target["id"],
                              "conceptId": (seg.get("conceptIds") or [None])[0]}])
    return {"status": "generating", "segId": new_sid}


def build_deeper_segment(svc: Services, uid: str, bid: str, sid: str, item_id: str, new_sid: str) -> None:
    ref = svc.paths.segment(uid, bid, new_sid)
    it = load_items(svc, [item_id]).get(item_id)
    if not it:
        ref.update({"status": "failed"})
        return
    for link in (it.get("innerLinks") or [])[:2]:
        res = fetch_article(link, timeout=svc.settings.enrich_timeout_seconds, max_chars=6000, client=svc.http)
        if res.text:
            it.setdefault("_inner", []).append({"url": link, "text": res.text})
    concepts = {c["name"]: float(c.get("mastery", 0.1)) for c in learner.load_concepts(svc, uid)}
    profile = svc.paths.profile(uid).get().to_dict() or {}
    goals = ", ".join(g.get("topic", "") for g in profile.get("goals") or [])
    plan = PlannedSegment(type="learning", title=f"Deeper: {it['headline'][:60]}", why_chosen=f"Goals: {goals}"[:160],
                          item_ids=[item_id], target_seconds=150)
    full = {**it, "_excerptChars": 12000}
    user = svc.paths.user(uid).get().to_dict() or {}
    style = ((user.get("prefs") or {}).get("style")) or "twoHost"
    try:
        script, mode, problems, faith = personal.write_script(
            svc, plan, {item_id: full}, concepts, style, (profile.get("project") or {}).get("text", ""), None,
            seg_type="deeper",
        )
    except AiUnavailable:
        ref.update({"status": "failed", "audioStatus": "failed", "audioMessage": "The AI service was unavailable."})
        return
    ref.update({
        "status": "ready",
        **personal.script_doc(script, personal.hosts_for(svc)),
        "style": style,
        "scriptMode": mode,
        "scriptProblems": problems[:5],
        "faithfulness": faith["verdict"],
        "unsupportedClaims": faith["unsupportedClaims"],
    })
    save_item(svc, uid, item_id, None, "deeper", bid, sid)
    svc.dispatcher.task("segment-audio", {"uid": uid, "briefingId": bid, "segId": new_sid})


def _renumber(svc: Services, uid: str, bid: str) -> None:
    segs = sorted(svc.paths.segments(uid, bid).stream(), key=lambda s: float((s.to_dict() or {}).get("order", 0)))
    batch = svc.db.batch()
    for i, s in enumerate(segs):
        batch.update(s.reference, {"order": i + 1})
    batch.commit()


def reorder(svc: Services, uid: str, bid: str, order: list[str], removed: list[str]) -> None:
    segs = {s.id: s for s in svc.paths.segments(uid, bid).stream()}
    unknown = [x for x in [*order, *removed] if x not in segs]
    if unknown:
        raise not_found(f"Segments {unknown}")
    batch = svc.db.batch()
    pos = {sid: i for i, sid in enumerate(order)}
    rest = [sid for sid in sorted(segs, key=lambda x: float((segs[x].to_dict() or {}).get("order", 0))) if sid not in pos]
    for i, sid in enumerate([*order, *rest]):
        batch.update(segs[sid].reference, {"order": i + 1, "removedByUser": sid in removed})
    batch.commit()
    events = [{"type": "cardRemoved", "briefingId": bid, "segId": sid, "itemId": ((segs[sid].to_dict() or {}).get("itemIds") or [None])[0]}
              for sid in removed if not (segs[sid].to_dict() or {}).get("removedByUser")]
    if events:
        record_events(svc, uid, events)



# --- Spoken answers: Gemini single-speaker TTS ------------------------------------------------

GRADE_WORDS = {"correct": "That's right.", "partly": "Partly right.", "not_yet": "Not quite yet."}


def answer_audio(svc: Services, uid: str, qa_id: str) -> dict[str, Any]:
    """Speaks a stored answer (Ask or Apply). Only server-stored text is spoken."""
    from app.ai.prompts import ANSWER_VOICE_STYLE
    from app.pipeline.audio_stage import speak_text
    from app.pipeline.validate import CITATION_RE

    qa = svc.paths.qa(uid).document(qa_id).get().to_dict()
    if not qa:
        raise not_found("Answer")
    if qa.get("kind") == "apply":
        parts = [f"Idea {i + 1}: {s['idea']}. {s['why_it_might_help']} Check first: {s['check_first']}"
                 for i, s in enumerate(qa.get("suggestions") or [])]
        text = " ".join(parts)
    else:
        text = CITATION_RE.sub("", qa.get("answer") or "").replace(" .", ".").strip()
    if not text:
        raise ApiError(422, "nothing_to_say", "This answer has no text to read out.")
    check_limit(svc, uid, "tts", svc.settings.daily_limit_tts)
    return speak_text(svc, uid, f"audio/{uid}/qa/{qa_id}.wav", text, ANSWER_VOICE_STYLE)


def quiz_audio(svc: Services, uid: str, quiz_id: str, part: str) -> dict[str, Any]:
    """Speaks the quiz question, or the feedback once the answer has been graded (ask before telling)."""
    from app.ai.prompts import ANSWER_VOICE_STYLE
    from app.pipeline.audio_stage import speak_text

    quiz = svc.paths.quizzes(uid).document(quiz_id).get().to_dict()
    if not quiz:
        raise not_found("Quiz")
    if part == "question":
        text = quiz["question"]
    else:
        if quiz.get("status") != "graded":
            raise conflict("Answer the question first")
        text = f"{GRADE_WORDS.get(quiz['grade'], '')} {quiz.get('feedback', '')}"
        if quiz["grade"] != "correct" and quiz.get("correctAnswer"):
            text += f" The answer: {quiz['correctAnswer']}"
    check_limit(svc, uid, "tts", svc.settings.daily_limit_tts)
    return speak_text(svc, uid, f"audio/{uid}/quiz/{quiz_id}-{part}.wav", text.strip(), ANSWER_VOICE_STYLE)
