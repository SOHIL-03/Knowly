"""Briefing creation (B1 Trigger) and read models for the Today screen."""

from __future__ import annotations

import logging
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud.firestore_v1.base_query import FieldFilter

from app.errors import not_found
from app.logs import get_logger, log
from app.services import Services
from app.store import local_date, now, to_jsonable

logger = get_logger(__name__)


def create_briefing(svc: Services, uid: str, bid: str, *, kind: str = "daily",
                    review_before_audio: bool | None = None) -> tuple[str, bool]:
    """Creates the briefing with status=generating and starts the pipeline.

    The document id is the idempotency key (uid + date, or date + suffix for regenerations and
    mini-briefings). A second trigger for the same id is ignored. Returns (id, created)."""
    user = svc.paths.user(uid).get().to_dict() or {}
    ref = svc.paths.briefing(uid, bid)
    try:
        ref.create({
            "date": local_date(user.get("timezone") or "UTC"),
            "kind": kind,
            "mode": user.get("mode", "sample"),
            "status": "generating",
            "reviewBeforeAudio": svc.settings.review_before_audio if review_before_audio is None else review_before_audio,
            "progress": {"stage": "queued", "message": "Starting"},
            "createdAt": now(),
        })
    except AlreadyExists:
        log(logger, logging.INFO, "briefing_exists", uid=uid, briefingId=bid)
        return bid, False
    log(logger, logging.INFO, "briefing_created", uid=uid, briefingId=bid, kind=kind, mode=user.get("mode", "sample"),
        dispatch=svc.settings.dispatch_mode)
    svc.dispatcher.run_briefing(uid, bid)
    return bid, True


def next_regeneration_id(svc: Services, uid: str, day: str) -> str:
    existing = {b.id for b in svc.paths.briefings(uid).where(filter=FieldFilter("date", "==", day)).stream()}
    if day not in existing:
        return day
    n = 2
    while f"{day}-r{n}" in existing:
        n += 1
    return f"{day}-r{n}"


def briefing_view(svc: Services, uid: str, bid: str) -> dict[str, Any]:
    snap = svc.paths.briefing(uid, bid).get()
    if not snap.exists:
        raise not_found("Briefing")
    data = to_jsonable(snap.to_dict() or {})
    data.pop("leaseOwner", None)
    data.pop("leaseUntil", None)
    facets = {r.get("itemId"): r for r in data.get("ranked") or [] if isinstance(r, dict)}
    segs = []
    for s in svc.paths.segments(uid, bid).order_by("order").stream():
        seg = to_jsonable(s.to_dict() or {})
        seg["id"] = s.id
        seg["audioUrl"] = svc.blobs.read_url(seg["audioPath"]) if seg.get("audioPath") and seg.get("audioStatus") == "ok" else None
        seg["wordTimingsUrl"] = svc.blobs.read_url(seg["wordTimingsPath"]) if seg.get("wordTimingsPath") else None
        seg["sources"] = _sources(svc, seg.get("itemIds") or [], seg.get("itemReasons") or {}, facets)
        segs.append(seg)
    return {"id": bid, **data, "segments": segs}


def _sources(svc: Services, ids: list[str], reasons: dict[str, str], ranked: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    out = []
    for iid in ids:
        it = svc.paths.item(iid).get().to_dict()
        if it:
            src = {"itemId": iid, "headline": it["headline"], "newsletter": it.get("senderName"),
                   "url": it.get("url"), "linkOnly": bool(it.get("linkOnly")), "reason": reasons.get(iid)}
            r = (ranked or {}).get(iid) or {}
            if r.get("facetId") and r.get("explained"):  # only when the reason line names this facet
                src["matchedFacet"] = {"id": r["facetId"], "type": r.get("facetType"), "text": r.get("facetText")}
            out.append(src)
    return out


def latest_briefing_id(svc: Services, uid: str) -> str | None:
    q = svc.paths.briefings(uid).order_by("createdAt", direction="DESCENDING").limit(1).stream()
    for snap in q:
        return snap.id
    return None
