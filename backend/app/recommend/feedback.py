"""Deterministic feedback learning. No language model interprets feedback.

* Save, Ask, Go deeper and Less like this move the learned weight of the facet that matched the story
  (users/{uid}/profile/main.facetPrefs[facetId].delta). The declared facet weight is never rewritten:
  effective weight = clamp(declared weight + decayed learned delta, min, max).
* "Knew this already" only raises familiarity of the story's topics (profile.familiarity), which lowers
  the novelty signal for similar stories. It does not reduce how relevant the topic is.
* Learned values halve every `rec_feedback_half_life_days` (default 30) and are bounded.
* Every submission is a record in users/{uid}/feedback/{idempotency key}, written in the same
  transaction as the profile update, so a repeated submission (a client retry, a second tap) cannot
  count twice. The key is one per briefing, story and action.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from google.cloud import firestore

from app.learner.model import topic_key
from app.logs import get_logger, log
from app.recommend.facets import Facet
from app.recommend.settings import RecConfig
from app.services import Services
from app.store import now, stable_id

logger = get_logger(__name__)
FEEDBACK_ACTIONS = ("save", "ask", "deeper", "lessLikeThis", "knewThis")
MAX_TOPICS = 6


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def decay(value: float, updated_at: datetime | None, at: datetime, half_life_days: float) -> float:
    if updated_at is None:
        return value
    age_days = max(0.0, (at - updated_at).total_seconds() / 86400)
    return value * math.pow(0.5, age_days / half_life_days)


def _entry(prefs: dict[str, Any], key: str, field: str) -> tuple[float, datetime | None]:
    e = prefs.get(key)
    if not isinstance(e, dict):
        return 0.0, None
    try:
        value = float(e.get(field, 0.0))
    except (TypeError, ValueError):
        return 0.0, None
    return (value if math.isfinite(value) else 0.0), (e.get("updatedAt") if isinstance(e.get("updatedAt"), datetime) else None)


def learned_delta(prefs: dict[str, Any], facet_id: str, at: datetime, cfg: RecConfig) -> float:
    value, updated = _entry(prefs, facet_id, "delta")
    return clamp(decay(value, updated, at, cfg.feedback_half_life_days), -cfg.feedback_max_delta, cfg.feedback_max_delta)


def effective_weight(facet: Facet, prefs: dict[str, Any], at: datetime, cfg: RecConfig) -> float:
    """Declared weight plus the decayed learned adjustment, kept within the configured bounds."""
    return clamp(facet.weight + learned_delta(prefs, facet.id, at, cfg), cfg.weight_min, cfg.weight_max)


def step_for(action: str, cfg: RecConfig) -> float:
    return cfg.feedback_steps.get(action, 0.0)


def update_facet_pref(prefs: dict[str, Any], facet_id: str, step: float, at: datetime, cfg: RecConfig) -> dict[str, Any]:
    """prefs with the facet's learned delta moved by `step` (after decaying what was there), bounded."""
    out = dict(prefs)
    new = clamp(learned_delta(prefs, facet_id, at, cfg) + step, -cfg.feedback_max_delta, cfg.feedback_max_delta)
    out[facet_id] = {"delta": new, "updatedAt": at}
    return out


def familiarity_for(topics: list[str], familiarity: dict[str, Any], at: datetime, cfg: RecConfig) -> float:
    """Highest decayed familiarity among the story's topics, in [0, cap]. 0 when nothing was marked known."""
    best = 0.0
    for t in topics:
        value, updated = _entry(familiarity, topic_key(t), "w")
        best = max(best, decay(value, updated, at, cfg.feedback_half_life_days))
    return clamp(best, 0.0, cfg.familiarity_cap)


def update_familiarity(familiarity: dict[str, Any], topics: list[str], at: datetime, cfg: RecConfig) -> dict[str, Any]:
    out = dict(familiarity)
    for t in topics[:MAX_TOPICS]:
        key = topic_key(t)
        if not key:
            continue
        value, updated = _entry(familiarity, key, "w")
        out[key] = {"w": clamp(decay(value, updated, at, cfg.feedback_half_life_days) + cfg.knew_step, 0.0, cfg.familiarity_cap),
                    "updatedAt": at}
    return out


def idempotency_key(briefing_id: str | None, item_id: str, action: str) -> str:
    return stable_id(briefing_id or "-", item_id, action, prefix="fb_")


def story_topics(item: dict[str, Any]) -> list[str]:
    return [t for t in [item.get("category"), *(item.get("entities") or [])] if isinstance(t, str) and t][:MAX_TOPICS]


def matched_facet(briefing: dict[str, Any], item_id: str) -> str | None:
    for r in briefing.get("ranked") or []:
        if isinstance(r, dict) and r.get("itemId") == item_id:
            return r.get("facetId") or None
    return None


def apply_feedback(svc: Services, uid: str, events: list[dict[str, Any]], cfg: RecConfig | None = None) -> dict[str, int]:
    """Records feedback events and applies their deterministic preference updates. Never raises: a
    failure here must not break the request that reported the event."""
    if not svc.settings.rec_enabled:
        return {}
    cfg = cfg or RecConfig.from_settings(svc.settings)
    counts = {"applied": 0, "duplicate": 0}
    briefings: dict[str, dict[str, Any]] = {}
    for ev in events:
        action, item_id = ev.get("type"), ev.get("itemId")
        if action not in FEEDBACK_ACTIONS or not item_id:
            continue
        try:
            bid = ev.get("briefingId")
            if bid and bid not in briefings:
                briefings[bid] = svc.paths.briefing(uid, bid).get().to_dict() or {}
            item = svc.paths.item(item_id).get().to_dict() or {}
            if _record(svc, uid, str(item_id), str(action), bid, ev.get("segId"), item, matched_facet(briefings.get(bid or "", {}), item_id), cfg):
                counts["applied"] += 1
            else:
                counts["duplicate"] += 1
        except Exception as exc:  # noqa: BLE001 - feedback learning is best effort
            log(logger, logging.WARNING, "rec_feedback_failed", uid=uid, action=action, error=type(exc).__name__)
    if counts["applied"] or counts["duplicate"]:
        log(logger, logging.INFO, "rec_feedback", uid=uid, **counts)
    return counts


def _record(svc: Services, uid: str, item_id: str, action: str, bid: str | None, seg_id: str | None, item: dict[str, Any],
            facet_id: str | None, cfg: RecConfig) -> bool:
    key = idempotency_key(bid, item_id, action)
    fb_ref = svc.paths.feedback(uid).document(key)
    profile_ref = svc.paths.profile(uid)
    at = now()
    topics = story_topics(item)
    record = {"userId": uid, "storyId": item_id, "briefingId": bid, "segId": seg_id, "action": action,
              "matchedFacetId": facet_id, "topic": topics[0] if topics else None, "createdAt": at, "idempotencyKey": key}

    @firestore.transactional
    def txn(tx) -> bool:
        if fb_ref.get(transaction=tx).exists:
            return False
        prof = profile_ref.get(transaction=tx).to_dict() or {}
        update: dict[str, Any] = {}
        if action == "knewThis":
            if topics:
                update["familiarity"] = update_familiarity(dict(prof.get("familiarity") or {}), topics, at, cfg)
        elif facet_id and step_for(action, cfg):
            update["facetPrefs"] = update_facet_pref(dict(prof.get("facetPrefs") or {}), facet_id, step_for(action, cfg), at, cfg)
        tx.set(fb_ref, record)
        if update:
            tx.set(profile_ref, update, merge=True)
        return True

    return txn(svc.db.transaction())
