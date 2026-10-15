"""Firestore paths and small helpers for the Architecture v3 data model (section 6).

Shared cache:   senders/{id}, issues/{id}, items/{id}
Private:        users/{uid} and everything below it
Server only:    quizSecrets/{quizId} (rubrics), deletionAudit/{uidHash}
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud import firestore
from google.cloud.firestore_v1.vector import Vector


def now() -> datetime:
    return datetime.now(UTC)


def local_date(tz: str, at: datetime | None = None) -> str:
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    return (at or now()).astimezone(zone).date().isoformat()


def stable_id(*parts: str, prefix: str = "") -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]
    return f"{prefix}{digest}"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60] or "x"


def vec(values: list[float] | None) -> Vector | None:
    return Vector(values) if values else None


def unvec(value: Any) -> list[float] | None:
    if value is None:
        return None
    return [float(v) for v in value]


class Paths:
    def __init__(self, db: firestore.Client) -> None:
        self.db = db

    # shared
    def sender(self, sid: str):
        return self.db.collection("senders").document(sid)

    def issue(self, iid: str):
        return self.db.collection("issues").document(iid)

    def items(self):
        return self.db.collection("items")

    def item(self, iid: str):
        return self.db.collection("items").document(iid)

    # private
    def user(self, uid: str):
        return self.db.collection("users").document(uid)

    def profile(self, uid: str):
        return self.user(uid).collection("profile").document("main")

    def newsletters(self, uid: str):
        return self.user(uid).collection("newsletters")

    def concepts(self, uid: str):
        return self.user(uid).collection("concepts")

    def briefings(self, uid: str):
        return self.user(uid).collection("briefings")

    def briefing(self, uid: str, bid: str):
        return self.briefings(uid).document(bid)

    def segments(self, uid: str, bid: str):
        return self.briefing(uid, bid).collection("segments")

    def segment(self, uid: str, bid: str, sid: str):
        return self.segments(uid, bid).document(sid)

    def events(self, uid: str):
        return self.user(uid).collection("events")

    def library(self, uid: str):
        return self.user(uid).collection("library")

    def qa(self, uid: str):
        return self.user(uid).collection("qa")

    def quizzes(self, uid: str):
        return self.user(uid).collection("quizzes")

    def usage(self, uid: str, day: str):
        return self.user(uid).collection("usage").document(day)

    # server only
    def quiz_secret(self, qid: str):
        return self.db.collection("quizSecrets").document(qid)

    def deletion_audit(self, uid_hash: str):
        return self.db.collection("deletionAudit").document(uid_hash)


DEFAULT_PREFS: dict[str, Any] = {
    "lengthMin": 10,
    "style": "twoHost",
    "speed": 1.0,
    "schedule": {"mode": "daily", "time": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]},
    "notify": "push",
    "wildcardOn": True,
    "endQuizOn": True,
}


def next_run_at(prefs: dict[str, Any], tz: str, after: datetime | None = None) -> datetime | None:
    """Next scheduled briefing time in UTC for the user's local schedule (A6: daily batch)."""
    sched = prefs.get("schedule") or {}
    if sched.get("mode") != "daily":
        return None
    try:
        zone = ZoneInfo(tz)
    except Exception:
        zone = ZoneInfo("UTC")
    hh, mm = (int(x) for x in str(sched.get("time", "07:00")).split(":"))
    days = set(sched.get("days") or range(7))
    base = (after or now()).astimezone(zone)
    for offset in range(0, 8):
        d: date = (base + timedelta(days=offset)).date()
        candidate = datetime(d.year, d.month, d.day, hh, mm, tzinfo=zone)
        if candidate > base and candidate.weekday() in days:
            return candidate.astimezone(UTC)
    return None


def to_jsonable(value: Any) -> Any:
    """Firestore snapshot data -> JSON-safe structures (timestamps to ISO, vectors dropped)."""
    if isinstance(value, Vector):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items() if not isinstance(v, Vector)}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    return value
