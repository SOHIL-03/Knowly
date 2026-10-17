"""The persistent podcast page: /podcasts/{podcastId} and everything on it (transcript, chat, concepts, assessments), plus the
learning profile. podcastId = category.date.section[.variant], see app.tldr.podcast.make_pid.

Identity always comes from the verified token; no request carries a user id. A private podcast, chat, assessment or
profile of another user is simply "not found".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.deps import CurrentUser, current_user, optional_user, services
from app.errors import ApiError, not_found
from app.learning import assessment, chat, concepts, profile
from app.services import Services
from app.tldr import podcast
from app.tldr.scraper import TldrError

router = APIRouter()


def _load(svc: Services, pid: str, uid: str | None) -> tuple[dict, str]:
    try:
        return podcast.load_for(svc, pid, uid)
    except TldrError:
        raise not_found("Podcast")


@router.get("/podcasts/{pid}")
def get_podcast(pid: str, svc: Services = Depends(services), user: CurrentUser | None = Depends(optional_user)):
    """The podcast by its persistent id: status, transcript with timings, audio, sources. Works after refresh or a new session."""
    _load(svc, pid, user.uid if user else None)
    category, date, sid, variant = podcast.parse_pid(pid)
    return podcast.view(svc, category, date, sid, variant, user.uid if user else None)


def podcast_concepts(svc: Services, pid: str, uid: str | None) -> list[dict]:
    """Concepts of a podcast. If extraction failed or never ran, only this step is retried (from the stored transcript)."""
    st, key = _load(svc, pid, uid)
    stored = concepts.load(svc, key)
    if stored["status"] == "ready":
        return stored["concepts"]
    if st.get("status") not in ("ready", "processing") or not st.get("lines"):
        return []
    _, _, sid, _ = podcast.parse_pid(pid)
    concepts.extract_and_save(svc, key, pid, sid, st["lines"], svc.blobs.get_text(f"{key}.source.txt") or "")
    return concepts.load(svc, key)["concepts"]


@router.get("/podcasts/{pid}/concepts")
def get_concepts(pid: str, svc: Services = Depends(services), user: CurrentUser | None = Depends(optional_user)):
    found = podcast_concepts(svc, pid, user.uid if user else None)
    return {"status": "ready" if found else "none", "concepts": found}


# --- chat ----------------------------------------------------------------------------------------------------------------

class ChatIn(BaseModel):
    clientId: str = Field(min_length=8, max_length=40, description="Idempotency key: a retry with the same id returns the stored answer")
    text: str = Field(max_length=2000)
    inputMode: str = Field(default="text", pattern="^(text|voice)$")


@router.get("/podcasts/{pid}/chat")
def chat_history(pid: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    try:
        return {"messages": chat.history(svc, user.uid, pid)}
    except TldrError:
        raise not_found("Podcast")


@router.post("/podcasts/{pid}/chat")
def chat_send(pid: str, body: ChatIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Typed questions and confirmed voice transcripts both come here and take the same path (OpenAI answer)."""
    try:
        return chat.answer(svc, user.uid, pid, body.text, body.clientId, body.inputMode)
    except TldrError:
        raise not_found("Podcast")


@router.post("/podcasts/{pid}/chat/{mid}/audio")
def chat_audio(pid: str, mid: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    try:
        return chat.answer_audio(svc, user.uid, pid, mid)
    except TldrError:
        raise not_found("Podcast")


@router.post("/podcasts/{pid}/transcribe")
async def transcribe(pid: str, request: Request, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Body: the raw recording (Content-Type audio/webm, audio/ogg, audio/mp4 ...). Returns {text} for the listener to review."""
    audio = await request.body()
    try:
        return chat.transcribe(svc, user.uid, pid, audio, request.headers.get("content-type", ""))
    except TldrError:
        raise not_found("Podcast")


# --- assessments ---------------------------------------------------------------------------------------------------------

class AssessmentIn(BaseModel):
    retake: bool = False


class AnswerIn(BaseModel):
    answer: str = Field(max_length=assessment.MAX_ANSWER_CHARS)


@router.get("/podcasts/{pid}/assessments")
def list_assessments(pid: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    _load(svc, pid, user.uid)
    return {"assessments": assessment.list_for(svc, user.uid, pid)}


@router.post("/podcasts/{pid}/assessments")
def start_assessment(pid: str, body: AssessmentIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Test Your Understanding. Returns the open assessment of this podcast if there is one (never a duplicate)."""
    st, _ = _load(svc, pid, user.uid)
    if not st.get("lines"):
        raise ApiError(409, "not_ready", "The podcast script is not ready yet.")
    existing = [a for a in assessment.list_for(svc, user.uid, pid) if a["status"] in assessment.OPEN_STATES]
    if existing:
        return existing[0]
    return assessment.create(svc, user.uid, pid, podcast_concepts(svc, pid, user.uid), st["lines"], retake=body.retake)


@router.get("/assessments/{aid}")
def get_assessment(aid: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return assessment.get(svc, user.uid, aid)


@router.put("/assessments/{aid}/answers/{qid}")
def save_answer(aid: str, qid: str, body: AnswerIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return assessment.save_answer(svc, user.uid, aid, qid, body.answer)


@router.post("/assessments/{aid}/submit")
def submit_assessment(aid: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Evaluates the saved answers and updates the verified profile from that evidence. Safe to repeat."""
    a = assessment.get(svc, user.uid, aid)
    by_id = {c["concept_id"]: c for c in podcast_concepts(svc, a["podcastId"], user.uid)}
    return assessment.submit(svc, user.uid, aid, by_id)


# --- learning profile ----------------------------------------------------------------------------------------------------

@router.get("/learning/profile")
def learning_profile(user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """The caller's verified concept mastery (from assessment evidence only) and their unverified signals."""
    return profile.get_profile(svc, user.uid)
