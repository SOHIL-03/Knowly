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
from app.logs import get_logger, log
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
    if not _claim(svc, ref):
        return "skipped"
    seg = ref.get().to_dict() or {}
    lines = seg.get("script") or []
    if not lines:
        ref.update({"audioStatus": "failed", "audioMessage": "This segment has no script yet."})
        return "failed"
    style = seg.get("style", "twoHost")
    voices = voices_for(svc, style)
    names = list(voices)
    turns = [Turn(speaker=names[0] if style == "brief" or line["speaker"] == "A" else names[-1],
                  text=line["text"], style=line.get("style")) for line in lines]
    attempts = int(seg.get("audioAttempts", 0)) + 1
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
        log(logger, logging.WARNING, "segment_audio_failed", uid=uid, briefingId=bid, segId=sid, attempt=attempts)
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
    log(logger, logging.INFO, "segment_audio_ready", uid=uid, briefingId=bid, segId=sid, seconds=round(speech.duration_sec))
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
        return {"url": svc.blobs.read_url(path)}
    s = svc.settings
    speech = svc.tts.synthesize([Turn(speaker="", text=text, style=style)], {"Tutor": s.tts_answer_voice})
    svc.blobs.put_bytes(path, speech.wav, "audio/wav")
    return {"url": svc.blobs.read_url(path), "durationSec": round(speech.duration_sec, 1)}
