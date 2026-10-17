"""Server-side learner model writes. The browser can read these documents but never write them."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

from app.ai.client import AiUnavailable, cosine
from app.learner import model as lm
from app.logs import get_logger, log
from app.recommend.vectors import clean_vector
from app.services import Services
from app.store import now, slug, unvec, vec

logger = get_logger(__name__)


def concept_doc(name: str, embedding: list[float] | None, *, rating: str | None, source: str) -> dict[str, Any]:
    mastery = lm.mastery_from_self_rating(rating) if rating else lm.SELF_RATING_MASTERY["new"]
    return {
        "name": name,
        "embedding": vec(embedding),
        "selfRating": rating,
        "mastery": mastery,
        "stability": 0.0,
        "difficulty": 5.0,
        "reviewStep": -1,
        "lastReviewedAt": None,
        "nextDueAt": None,
        "evidenceCount": 0,
        "userCorrected": False,
        "sourceItemIds": [],
        "replayCount": 0,
        "createdFrom": source,
        "updatedAt": now(),
    }


def _nearest(svc: Services, uid: str, emb: list[float], existing: list[dict[str, Any]]) -> tuple[str | None, float]:
    """Nearest concept by cosine. Uses Firestore vector search when the index exists; otherwise
    falls back to comparing in code (a user has at most a few hundred concepts)."""
    try:
        q = svc.paths.concepts(uid).find_nearest(
            vector_field="embedding",
            query_vector=Vector(emb),
            distance_measure=DistanceMeasure.COSINE,
            limit=1,
            distance_result_field="dist",
        )
        for snap in q.stream():
            d = snap.to_dict().get("dist")
            if d is not None:
                return snap.id, 1.0 - float(d)
    except Exception as exc:
        log(logger, logging.DEBUG, "vector_query_fallback", error=type(exc).__name__)
    best: tuple[str | None, float] = (None, 0.0)
    for c in existing:
        sim = cosine(emb, unvec(c.get("embedding")))
        if sim > best[1]:
            best = (c["id"], sim)
    return best


def load_concepts(svc: Services, uid: str) -> list[dict[str, Any]]:
    return [{"id": s.id, **s.to_dict()} for s in svc.paths.concepts(uid).stream()]


def match_or_create(
    svc: Services, uid: str, names: list[str], item_ids: list[str], *, source: str = "briefing"
) -> list[str]:
    """Maps concept names to the user's concept documents, creating new ones when nothing is close."""
    names = [n.strip() for n in names if n and n.strip()]
    if not names:
        return []
    existing = load_concepts(svc, uid)
    by_slug = {slug(c["name"]): c["id"] for c in existing}
    try:
        embs = [clean_vector(e, svc.settings.embed_dim) for e in svc.embedder.embed(names, task="query")]
    except AiUnavailable:
        embs = [None] * len(names)
    out: list[str] = []
    for name, emb in zip(names, embs, strict=True):
        cid = by_slug.get(slug(name))
        if not cid and emb:
            near, sim = _nearest(svc, uid, emb, existing)
            if near and sim >= svc.settings.concept_match_cosine_threshold:
                cid = near
        ref = svc.paths.concepts(uid).document(cid or f"c_{slug(name)}")
        if not cid:
            ref.set(concept_doc(name, emb, rating=None, source=source))
            existing.append({"id": ref.id, "name": name, "embedding": emb})
            by_slug[slug(name)] = ref.id
        if item_ids:
            ref.update({"sourceItemIds": firestore.ArrayUnion(item_ids[:5])})
        if ref.id not in out:
            out.append(ref.id)
    return out


def save_level_check(svc: Services, uid: str, ratings: list[tuple[str, str]]) -> list[str]:
    names = [n for n, _ in ratings]
    try:
        embs = [clean_vector(e, svc.settings.embed_dim) for e in svc.embedder.embed(names, task="query")]
    except AiUnavailable:
        embs = [None] * len(names)
    ids = []
    batch = svc.db.batch()
    for (name, rating), emb in zip(ratings, embs, strict=True):
        ref = svc.paths.concepts(uid).document(f"c_{slug(name)}")
        snap = ref.get()
        if snap.exists and (snap.to_dict() or {}).get("userCorrected"):
            ids.append(ref.id)
            continue
        doc = concept_doc(name, emb, rating=rating, source="onboarding")
        if snap.exists:
            prev = snap.to_dict() or {}
            doc["sourceItemIds"] = prev.get("sourceItemIds", [])
            doc["evidenceCount"] = prev.get("evidenceCount", 0)
        batch.set(ref, doc)
        ids.append(ref.id)
    batch.commit()
    return ids


def apply_quiz_result(svc: Services, uid: str, concept_id: str, grade: lm.Grade, at: datetime | None = None) -> dict[str, Any]:
    at = at or now()
    ref = svc.paths.concepts(uid).document(concept_id)
    transaction = svc.db.transaction()

    @firestore.transactional
    def txn(tx) -> dict[str, Any]:
        data = ref.get(transaction=tx).to_dict() or {}
        m = float(data.get("mastery", 0.1))
        new_m = lm.mastery_after_quiz(m, grade)
        step, due = lm.next_review(int(data.get("reviewStep", -1)), grade, at)
        update = {
            "mastery": new_m,
            "reviewStep": step,
            "nextDueAt": due,
            "lastReviewedAt": at,
            "stability": float(lm.REVIEW_INTERVALS_DAYS[step]),
            "evidenceCount": int(data.get("evidenceCount", 0)) + 1,
            "updatedAt": at,
        }
        tx.update(ref, update)
        return {"before": m, "after": new_m, "nextDueAt": due}

    return txn(transaction)


def apply_simpler(svc: Services, uid: str, concept_ids: list[str]) -> None:
    for cid in concept_ids:
        ref = svc.paths.concepts(uid).document(cid)
        snap = ref.get()
        if snap.exists:
            m = float((snap.to_dict() or {}).get("mastery", 0.1))
            ref.update({"mastery": lm.mastery_after_simpler(m), "updatedAt": now()})


def user_correction(svc: Services, uid: str, concept_id: str, level: str) -> dict[str, Any]:
    ref = svc.paths.concepts(uid).document(concept_id)
    if not ref.get().exists:
        return {}
    at = now()
    update = {
        "mastery": lm.mastery_from_self_rating(level),  # type: ignore[arg-type]
        "selfRating": level,
        "userCorrected": True,
        "reviewStep": 0,
        "nextDueAt": at + timedelta(days=lm.REVIEW_INTERVALS_DAYS[0]),
        "updatedAt": at,
    }
    ref.update(update)
    return update


# --- events -> profile (7.4) ---------------------------------------------------------------

def apply_events(svc: Services, uid: str, events: list[dict[str, Any]]) -> None:
    """Updates topic weights, source trust, concept evidence and mute suggestions from events."""
    at = now()
    profile_ref = svc.paths.profile(uid)
    profile = profile_ref.get().to_dict() or {}
    weights: dict[str, Any] = dict(profile.get("topicWeights") or {})
    less_counts: dict[str, int] = dict(profile.get("lessLikeThisCounts") or {})
    mutes: list[str] = list(profile.get("muteTopics") or [])
    trust_deltas: dict[str, float] = {}
    concept_evidence: dict[str, int] = {}
    replays: dict[str, int] = {}
    changed = False

    item_cache: dict[str, dict[str, Any]] = {}

    def item(iid: str | None) -> dict[str, Any]:
        if not iid:
            return {}
        if iid not in item_cache:
            item_cache[iid] = svc.paths.item(iid).get().to_dict() or {}
        return item_cache[iid]

    for ev in events:
        etype = ev["type"]
        it = item(ev.get("itemId"))
        topics = [t for t in [it.get("category"), *(it.get("entities") or [])] if t]
        payload = ev.get("payload") or {}
        if etype == "skip" and float(payload.get("listenedSec", 999)) > 10:
            continue  # only quick skips are a (weak) negative signal
        delta = lm.EVENT_TOPIC_DELTAS.get(etype)
        if delta and topics:
            for t in topics:
                key = lm.topic_key(t)
                weights[key] = lm.apply_topic_delta(weights.get(key), delta, at)
            changed = True
        tdelta = lm.EVENT_TRUST_DELTAS.get(etype)
        if tdelta and it.get("senderId"):
            trust_deltas[it["senderId"]] = trust_deltas.get(it["senderId"], 0.0) + tdelta
        if etype == "lessLikeThis":
            for t in topics[:1]:  # the item's category is the topic being rejected
                key = lm.topic_key(t)
                less_counts[key] = less_counts.get(key, 0) + 1
                if less_counts[key] >= lm.LESS_LIKE_THIS_MUTE_AFTER and t not in mutes:
                    mutes.append(t)
            changed = True
        if etype in ("ask", "deeper") and ev.get("conceptId"):
            concept_evidence[ev["conceptId"]] = concept_evidence.get(ev["conceptId"], 0) + 1
        if etype == "replay" and ev.get("conceptId"):
            replays[ev["conceptId"]] = replays.get(ev["conceptId"], 0) + 1

    if changed:
        profile_ref.set(
            {"topicWeights": weights, "lessLikeThisCounts": less_counts, "muteTopics": mutes, "updatedAt": at},
            merge=True,
        )
    for sender_id, d in trust_deltas.items():
        ref = svc.paths.newsletters(uid).document(sender_id)
        snap = ref.get()
        cur = float((snap.to_dict() or {}).get("trust", 0.5)) if snap.exists else 0.5
        ref.set({"trust": lm.clamp_trust(cur + d)}, merge=True)
    for cid, n in concept_evidence.items():
        ref = svc.paths.concepts(uid).document(cid)
        if ref.get().exists:
            ref.update({"evidenceCount": firestore.Increment(n), "updatedAt": at})
    for cid, n in replays.items():
        ref = svc.paths.concepts(uid).document(cid)
        if ref.get().exists:
            ref.update({"replayCount": firestore.Increment(n)})
