"""Checkpoint endpoints: /ask (streamed), /deeper, /apply, /quiz/*, /rephrase."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import checkpoint as cp
from app.ai.client import QUOTA_MESSAGE, AiOutputInvalid, AiQuotaExceeded, AiUnavailable
from app.deps import CurrentUser, current_user, services
from app.errors import ApiError
from app.services import Services

router = APIRouter()


class SegRef(BaseModel):
    briefingId: str = Field(max_length=64)
    segId: str = Field(max_length=32)


class AskIn(SegRef):
    question: str = Field(min_length=2, max_length=1000)


def _ai_errors(fn):
    try:
        return fn()
    except AiQuotaExceeded:
        raise ApiError(429, "ai_quota", QUOTA_MESSAGE)
    except AiUnavailable:
        raise ApiError(503, "ai_unavailable", "The assistant is unavailable right now. Try again shortly.")
    except AiOutputInvalid:
        raise ApiError(502, "ai_invalid", "The assistant returned an answer we could not check. Try again.")


@router.post("/ask")
def ask(body: AskIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Server-sent events: delta / reset events while streaming, then one final event with the
    validated answer and citations (newsletter name + link resolved on the server)."""
    gen = cp.ask_stream(svc, user.uid, body.briefingId, body.segId, body.question.strip())
    try:
        first = next(gen)  # surface limit / not-found errors as normal JSON errors
    except StopIteration:
        first = None
    except AiQuotaExceeded:
        raise ApiError(429, "ai_quota", QUOTA_MESSAGE)
    except AiUnavailable:
        raise ApiError(503, "ai_unavailable", "The assistant is unavailable right now. Try again shortly.")

    def stream():
        try:
            if first is not None:
                yield f"data: {json.dumps(first, default=str)}\n\n"
            for ev in gen:
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except AiUnavailable as exc:
            message = QUOTA_MESSAGE if isinstance(exc, AiQuotaExceeded) else "The assistant is unavailable right now."
            yield f"data: {json.dumps({'type': 'error', 'message': message})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class DeeperIn(SegRef):
    itemId: str | None = Field(default=None, max_length=64)


@router.post("/deeper", status_code=202)
def deeper(body: DeeperIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return cp.request_deeper(svc, user.uid, body.briefingId, body.segId, body.itemId)


@router.post("/apply")
def apply(body: SegRef, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return _ai_errors(lambda: cp.apply_to_project(svc, user.uid, body.briefingId, body.segId))


@router.post("/quiz/start")
def quiz_start(body: SegRef, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return _ai_errors(lambda: cp.quiz_start(svc, user.uid, body.briefingId, body.segId))


class QuizHintIn(BaseModel):
    quizId: str = Field(max_length=40)


@router.post("/quiz/hint")
def quiz_hint(body: QuizHintIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return cp.quiz_hint(svc, user.uid, body.quizId)


class QuizAnswerIn(BaseModel):
    quizId: str = Field(max_length=40)
    answer: str | None = Field(default=None, max_length=2000)
    dontKnow: bool = False


@router.post("/quiz/answer")
def quiz_answer(body: QuizAnswerIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return _ai_errors(lambda: cp.quiz_answer(svc, user.uid, body.quizId, body.answer, body.dontKnow))


class RephraseIn(SegRef):
    text: str = Field(min_length=5, max_length=3000)
    direction: Literal["simpler", "technical"] = "simpler"


@router.post("/rephrase")
def rephrase(body: RephraseIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return _ai_errors(lambda: cp.rephrase(svc, user.uid, body.briefingId, body.segId, body.text, body.direction))


@router.post("/qa/{qa_id}/audio")
def answer_audio(qa_id: str, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    """Spoken version of a stored answer (Gemini single-speaker TTS); cached after the first call."""
    return _ai_errors(lambda: cp.answer_audio(svc, user.uid, qa_id))


class QuizAudioIn(BaseModel):
    part: Literal["question", "feedback"] = "question"


@router.post("/quiz/{quiz_id}/audio")
def quiz_audio(quiz_id: str, body: QuizAudioIn, user: CurrentUser = Depends(current_user), svc: Services = Depends(services)):
    return _ai_errors(lambda: cp.quiz_audio(svc, user.uid, quiz_id, body.part))
