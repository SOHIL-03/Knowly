"""Question and answer about one podcast, by text or voice.

  typed:  text -> OpenAI (answer) -> [on request] Gemini TTS
  voice:  recording -> Gemini STT -> text shown to the listener, who confirms -> OpenAI (answer) -> [on request] Gemini TTS

Both inputs end in `answer()`, the same function. Stored under users/{uid}/podcastChats/{podcastId}/messages/{id}.

This module deliberately does NOT import app.learning.profile's evidence functions: asking a question, the answer, and
listening to it can never change mastery. At most an unverified "question asked" signal is recorded (profile.record_signal).
It reads the verified profile only to pitch the explanation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.ai import prompts
from app.ai.client import QUOTA_MESSAGE, AiOutputInvalid, AiQuotaExceeded, AiUnavailable
from app.ai.schemas import PodcastAnswer
from app.audio.stt import ALLOWED_MIME
from app.checkpoint import check_limit
from app.errors import ApiError, not_found
from app.learning import profile
from app.logs import get_logger, log
from app.services import Services
from app.store import now
from app.tldr import podcast

logger = get_logger(__name__)

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
MAX_QUESTION_CHARS = 1000
HISTORY_TURNS = 8
TRANSCRIPT_CHARS = 14000
SOURCE_CHARS = 8000


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _msg(mid: str, m: dict[str, Any]) -> dict[str, Any]:
    keep = ("role", "content", "inputMode", "transcriptionStatus", "grounded", "clarifyingQuestion", "audioUrl", "replyTo")
    out = {"id": mid, **{k: m.get(k) for k in keep if k in m}}
    out["createdAt"] = _iso(m.get("createdAt"))
    return out


def history(svc: Services, uid: str, pid: str, limit: int = 100) -> list[dict[str, Any]]:
    podcast.load_for(svc, pid, uid)  # not found for another user's private podcast
    snaps = svc.paths.podcast_chat(uid, pid).order_by("createdAt").limit(limit).stream()
    return [_msg(s.id, s.to_dict() or {}) for s in snaps]


def _ai_error(exc: Exception) -> ApiError:
    if isinstance(exc, AiQuotaExceeded):
        return ApiError(429, "ai_quota", QUOTA_MESSAGE)
    if isinstance(exc, AiUnavailable):
        return ApiError(503, "ai_unavailable", "The assistant is unavailable right now. Try again shortly.")
    return ApiError(502, "ai_invalid", "The assistant returned an answer we could not check. Try again.")


def _context(svc: Services, uid: str, pid: str, st: dict[str, Any], key: str, question: str) -> str:
    transcript = "\n".join(f"{ln['speaker']}: {ln['text']}" for ln in st.get("lines") or [])[:TRANSCRIPT_CHARS]
    source = (svc.blobs.get_text(f"{key}.source.txt") or "")[:SOURCE_CHARS]
    meta = f"Podcast: {st.get('title') or ''} (section: {st.get('sectionName') or ''}; id {pid})"
    parts = [meta, f"<source>\nTRANSCRIPT:\n{transcript}\n\nSOURCE MATERIAL:\n{source}\n</source>"]
    try:  # verified understanding only pitches the level; unknown stays unknown
        lc = profile.build_context(svc, uid, [], f"{st.get('title') or ''} {question} {transcript[:3000]}")
        if lc:
            parts.append(prompts.learning_block(lc))
        pref = svc.paths.podcast_meta(uid, pid).get().to_dict() or {}
        if pref.get("preferences"):
            parts.append(f"<listener_preferences>\n{pref['preferences']}\n</listener_preferences>")
    except Exception as exc:
        log(logger, logging.WARNING, "chat_profile_lookup_failed", error=type(exc).__name__)
    return "\n\n".join(parts)


def answer(svc: Services, uid: str, pid: str, text: str, client_id: str, input_mode: str = "text") -> dict[str, Any]:
    """One question and its answer. `client_id` makes a retry or double submit return the stored exchange instead of
    asking the model again. If the question was stored but the model failed, the retry only runs the model."""
    if not CLIENT_ID_RE.match(client_id or ""):
        raise ApiError(422, "invalid", "Invalid request id")
    question = " ".join((text or "").split())
    if not question:
        raise ApiError(422, "empty_question", "Type or say a question first.")
    question = question[:MAX_QUESTION_CHARS]
    st, key = podcast.load_for(svc, pid, uid)
    if not st.get("lines"):
        raise ApiError(409, "not_ready", "The podcast script is not ready yet.")
    col = svc.paths.podcast_chat(uid, pid)
    uref, aref = col.document(f"u_{client_id}"), col.document(f"a_{client_id}")
    existing_a = aref.get()
    if existing_a.exists:
        return {"user": _msg(uref.id, uref.get().to_dict() or {}), "assistant": _msg(aref.id, existing_a.to_dict() or {})}
    check_limit(svc, uid, "ask", svc.settings.daily_limit_ask)
    at = now()
    if not uref.get().exists:
        uref.set({"role": "user", "content": question, "inputMode": input_mode, "createdAt": at,
                  "transcriptionStatus": "confirmed" if input_mode == "voice" else None})
    prior = [m for m in history(svc, uid, pid, 60) if m["id"] not in (uref.id, aref.id)][-HISTORY_TURNS:]
    convo = "\n".join(f"{'Listener' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in prior)
    prompt = (f"{_context(svc, uid, pid, st, key, question)}\n\nCONVERSATION SO FAR:\n{convo or '(none)'}\n\n"
              f"<question>\n{question}\n</question>")
    try:
        out = svc.llm.generate_json("flash", prompts.PODCAST_CHAT_SYSTEM, prompt, PodcastAnswer, temperature=0.3, max_output_tokens=900)
    except (AiUnavailable, AiOutputInvalid) as exc:
        log(logger, logging.WARNING, "podcast_chat_failed", podcastId=pid, error=type(exc).__name__)
        raise _ai_error(exc)
    reply = {"role": "assistant", "content": out.answer.strip(), "grounded": out.grounded,
             "clarifyingQuestion": out.clarifying_question, "replyTo": uref.id, "createdAt": now(), "audioPath": None}
    aref.set(reply)
    profile.record_signal(svc, uid, pid, question)  # an unverified hint, kept apart from mastery
    log(logger, logging.INFO, "podcast_chat_answered", podcastId=pid, inputMode=input_mode, chars=len(out.answer), grounded=out.grounded)
    return {"user": _msg(uref.id, uref.get().to_dict() or {}), "assistant": _msg(aref.id, reply)}


def answer_audio(svc: Services, uid: str, pid: str, mid: str) -> dict[str, Any]:
    """Speaks a stored answer with Gemini TTS (cached). Re-running this never re-runs the language model."""
    from app.ai.prompts import ANSWER_VOICE_STYLE
    from app.pipeline.audio_stage import speak_text

    podcast.load_for(svc, pid, uid)
    ref = svc.paths.podcast_chat(uid, pid).document(mid)
    m = ref.get().to_dict()
    if not m or m.get("role") != "assistant":
        raise not_found("Answer")
    text = (m.get("content") or "").strip()
    if not text:
        raise ApiError(422, "nothing_to_say", "This answer has no text to read out.")
    check_limit(svc, uid, "tts", svc.settings.daily_limit_tts)
    path = f"audio/{uid}/podcast/{pid}/{mid}.wav"
    try:
        out = speak_text(svc, uid, path, text, ANSWER_VOICE_STYLE)
    except (AiUnavailable, AiOutputInvalid) as exc:
        log(logger, logging.WARNING, "podcast_chat_tts_failed", podcastId=pid, error=type(exc).__name__)
        raise ApiError(503, "tts_unavailable", "The spoken answer could not be made. The text answer is still here; try again shortly.")
    ref.update({"audioPath": path})
    return out


def transcribe(svc: Services, uid: str, pid: str, audio: bytes, mime_type: str) -> dict[str, Any]:
    """Voice -> text with Gemini. Nothing is sent to the answering model here: the listener reviews the text first.
    The audio itself is not stored. Empty or failed recognition is an error, never an empty question."""
    podcast.load_for(svc, pid, uid)
    mime = (mime_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIME:
        raise ApiError(415, "unsupported_audio", "That audio format is not supported.")
    if not audio or len(audio) < 800:
        raise ApiError(422, "empty_audio", "The recording was empty. Try again or type your question.")
    if len(audio) > svc.settings.stt_max_bytes:
        raise ApiError(413, "audio_too_large", "That recording is too long. Keep questions under about 40 seconds.")
    if svc.stt is None:
        raise ApiError(503, "stt_unavailable", "Voice questions are not available right now. Type your question instead.")
    check_limit(svc, uid, "stt", svc.settings.daily_limit_stt)
    try:
        raw = svc.stt.transcribe(audio, mime)
    except (AiUnavailable, AiOutputInvalid) as exc:
        log(logger, logging.WARNING, "stt_failed", podcastId=pid, error=type(exc).__name__)
        raise ApiError(503, "stt_unavailable", "We could not transcribe that. Try again or type your question.")
    text = " ".join(raw.split())[:MAX_QUESTION_CHARS]
    if len(re.findall(r"\w", text)) < 2:
        raise ApiError(422, "empty_transcription", "We could not hear a question in that recording. Try again or type it.")
    return {"text": text}

