"""Briefings, segments, audio and playback events."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.briefings import briefing_view, create_briefing, latest_briefing_id, next_regeneration_id
from app.checkpoint import check_limit, record_events, reorder
from app.deps import CurrentUser, current_user, services
from app.errors import ApiError, not_found
from app.pipeline.audio_stage import retry_segment_audio
from app.services import Services
from app.store import local_date, now, to_jsonable

router = APIRouter()


class GenerateIn(BaseModel):
    kind: Literal["daily", "review"] = "daily"
    regenerate: bool = False


@router.post("/briefings/generate", status_code=202)
def generate(body: GenerateIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Starts a briefing and returns its id at once; progress is read from Firestore."""
    data = svc.paths.user(user.uid).get().to_dict()
    if not data:
        raise ApiError(404, "not_bootstrapped", "Account not set up yet")
    if data.get("mode") != "sample" and (data.get("gmail") or {}).get("status") == "reauth":
        raise ApiError(409, "gmail_reauth", "Reconnect Gmail to keep getting briefings.")
    day = local_date(data.get("timezone") or "UTC")
    if body.kind == "review":
        bid = f"{day}-review"
        if body.regenerate:
            bid = next_regeneration_id(svc, user.uid, bid)
    else:
        bid = next_regeneration_id(svc, user.uid, day) if body.regenerate else day
    if body.regenerate:
        check_limit(svc, user.uid, "generate", svc.settings.daily_limit_generate)
    bid, created = create_briefing(svc, user.uid, bid, kind=body.kind)
    return {"briefingId": bid, "created": created}


@router.get("/briefings")
def list_briefings(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    out = []
    for s in svc.paths.briefings(user.uid).order_by("createdAt", direction="DESCENDING").limit(30).stream():
        d = s.to_dict() or {}
        out.append({"id": s.id, "date": d.get("date"), "status": d.get("status"), "kind": d.get("kind"),
                    "totalDurationSec": d.get("totalDurationSec"), "rating": d.get("rating"),
                    "recap": to_jsonable(d.get("recap"))})
    return {"briefings": out}


@router.get("/briefings/today")
def today(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    bid = latest_briefing_id(svc, user.uid)
    if not bid:
        raise not_found("Briefing")
    return briefing_view(svc, user.uid, bid)


@router.get("/briefings/{bid}")
def get_briefing(bid: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return briefing_view(svc, user.uid, bid)


class SegmentsPatch(BaseModel):
    order: list[str] = Field(default_factory=list, max_length=20)
    removed: list[str] = Field(default_factory=list, max_length=20)


@router.patch("/briefings/{bid}/segments")
def patch_segments(bid: str, body: SegmentsPatch, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    if not svc.paths.briefing(user.uid, bid).get().exists:
        raise not_found("Briefing")
    reorder(svc, user.uid, bid, body.order, body.removed)
    return {"ok": True}


class RatingIn(BaseModel):
    rating: Literal["up", "down"]
    reason: str | None = Field(default=None, max_length=500)


@router.post("/briefings/{bid}/rating")
def rate(bid: str, body: RatingIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    ref = svc.paths.briefing(user.uid, bid)
    if not ref.get().exists:
        raise not_found("Briefing")
    ref.update({"rating": body.rating, "ratingReason": body.reason, "ratedAt": now()})
    return {"ok": True}


def _segment(svc: Services, uid: str, bid: str, sid: str) -> dict[str, Any]:
    snap = svc.paths.segment(uid, bid, sid).get()
    if not snap.exists:
        raise not_found("Segment")
    return snap.to_dict() or {}


@router.post("/briefings/{bid}/segments/{sid}/audio/retry", status_code=202)
def audio_retry(bid: str, sid: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    seg = _segment(svc, user.uid, bid, sid)
    if seg.get("audioStatus") == "processing":
        raise ApiError(409, "conflict", "Audio is being processed")
    retry_segment_audio(svc, user.uid, bid, sid)
    return {"ok": True}


class EventIn(BaseModel):
    type: Literal["play", "skip", "replay", "openSource", "lessLikeThis", "cardRemoved", "speedChange", "pause", "complete"]
    briefingId: str | None = Field(default=None, max_length=64)
    segId: str | None = Field(default=None, max_length=32)
    itemId: str | None = Field(default=None, max_length=64)
    conceptId: str | None = Field(default=None, max_length=80)
    payload: dict[str, Any] = Field(default_factory=dict)


class EventsIn(BaseModel):
    events: list[EventIn] = Field(max_length=200)


@router.post("/events")
def post_events(body: EventsIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Batched playback and interaction events from the player (sent every 10 s and on pause)."""
    events = []
    for e in body.events:
        ev = e.model_dump()
        ev["payload"] = {k: v for k, v in list(ev["payload"].items())[:10] if isinstance(v, (str, int, float, bool))}
        events.append(ev)
    if events:
        record_events(svc, user.uid, events)
    return {"accepted": len(events)}
