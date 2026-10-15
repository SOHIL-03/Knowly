"""Account bootstrap, onboarding, profile, preferences, newsletters and Gmail connection."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from google.cloud import firestore
from pydantic import BaseModel, Field, field_validator

from app.ai import prompts
from app.ai.client import AiOutputInvalid, AiUnavailable
from app.ai.schemas import ConceptCandidates
from app.deps import CurrentUser, current_user, services
from app.errors import ApiError, bad_request
from app.learner import service as learner
from app.pipeline.ingest import load_sample_issues
from app.services import Services
from app.store import DEFAULT_PREFS, next_run_at, now, to_jsonable

router = APIRouter()


class Bootstrap(BaseModel):
    timezone: str = Field(default="UTC", max_length=64)
    mode: Literal["live", "sample"] | None = None


@router.post("/me/bootstrap")
def bootstrap(body: Bootstrap, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Creates users/{uid} on first sign-in. Anonymous users are always in sample mode."""
    ref = svc.paths.user(user.uid)
    snap = ref.get()
    mode = "sample" if user.anonymous else (body.mode or "sample")
    if not snap.exists:
        ref.set({
            "email": user.email, "createdAt": now(), "timezone": body.timezone, "mode": mode,
            "prefs": DEFAULT_PREFS, "onboarded": False, "gmail": {"status": "disconnected"},
            "nextRunAt": next_run_at(DEFAULT_PREFS, body.timezone),
        })
    else:
        update = {"timezone": body.timezone}
        if body.mode and not user.anonymous:
            update["mode"] = body.mode
        if user.email:
            update["email"] = user.email
        ref.update(update)
    return me(user, svc)


@router.get("/me")
def me(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    snap = svc.paths.user(user.uid).get()
    if not snap.exists:
        raise ApiError(404, "not_bootstrapped", "Account not set up yet")
    data = to_jsonable(snap.to_dict() or {})
    gmail = data.get("gmail") or {}
    gmail.pop("tokenSecretRef", None)
    profile = to_jsonable(svc.paths.profile(user.uid).get().to_dict() or {})
    return {"uid": user.uid, "anonymous": user.anonymous, **data, "gmail": gmail, "profile": profile}


class ProjectIn(BaseModel):
    text: str = Field(default="", max_length=4000)
    readmeUrl: str | None = Field(default=None, max_length=500)


class ProfileIn(BaseModel):
    project: ProjectIn
    goals: list[str] = Field(default_factory=list, max_length=5)
    muteTopics: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("goals", "muteTopics")
    @classmethod
    def _short(cls, v: list[str]) -> list[str]:
        return [x[:80] for x in v]


@router.put("/profile")
def put_profile(body: ProfileIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    from app.profile import save_profile

    if body.project.readmeUrl and not body.project.readmeUrl.startswith("https://"):
        raise bad_request("README link must start with https://")
    res = save_profile(svc, user.uid, body.project.text, body.project.readmeUrl, body.goals, body.muteTopics)
    return {"ok": True, **res}


@router.get("/profile/levelcheck/suggestions")
def levelcheck_suggestions(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """5 to 8 concepts drawn from the user's goals and project, for the optional level check."""
    prof = svc.paths.profile(user.uid).get().to_dict() or {}
    goals = [g.get("topic") for g in prof.get("goals") or []]
    project = (prof.get("project") or {}).get("text") or ""
    if not goals and not project:
        return {"concepts": []}
    try:
        res = svc.llm.generate_json(
            "flash", prompts.CONCEPTS_SYSTEM,
            f"Learner goals: {goals}\nProject: <source>{project}</source>\nList 5 to 8 concepts for a level check.",
            ConceptCandidates, temperature=0.2,
        )
        return {"concepts": res.concepts[:8]}
    except (AiUnavailable, AiOutputInvalid):
        return {"concepts": goals[:8], "fallback": True}


class Rating(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    level: Literal["new", "heard", "use"]


class LevelCheck(BaseModel):
    ratings: list[Rating] = Field(max_length=12)


@router.post("/profile/levelcheck")
def levelcheck(body: LevelCheck, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    ids = learner.save_level_check(svc, user.uid, [(r.name, r.level) for r in body.ratings])
    return {"conceptIds": ids}


class Schedule(BaseModel):
    mode: Literal["daily", "onArrival"] = "daily"
    time: str = Field(default="07:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    days: list[int] = Field(default_factory=lambda: list(range(7)))


class Prefs(BaseModel):
    lengthMin: Literal[5, 10, 15] = 10
    style: Literal["twoHost", "brief"] = "twoHost"
    speed: float = Field(default=1.0, ge=0.5, le=2.5)
    schedule: Schedule = Field(default_factory=Schedule)
    notify: Literal["push", "email", "none"] = "push"
    wildcardOn: bool = True
    endQuizOn: bool = True


class PrefsIn(BaseModel):
    prefs: Prefs
    timezone: str | None = Field(default=None, max_length=64)
    onboarded: bool | None = None


@router.put("/prefs")
def put_prefs(body: PrefsIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    ref = svc.paths.user(user.uid)
    data = ref.get().to_dict() or {}
    tz = body.timezone or data.get("timezone") or "UTC"
    prefs = body.prefs.model_dump()
    update = {"prefs": prefs, "timezone": tz, "nextRunAt": next_run_at(prefs, tz)}
    if body.onboarded is not None:
        update["onboarded"] = body.onboarded
    ref.update(update)
    return {"ok": True, "nextRunAt": update["nextRunAt"].isoformat() if update["nextRunAt"] else None}


@router.get("/newsletters")
def list_newsletters(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    data = svc.paths.user(user.uid).get().to_dict() or {}
    subs = {s.id: s.to_dict() or {} for s in svc.paths.newsletters(user.uid).stream()}
    if data.get("mode") == "sample":
        out = []
        for raw in load_sample_issues("sample"):
            out.append({"senderId": raw.sender_id, "name": raw.sender_name, "domain": raw.sender_domain,
                        "enabled": True, "sample": True, "trust": subs.get(raw.sender_id, {}).get("trust", 0.5)})
        return {"mode": "sample", "newsletters": out}
    out = []
    for sid, sub in subs.items():
        sender = svc.paths.sender(sid).get().to_dict() or {}
        out.append({"senderId": sid, "name": sender.get("name"), "domain": sender.get("domain"),
                    "address": sender.get("address"), "enabled": bool(sub.get("enabled")), "trust": sub.get("trust", 0.5)})
    return {"mode": "live", "newsletters": out}


@router.get("/newsletters/detected")
def detected(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    from app.gmail.fetch import detect_senders

    gmail = (svc.paths.user(user.uid).get().to_dict() or {}).get("gmail") or {}
    if gmail.get("status") != "ok":
        raise ApiError(409, "gmail_not_connected", "Connect Gmail first, or use sample mode.")
    return {"senders": detect_senders(svc, user.uid)}


class NewslettersIn(BaseModel):
    enabled: list[str] = Field(max_length=100)
    manual: list[str] = Field(default_factory=list, max_length=20)  # sender addresses added by hand


@router.put("/newsletters")
def put_newsletters(body: NewslettersIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    from app.gmail.fetch import sender_id_for

    enabled = set(body.enabled)
    for addr in body.manual:
        addr = addr.strip().lower()
        if "@" not in addr:
            raise bad_request(f"{addr} is not an email address")
        sid = sender_id_for(addr)
        svc.paths.sender(sid).set({"name": addr, "address": addr, "domain": addr.split("@")[-1]}, merge=True)
        enabled.add(sid)
    batch = svc.db.batch()
    existing = {s.id for s in svc.paths.newsletters(user.uid).stream()}
    for sid in enabled | existing:
        ref = svc.paths.newsletters(user.uid).document(sid)
        doc = {"enabled": sid in enabled}
        if sid not in existing:
            doc.update({"addedAt": now(), "trust": 0.5})
        batch.set(ref, doc, merge=True)
    batch.commit()
    return {"enabled": sorted(enabled)}


class GmailConnect(BaseModel):
    code: str = Field(min_length=10, max_length=2048)


@router.post("/auth/gmail/connect")
def gmail_connect(body: GmailConnect, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    from app.gmail.auth import exchange_code
    from app.gmail.watch import start_watch

    if user.anonymous:
        raise ApiError(403, "google_sign_in_required", "Sign in with Google before connecting Gmail.")
    res = exchange_code(svc, user.uid, body.code)
    svc.paths.user(user.uid).update({"mode": "live"})
    watch = None
    try:
        watch = start_watch(svc, user.uid)
    except Exception:
        watch = None
    return {"connected": True, "email": res["email"], "watch": watch}


@router.post("/auth/gmail/disconnect")
def gmail_disconnect(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    from app.account import disconnect_gmail

    return disconnect_gmail(svc, user.uid)


class FcmToken(BaseModel):
    token: str = Field(min_length=20, max_length=4096)


@router.post("/notifications/token")
def add_fcm_token(body: FcmToken, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    svc.paths.user(user.uid).update({"fcmTokens": firestore.ArrayUnion([body.token])})
    return {"ok": True}
