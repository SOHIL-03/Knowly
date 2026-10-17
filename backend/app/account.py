"""Download my data and Delete all my data (Architecture v3 section 6.5 and 13)."""

from __future__ import annotations

import logging
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from app.infra.firebase import delete_auth_user
from app.infra.secrets import gmail_secret_name
from app.logs import get_logger, log, uid_hash
from app.services import Services
from app.store import now, to_jsonable

logger = get_logger(__name__)
USER_SUBCOLLECTIONS = ["profile", "newsletters", "concepts", "briefings", "events", "feedback", "library", "qa", "quizzes", "usage"]


def _dump(doc_ref) -> dict[str, Any]:
    snap = doc_ref.get()
    out: dict[str, Any] = {"id": doc_ref.id, "data": to_jsonable(snap.to_dict()) if snap.exists else None}
    subs = {}
    for coll in doc_ref.collections():
        subs[coll.id] = [_dump(d.reference) for d in coll.stream()]
    if subs:
        out["collections"] = subs
    return out


def export_user(svc: Services, uid: str) -> dict[str, Any]:
    tree = _dump(svc.paths.user(uid))
    user = tree.get("data") or {}
    if user.get("gmail"):
        user["gmail"].pop("tokenSecretRef", None)  # the token itself is never exported
    return {"exportedAt": now().isoformat(), "uid": uid, "user": tree}


def delete_user(svc: Services, uid: str, *, delete_auth: bool = True) -> dict[str, Any]:
    """Deletes everything. Each step is reported; failures are recorded instead of claiming success."""
    steps: dict[str, str] = {}

    def step(name: str, fn) -> None:
        try:
            result = fn()
            steps[name] = f"ok{'' if result is None else f' ({result})'}"
        except Exception as exc:
            steps[name] = f"failed: {type(exc).__name__}"
            logger.exception("delete step failed: %s", name)

    user = svc.paths.user(uid).get().to_dict() or {}
    gmail = user.get("gmail") or {}
    if gmail.get("tokenSecretRef"):
        from app.gmail.watch import stop_watch

        step("gmail_watch_stop", lambda: "stopped" if stop_watch(svc, uid) else "no active watch")
    step("gmail_token", lambda: svc.secrets.delete(gmail_secret_name(uid)))
    for prefix in (f"audio/{uid}/", f"briefs/{uid}/", f"transcripts/{uid}/"):
        step(f"storage:{prefix.split('/')[0]}", lambda p=prefix: svc.blobs.delete_prefix(p))
    rubrics = svc.paths.db.collection("quizSecrets").where(filter=FieldFilter("uid", "==", uid))
    step("quiz_rubrics", lambda: _delete_query(rubrics))
    step("firestore_user_tree", lambda: svc.db.recursive_delete(svc.paths.user(uid)))
    if delete_auth:
        step("auth_user", lambda: delete_auth_user(uid))
    complete = all(v.startswith("ok") for v in steps.values())
    svc.paths.deletion_audit(uid_hash(uid)).set({"at": now(), "complete": complete, "steps": steps})
    log(logger, logging.INFO, "account_deleted", uid=uid, complete=complete)
    return {"complete": complete, "steps": steps}


def _delete_query(query) -> int:
    n = 0
    for snap in query.stream():
        snap.reference.delete()
        n += 1
    return n


def disconnect_gmail(svc: Services, uid: str) -> dict[str, Any]:
    from app.gmail.watch import stop_watch

    result = {"watchStopped": False}
    try:
        result["watchStopped"] = stop_watch(svc, uid)
    except Exception:
        logger.exception("stop watch failed")
    svc.secrets.delete(gmail_secret_name(uid))
    svc.paths.user(uid).set({"gmail": {"status": "disconnected", "tokenSecretRef": None, "watchExpiresAt": None}}, merge=True)
    return result
