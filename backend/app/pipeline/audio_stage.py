"""B9 Audio: each segment's grounded script is spoken with Gemini multi-speaker TTS (two hosts),
or single-speaker TTS for the "brief" style. The script was validated and fact-checked in B8, and
TTS reads text verbatim, so the audio says what the script says.

Segment audioStatus: pending -> processing -> ok | failed (text stays available; Retry audio).
"""

from __future__ import annotations

import logging

from google.cloud import firestore

from app.ai.client import QUOTA_MESSAGE, AiQuotaExceeded, AiUnavailable
from app.audio.tts import Turn, line_timings
from app.logs import Timer, exc_fields, get_logger, log
from app.services import Services
from app.store import now, stable_id

logger = get_logger(__name__)
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 60
PROCESSING_STALE_SECONDS = 600


def voices_for(svc: Services, style: str) -> dict[str, str]:
    s = svc.settings
    if style == "brief":
        return {s.tts_host_a_name: s.tts_host_a_voice}
    return {s.tts_host_a_name: s.tts_host_a_voice, s.tts_host_b_name: s.tts_host_b_voice}


def synthesize_segment(svc: Services, uid: str, bid: str, sid: str) -> str:
    ref = svc.paths.segment(uid, bid, sid)
    timer = Timer()
    if not _claim(svc, ref):
        log(logger, logging.INFO, "segment_audio_skipped", uid=uid, briefingId=bid, segId=sid,
            reason="already ok, missing, or another worker is processing")
        return "skipped"
    seg = ref.get().to_dict() or {}
    lines = seg.get("script") or []
    if not lines:
        log(logger, logging.ERROR, "segment_audio_no_script", uid=uid, briefingId=bid, segId=sid)
        ref.update({"audioStatus": "failed", "audioMessage": "This segment has no script yet."})
        return "failed"
    style = seg.get("style", "twoHost")
    voices = voices_for(svc, style)
    names = list(voices)
    turns = [Turn(speaker=names[0] if style == "brief" or line["speaker"] == "A" else names[-1],
                  text=line["text"], style=line.get("style")) for line in lines]
    attempts = int(seg.get("audioAttempts", 0)) + 1
    log(logger, logging.INFO, "segment_audio_start", uid=uid, briefingId=bid, segId=sid, attempt=attempts, style=style,
        lines=len(lines), chars=sum(len(t.text) for t in turns), voices=list(voices.values()))
    try:
        speech = svc.tts.synthesize(turns, voices)
    except AiUnavailable as exc:
        quota = isinstance(exc, AiQuotaExceeded)  # retrying a used-up quota cannot succeed
        retry = attempts < MAX_ATTEMPTS and not quota
        failed_message = (f"{QUOTA_MESSAGE} The text version is below." if quota
                          else "Audio could not be made right now. The text version is below.")
        ref.update({
            "audioStatus": "pending" if retry else "failed",
            "audioAttempts": attempts,
            "audioMessage": None if retry else failed_message,
            "audioError": str(exc)[:200],
            "audioUpdatedAt": now(),
        })
        if retry:  # retry once with backoff (B9)
            svc.dispatcher.task("segment-audio", {"uid": uid, "briefingId": bid, "segId": sid},
                                delay_seconds=RETRY_DELAY_SECONDS, dedupe_key=f"audio-{stable_id(uid, bid, sid)}-{attempts}")
        log(logger, logging.WARNING, "segment_audio_failed", uid=uid, briefingId=bid, segId=sid, attempt=attempts,
            retrying=retry, quota=quota, category=exc.category, durationMs=timer.ms(), **exc_fields(exc, frames=1))
        return "retrying" if retry else "failed"

    path = f"audio/{uid}/{bid}/{sid}.wav"
    svc.blobs.put_bytes(path, speech.wav, "audio/wav")
    ref.update({
        "audioPath": path,
        "audioStatus": "ok",
        "audioAttempts": attempts,
        "audioMessage": None,
        "audioError": None,
        "durationSec": round(speech.duration_sec),
        "lineTimings": line_timings(turns, speech.parts),
        "voices": voices,
        "audioUpdatedAt": now(),
    })
    _refresh_briefing_audio(svc, uid, bid)
    log(logger, logging.INFO, "segment_audio_ready", uid=uid, briefingId=bid, segId=sid, seconds=round(speech.duration_sec),
        bytes=len(speech.wav), path=path, durationMs=timer.ms())
    return "ok"


def _claim(svc: Services, ref) -> bool:
    """Only one worker synthesizes a segment at a time; duplicate task deliveries exit early."""
    transaction = svc.db.transaction()

    @firestore.transactional
    def txn(tx) -> bool:
        data = ref.get(transaction=tx).to_dict()
        if not data or data.get("audioStatus") == "ok":
            return False
        started = data.get("audioUpdatedAt")
        if data.get("audioStatus") == "processing" and started and (now() - started).total_seconds() < PROCESSING_STALE_SECONDS:
            return False
        tx.update(ref, {"audioStatus": "processing", "audioUpdatedAt": now()})
        return True

    return txn(transaction)


def retry_segment_audio(svc: Services, uid: str, bid: str, sid: str) -> None:
    svc.paths.segment(uid, bid, sid).update({"audioStatus": "pending", "audioAttempts": 0, "audioMessage": None,
                                             "audioUpdatedAt": None})
    svc.dispatcher.task("segment-audio", {"uid": uid, "briefingId": bid, "segId": sid},
                        dedupe_key=f"audio-retry-{stable_id(uid, bid, sid, now().isoformat())}")


def approve_segment_audio(svc: Services, uid: str, bid: str, sid: str, texts: list[str] | None = None) -> bool:
    """Review workflow: the user approved this segment's script, so start the existing TTS for it.

    `texts` are the reviewed line texts. When any differ from the generated script, the stored script
    is replaced by exactly those texts (the original is kept in scriptOriginal and scriptEdited is
    set), so TTS reads what was approved. Returns whether the script was edited."""
    ref = svc.paths.segment(uid, bid, sid)
    seg = ref.get().to_dict() or {}
    lines = seg.get("script") or []
    edited = texts is not None and any(t != line["text"] for t, line in zip(texts, lines, strict=True))
    update: dict = {"audioStatus": "pending", "audioAttempts": 0, "audioMessage": None, "audioUpdatedAt": None}
    if edited:
        update |= {"script": [{**line, "text": t} for t, line in zip(texts, lines, strict=True)],
                   "scriptEdited": True, "scriptOriginal": seg.get("scriptOriginal") or lines}
    ref.update(update)
    svc.dispatcher.task("segment-audio", {"uid": uid, "briefingId": bid, "segId": sid},
                        dedupe_key=f"audio-approve-{stable_id(uid, bid, sid, now().isoformat())}")
    log(logger, logging.INFO, "segment_audio_approved", uid=uid, briefingId=bid, segId=sid, edited=edited, lines=len(lines))
    return edited


def _refresh_briefing_audio(svc: Services, uid: str, bid: str) -> None:
    segs = [s.to_dict() or {} for s in svc.paths.segments(uid, bid).stream()]
    svc.paths.briefing(uid, bid).update({
        "audioReadyCount": sum(1 for s in segs if s.get("audioStatus") == "ok"),
        "totalDurationSec": sum(int(s.get("durationSec") or 0) for s in segs if not s.get("removedByUser")),
        "updatedAt": firestore.SERVER_TIMESTAMP,
    })


# --- Spoken answers (single-speaker TTS) ----------------------------------------------------

def speak_text(svc: Services, uid: str, path: str, text: str, style: str) -> dict:
    """Synthesizes text once and caches it at `path`; returns {url, durationSec}."""
    if svc.blobs.exists(path):
        log(logger, logging.INFO, "spoken_text_cache_hit", uid=uid, path=path)
        return {"url": svc.blobs.read_url(path)}
    s = svc.settings
    log(logger, logging.INFO, "spoken_text_synthesize", uid=uid, path=path, chars=len(text), style=style)
    speech = svc.tts.synthesize([Turn(speaker="", text=text, style=style)], {"Tutor": s.tts_answer_voice})
    svc.blobs.put_bytes(path, speech.wav, "audio/wav")
    return {"url": svc.blobs.read_url(path), "durationSec": round(speech.duration_sec, 1)}
