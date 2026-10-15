"""Endpoints called by Google Cloud, not by the browser: Pub/Sub push, Cloud Tasks, Cloud Scheduler,
plus the small admin view and dead-letter replay (Architecture v3 section 15.3).

Every route here requires a verified Google OIDC token for the invoker service account."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from google.cloud.firestore_v1.base_query import FieldFilter

from app.deps import internal_caller, services
from app.errors import not_found
from app.logs import get_logger, log
from app.services import Services
from app.store import local_date, now, to_jsonable

router = APIRouter(prefix="/internal", dependencies=[Depends(internal_caller)])
logger = get_logger(__name__)


@router.post("/pubsub/run-briefing")
async def pubsub_run_briefing(request: Request, svc: Services = Depends(services)):
    envelope = await request.json()
    msg = json.loads(base64.b64decode(envelope["message"]["data"]))
    uid, bid = msg["uid"], msg["briefingId"]
    if svc.settings.briefing_job_name:
        _start_job(svc, uid, bid)
        return {"started": "job"}
    from app.pipeline.runner import run_briefing

    return {"result": run_briefing(svc, uid, bid)}


def _start_job(svc: Services, uid: str, bid: str) -> None:
    from google.cloud import run_v2

    client = run_v2.JobsClient()
    name = client.job_path(svc.settings.gcp_project, svc.settings.gcp_region, svc.settings.briefing_job_name)
    client.run_job(request=run_v2.RunJobRequest(
        name=name,
        overrides=run_v2.RunJobRequest.Overrides(container_overrides=[
            run_v2.RunJobRequest.Overrides.ContainerOverride(env=[
                run_v2.EnvVar(name="KNOWLY_UID", value=uid), run_v2.EnvVar(name="KNOWLY_BRIEFING_ID", value=bid)])
        ]),
    ))


@router.post("/pubsub/gmail")
async def pubsub_gmail(request: Request, svc: Services = Depends(services)):
    from app.gmail.watch import handle_push

    return {"result": handle_push(svc, await request.json())}


@router.post("/tasks/{name}")
async def run_task(name: str, request: Request):
    from app.tasks import TASK_HANDLERS

    handler = TASK_HANDLERS.get(name)
    if handler is None:
        raise not_found("Task")
    handler(await request.json())  # a raised error returns 500 and Cloud Tasks retries with backoff
    return {"ok": True}


@router.post("/scheduler/dispatch")
def scheduler_dispatch(svc: Services = Depends(services)):
    """Runs every 5 minutes: starts briefings for users whose local time has just passed (A6)."""
    from app.briefings import create_briefing
    from app.store import DEFAULT_PREFS, next_run_at

    started = 0
    at = now()
    for snap in svc.paths.db.collection("users").where(filter=FieldFilter("nextRunAt", "<=", at)).limit(200).stream():
        user = snap.to_dict() or {}
        prefs = {**DEFAULT_PREFS, **(user.get("prefs") or {})}
        tz = user.get("timezone") or "UTC"
        snap.reference.update({"nextRunAt": next_run_at(prefs, tz, after=at)})
        if user.get("mode") != "sample" and (user.get("gmail") or {}).get("status") != "ok":
            continue  # paused until Gmail is reconnected
        _, created = create_briefing(svc, snap.id, local_date(tz), kind="daily")
        started += int(created)
    log(logger, logging.INFO, "scheduler_dispatch", started=started)
    return {"started": started}


@router.post("/scheduler/renew-watches")
def renew_watches(svc: Services = Depends(services)):
    from app.gmail.watch import renew_all

    return renew_all(svc)


@router.get("/admin/briefings")
def admin_briefings(date: str | None = None, svc: Services = Depends(services)) -> dict[str, Any]:
    """Today's briefings with status and failed stage, across users (uids are hashed)."""
    from app.logs import uid_hash

    day = date or local_date("UTC")
    rows = []
    for snap in svc.paths.db.collection_group("briefings").where(filter=FieldFilter("date", "==", day)).stream():
        d = snap.to_dict() or {}
        uid = snap.reference.parent.parent.id
        rows.append({"uidHash": uid_hash(uid), "briefingId": snap.id, "status": d.get("status"),
                     "stage": (d.get("progress") or {}).get("stage"), "error": d.get("error"),
                     "timings": d.get("timings"), "audioReady": d.get("audioReadyCount")})
    return {"date": day, "briefings": to_jsonable(rows)}


@router.post("/admin/replay")
async def replay(request: Request, svc: Services = Depends(services)):
    """Dead-letter replay: resets a failed briefing and starts it again."""
    body = await request.json()
    ref = svc.paths.briefing(body["uid"], body["briefingId"])
    if not ref.get().exists:
        raise not_found("Briefing")
    ref.update({"status": "generating", "error": None, "leaseUntil": None, "progress": {"stage": "queued", "message": "Retrying"}})
    svc.dispatcher.run_briefing(body["uid"], body["briefingId"])
    return {"ok": True}
