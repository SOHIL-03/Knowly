"""Gmail push notifications: users.watch -> Pub/Sub gmail-events -> debounced mini-briefing."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from app.gmail.auth import GmailReauthRequired, gmail_service
from app.logs import get_logger, log
from app.services import Services
from app.store import local_date, now, stable_id

logger = get_logger(__name__)
DEBOUNCE_SECONDS = 600


def start_watch(svc: Services, uid: str) -> dict[str, Any] | None:
    topic = svc.settings.gmail_pubsub_topic_full
    if not topic:
        return None  # watch deferred (cut line 16.2); scheduled generation still works
    try:
        gmail = gmail_service(svc, uid)
    except GmailReauthRequired:
        return None
    res = gmail.users().watch(userId="me", body={"topicName": topic, "labelIds": ["INBOX"],
                                                 "labelFilterBehavior": "include"}).execute()
    expires = datetime.fromtimestamp(int(res["expiration"]) / 1000, tz=UTC)
    svc.paths.user(uid).set({"gmail": {"watchHistoryId": res["historyId"], "watchExpiresAt": expires}}, merge=True)
    return {"historyId": res["historyId"], "expiresAt": expires.isoformat()}


def stop_watch(svc: Services, uid: str) -> bool:
    try:
        gmail_service(svc, uid).users().stop(userId="me").execute()
        return True
    except GmailReauthRequired:
        return False


def renew_all(svc: Services) -> dict[str, int]:
    renewed = failed = 0
    for snap in svc.paths.db.collection("users").where(filter=FieldFilter("gmail.status", "==", "ok")).stream():
        try:
            if start_watch(svc, snap.id):
                renewed += 1
        except Exception:
            failed += 1
            logger.exception("watch renewal failed")
    return {"renewed": renewed, "failed": failed}


def handle_push(svc: Services, envelope: dict[str, Any]) -> str:
    data = json.loads(base64.b64decode(envelope["message"]["data"]))
    address = (data.get("emailAddress") or "").lower()
    users = list(svc.paths.db.collection("users").where(filter=FieldFilter("gmail.email", "==", address)).limit(1).stream())
    if not users:
        return "unknown_user"
    snap = users[0]
    user = snap.to_dict() or {}
    if (user.get("prefs") or {}).get("schedule", {}).get("mode") != "onArrival":
        return "daily_mode"
    window = int(now().timestamp()) // DEBOUNCE_SECONDS
    svc.dispatcher.task("gmail-debounced", {"uid": snap.id}, delay_seconds=DEBOUNCE_SECONDS,
                        dedupe_key=f"gmail-{stable_id(snap.id, str(window))}")
    log(logger, logging.INFO, "gmail_push_debounced", uid=snap.id)
    return "queued"


def run_debounced_mini_briefing(svc: Services, uid: str) -> None:
    from app.briefings import create_briefing

    user = svc.paths.user(uid).get().to_dict() or {}
    bid = f"{local_date(user.get('timezone') or 'UTC')}-a{now().strftime('%H%M')}"
    create_briefing(svc, uid, bid, kind="onArrival")
