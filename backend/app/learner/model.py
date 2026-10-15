"""Learner model rules (Architecture v3 sections 7.2 - 7.4).

Pure functions only; Firestore writes happen in app.learner.service.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Literal

SelfRating = Literal["new", "heard", "use"]
Grade = Literal["correct", "partly", "not_yet"]

SELF_RATING_MASTERY: dict[str, float] = {"new": 0.10, "heard": 0.40, "use": 0.75}
# MVP review intervals (user flow E2): 1, 3, 7 days.
REVIEW_INTERVALS_DAYS = [1, 3, 7]
TOPIC_HALF_LIFE_DAYS = 30.0
MAX_REVIEWS_PER_BRIEFING = 2


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def mastery_from_self_rating(rating: SelfRating) -> float:
    return SELF_RATING_MASTERY[rating]


def mastery_after_quiz(m: float, grade: Grade) -> float:
    if grade == "correct":
        return clamp(m + 0.25 * (1 - m))
    if grade == "partly":
        return clamp(m + 0.10 * (1 - m))
    return clamp(m * 0.70)


def mastery_after_simpler(m: float) -> float:
    return min(m, 0.35)


def next_review(step: int, grade: Grade, at: datetime) -> tuple[int, datetime]:
    """Returns (new step index, nextDueAt).

    correct -> the interval grows along 1, 3, 7 days (stays at 7);
    partly or not yet -> review again in 1 day and restart the ladder.
    """
    if grade == "correct":
        new_step = min(step + 1, len(REVIEW_INTERVALS_DAYS) - 1) if step >= 0 else 0
        days = REVIEW_INTERVALS_DAYS[new_step]
        return new_step, at + timedelta(days=days)
    return 0, at + timedelta(days=1)


def saved_item_first_review(at: datetime) -> datetime:
    return at + timedelta(days=REVIEW_INTERVALS_DAYS[0])


def explanation_band(m: float) -> Literal["define", "remind", "skip"]:
    """How mastery changes the audio: <0.35 define with analogy, 0.35-0.7 short reminder, >0.7 skip basics."""
    if m < 0.35:
        return "define"
    if m <= 0.7:
        return "remind"
    return "skip"


# --- Topic weights with decay (7.4) -------------------------------------------------------

EVENT_TOPIC_DELTAS: dict[str, float] = {
    "skip": -0.05,  # only when skipped within 10 seconds (checked by the caller)
    "cardRemoved": -0.05,
    "lessLikeThis": -0.30,
    "ask": 0.15,
    "deeper": 0.15,
    "apply": 0.10,
    "save": 0.05,
    "openSource": 0.05,
}
EVENT_TRUST_DELTAS: dict[str, float] = {"lessLikeThis": -0.10, "save": 0.05, "openSource": 0.05, "skip": -0.02}
LESS_LIKE_THIS_MUTE_AFTER = 3


def decayed(weight: float, updated_at: datetime | None, at: datetime) -> float:
    if updated_at is None:
        return weight
    age_days = max(0.0, (at - updated_at).total_seconds() / 86400)
    return weight * math.pow(0.5, age_days / TOPIC_HALF_LIFE_DAYS)


def apply_topic_delta(entry: dict[str, Any] | None, delta: float, at: datetime) -> dict[str, Any]:
    entry = dict(entry or {})
    w = decayed(float(entry.get("w", 0.0)), entry.get("updatedAt"), at)
    entry["w"] = clamp(w + delta, -1.0, 1.0)
    entry["updatedAt"] = at
    return entry


def effective_topic_weight(topic_weights: dict[str, Any], topics: list[str], at: datetime) -> float:
    vals = [
        decayed(float(e.get("w", 0.0)), e.get("updatedAt"), at)
        for t in topics
        if (e := topic_weights.get(topic_key(t)))
    ]
    return sum(vals) / len(vals) if vals else 0.0


def topic_key(topic: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in topic.lower()).strip("_")[:60]


def clamp_trust(t: float) -> float:
    return clamp(t, 0.0, 1.0)
