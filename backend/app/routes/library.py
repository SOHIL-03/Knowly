"""Library, Knowledge page, data export / deletion, and local media serving."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import checkpoint as cp
from app.account import delete_user, export_user
from app.deps import CurrentUser, current_user, services
from app.errors import ApiError, not_found
from app.infra.blobs import verify_local_media
from app.learner import service as learner
from app.learner.model import explanation_band
from app.services import Services
from app.store import to_jsonable

router = APIRouter()


class SaveIn(BaseModel):
    itemId: str = Field(max_length=64)
    note: str | None = Field(default=None, max_length=2000)
    briefingId: str | None = Field(default=None, max_length=64)
    segId: str | None = Field(default=None, max_length=32)
    source: Literal["save", "qa"] = "save"


@router.post("/library")
def save(body: SaveIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return cp.save_item(svc, user.uid, body.itemId, body.note, body.source, body.briefingId, body.segId)


@router.delete("/library/{lib_id}")
def unsave(lib_id: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    ref = svc.paths.library(user.uid).document(lib_id)
    if not ref.get().exists:
        raise not_found("Library item")
    ref.delete()
    return {"ok": True}


@router.get("/library")
def library(
    q: str | None = Query(default=None, max_length=200),
    topic: str | None = Query(default=None, max_length=80),
    newsletter: str | None = Query(default=None, max_length=80),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    user: CurrentUser = Depends(current_user),
    svc: Services = Depends(services),
):
    """Saved stories and Q&A with search and filters by topic, newsletter and date.

    Firestore has no full-text search, so filtering happens here over the user's own library,
    which is small (one user's saves)."""
    items = [{"id": s.id, **to_jsonable(s.to_dict() or {})}
             for s in svc.paths.library(user.uid).order_by("savedAt", direction="DESCENDING").limit(500).stream()]
    qas = [{"id": s.id, **to_jsonable(s.to_dict() or {})}
           for s in svc.paths.qa(user.uid).order_by("ts", direction="DESCENDING").limit(200).stream()]

    def keep(text: str, tags: list[str], sender: str | None, when: str | None) -> bool:
        if q and q.lower() not in text.lower():
            return False
        if topic and topic.lower() not in [t.lower() for t in tags]:
            return False
        if newsletter and newsletter != sender:
            return False
        d = (when or "")[:10]
        if date_from and d and d < date_from.isoformat():
            return False
        if date_to and d and d > date_to.isoformat():
            return False
        return True

    items = [i for i in items if keep(f"{i.get('headline','')} {i.get('blurb','')} {i.get('note','')}", i.get("tags") or [],
                                      i.get("senderId"), i.get("savedAt"))]
    qas = [x for x in qas if keep(f"{x.get('question','')} {x.get('answer','')}", [],
                                  None if not newsletter else (x.get("citations") or [{}])[0].get("newsletter"), x.get("ts"))]
    topics = sorted({t for i in items for t in (i.get("tags") or [])})
    senders = sorted({(i.get("senderId"), i.get("senderName")) for i in items if i.get("senderId")}, key=lambda s: s[1] or "")
    return {"items": items, "qa": qas, "facets": {"topics": topics, "newsletters": [{"id": a, "name": b} for a, b in senders]}}


@router.get("/knowledge")
def knowledge(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    concepts = []
    for c in learner.load_concepts(svc, user.uid):
        c.pop("embedding", None)
        c = to_jsonable(c)
        c["band"] = explanation_band(float(c.get("mastery", 0)))
        concepts.append(c)
    concepts.sort(key=lambda c: (-int(c.get("evidenceCount") or 0), c.get("name", "")))
    return {"concepts": concepts}


class KnowledgePatch(BaseModel):
    level: Literal["new", "heard", "use"]


@router.patch("/knowledge/{concept_id}")
def correct(concept_id: str, body: KnowledgePatch, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    res = learner.user_correction(svc, user.uid, concept_id, body.level)
    if not res:
        raise not_found("Concept")
    return to_jsonable(res)


@router.get("/export")
def export(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    import json

    body = json.dumps(export_user(svc, user.uid), indent=1, default=str)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="knowly-export.json"'})


@router.delete("/account")
def delete_account(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    result = delete_user(svc, user.uid)
    if not result["complete"]:
        raise ApiError(500, "deletion_incomplete", "Some data could not be deleted. We recorded which steps failed; try again.",
                       result["steps"])
    return result


@router.get("/media")
def media(t: str, s: str, svc: Services = Depends(services)):
    """Local-mode replacement for signed Cloud Storage URLs (the emulator cannot sign)."""
    if svc.settings.is_cloud:
        raise not_found("Media")
    path = verify_local_media(svc.settings, t, s)
    if not path:
        raise ApiError(403, "forbidden", "Link expired")
    data = svc.blobs.get_bytes(path)
    if data is None:
        raise not_found("Media")
    ext = path.rsplit(".", 1)[-1]
    ctype = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm",
             "ogg": "audio/ogg", "json": "application/json"}.get(ext, "application/octet-stream")
    return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=300", "Accept-Ranges": "none"})
