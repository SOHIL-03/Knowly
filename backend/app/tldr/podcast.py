"""Interview-style podcast of one TLDR story: one host asks the questions, the other answers.

For the story's linked article the service fetches the full text and builds a coverage manifest of every
important point and fact (app/tldr/coverage.py); the AI writes the question and answer script part by
part, the script is validated against the manifest and only the gaps are regenerated, then Gemini
multi-speaker TTS speaks it chunk by chunk (app/tldr/audio.py). When the article
cannot be fetched (paywall, blocked, too short) it falls back to TLDR's own summary of the story and
says so (`basis: "summary"`). Results are cached per story in blob storage, so each story is generated
once and then served to everyone.

State lives in `podcasts/tldr/{category}/{date}/{storyId}.json` (status none | processing | ready |
failed), audio in `.wav` next to it.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta

from app.ai.client import QUOTA_MESSAGE, AiOutputInvalid, AiQuotaExceeded, AiUnavailable
from app.audio.tts import Turn, line_timings
from app.logs import Timer, exc_fields, get_logger, log
from app.pipeline.enrich import fetch_article
from app.services import Services
from app.store import now
from app.tldr import coverage
from app.tldr.audio import synthesize_all
from app.tldr.scraper import Issue, Story, TldrError, scraper

logger = get_logger(__name__)
PREFIX = "podcasts/tldr"
STALE = timedelta(minutes=15)  # a "processing" state older than this is treated as a dead worker


def _path(category: str, date: str, sid: str, ext: str) -> str:
    return f"{PREFIX}/{category}/{date}/{sid}.{ext}"


def _save(svc: Services, category: str, date: str, sid: str, **state) -> None:
    state["updatedAt"] = now().isoformat()
    svc.blobs.put_text(_path(category, date, sid, "json"), json.dumps(state, ensure_ascii=False), "application/json")


def _load(svc: Services, category: str, date: str, sid: str) -> dict:
    raw = svc.blobs.get_text(_path(category, date, sid, "json"))
    return json.loads(raw) if raw else {"status": "none"}


def _is_live(state: dict) -> bool:
    started = state.get("updatedAt")
    return state.get("status") == "processing" and bool(started) and now() - datetime.fromisoformat(started) < STALE


def find_story(issue: Issue, sid: str) -> Story:
    for sec in issue.sections:
        for s in sec.stories:
            if s.id == sid and not s.sponsored:
                return s
    raise TldrError("That story is not in this issue")


def view(svc: Services, category: str, date: str, sid: str) -> dict:
    st = _load(svc, category, date, sid)
    if st["status"] == "processing" and not _is_live(st):
        return {"status": "failed", "message": "Generation stopped unexpectedly. Try again."}
    out = {k: st.get(k) for k in ("status", "phase", "message", "title", "basis", "hosts", "lines", "takeaways", "durationSec",
                                  "report")}
    if st["status"] == "ready":
        out["audioUrl"] = svc.blobs.read_url(st["audioPath"])
    return out


def request(svc: Services, category: str, date: str, sid: str) -> dict:
    """Starts generation unless the podcast already exists or is being made. Returns the current view."""
    find_story(scraper.get_issue(date, category), sid)  # only real stories of real TLDR issues
    st = _load(svc, category, date, sid)
    if st["status"] == "ready" or _is_live(st):
        return view(svc, category, date, sid)
    _save(svc, category, date, sid, status="processing")
    svc.dispatcher.task("tldr-podcast", {"category": category, "date": date, "storyId": sid},
                        dedupe_key=f"tldr-podcast-{category}-{date}-{sid}-{now().isoformat()}")
    return {"status": "processing"}


def article_text(svc: Services, story: Story) -> tuple[str, str]:
    """(text, basis): the full linked article when it can be fetched, else TLDR's summary of it."""
    s = svc.settings
    if story.url.startswith(("http://", "https://")):
        res = fetch_article(story.url, timeout=s.enrich_timeout_seconds, max_chars=s.enrich_max_chars, client=svc.http)
        if res.text:
            return res.text, "article"
        log(logger, logging.INFO, "tldr_article_fallback", storyId=story.id, reason=res.reason)
    return story.summary, "summary"


def generate(svc: Services, category: str, date: str, sid: str) -> str:
    """Task handler: article -> manifest -> script -> validation/repair -> script preview -> audio.
    Failures are recorded (with the script and report when they exist), not raised."""
    if _load(svc, category, date, sid).get("status") == "ready":
        return "skipped"
    rid = f"{category}-{date}-{sid}-{uuid.uuid4().hex[:6]}"
    timer = Timer()
    s = svc.settings
    hosts = {"A": s.tts_host_a_name, "B": s.tts_host_b_name}
    stats = coverage.Stats()
    draft: dict = {}  # script, report and title once they exist, kept on failure for inspection
    try:
        issue = scraper.get_issue(date, category)
        story = find_story(issue, sid)
        text, basis = article_text(svc, story)
        log(logger, logging.INFO, "tldr_podcast_start", podcastId=rid, category=category, date=date, storyId=sid, articles=1,
            stories=1, basis=basis, inputChars=len(text), inputWords=len(text.split()), model=getattr(s, "openai_model", None),
            maxRepairs=s.tldr_max_repairs)
        ctx = coverage.Ctx(svc.llm, story.title, text, hosts, coverage.build_manifest(svc.llm, story.title, text, stats, rid),
                           stats, rid)
        outline = coverage.plan_outline(ctx.points)
        ctx.outline_titles = [" & ".join(ctx.by_id[i]["topic"] for i in ids[:2]) for ids in outline]
        log(logger, logging.INFO, "tldr_manifest", podcastId=rid, points=len(ctx.points), parts=len(outline),
            singleCall=len(outline) == 1, estWords=sum(coverage.est_words(p) for p in ctx.points))
        parts = coverage.generate_parts(ctx, outline)
        parts, v = coverage.repair(ctx, parts, coverage.validate(ctx, parts), s.tldr_max_repairs)
        lines = coverage.flat_lines(parts)
        report = coverage.report_for(ctx.points, parts, v, stats, {
            "podcastId": rid, "basis": basis, "sourceChars": len(text), "sourceWords": len(text.split()),
            "singleCall": len(outline) == 1, "ttsCalls": 0})
        title = next((p.title for p in reversed(parts) if p.title), story.title)
        takeaways = next((p.takeaways for p in reversed(parts) if p.takeaways), [])
        draft = {"title": title, "basis": basis, "hosts": hosts, "takeaways": takeaways, "report": report,
                 "lines": [{"speaker": hosts[ln.speaker], "role": "question" if ln.speaker == "A" else "answer", "text": ln.text,
                            "ids": ln.item_ids} for ln in lines]}
        log(logger, logging.INFO, "tldr_script_done", podcastId=rid, status=report["status"], points=report["expectedPoints"],
            covered=report["coveredPoints"], missingIds=[m["id"] for m in report["missing"]], repairs=stats.repairs,
            words=report["words"], chars=report["chars"], llmCalls=stats.llm_calls, finishReasons=stats.finish,
            tokensIn=stats.tokens_in, tokensOut=stats.tokens_out, durationMs=timer.ms())
        _save(svc, category, date, sid, status="processing", phase="audio", **draft)  # script preview before any TTS

        turns = [Turn(speaker=hosts[ln.speaker], text=ln.text, style=ln.style) for ln in lines]
        audio = synthesize_all(svc.tts, turns, {hosts["A"]: s.tts_host_a_voice, hosts["B"]: s.tts_host_b_voice},
                               chunk_chars=getattr(s, "tts_chunk_chars", 2500), rid=rid)
        audio_path = _path(category, date, sid, "wav")
        svc.blobs.put_bytes(audio_path, audio.wav, "audio/wav")
        report |= {"ttsCalls": audio.tts_calls, "ttsChunkChars": audio.chunk_chars, "audioChunks": len(audio.chunk_chars),
                   "audioSeconds": round(audio.duration_sec, 1), "audioBytes": len(audio.wav)}
        draft["report"] = report
    except (AiUnavailable, AiOutputInvalid, TldrError, coverage.ScriptIncomplete) as exc:
        quota = isinstance(exc, AiQuotaExceeded)
        message = QUOTA_MESSAGE if quota else (str(exc) if isinstance(exc, TldrError | coverage.ScriptIncomplete)
                                               else "The podcast could not be made right now. Try again later.")
        _save(svc, category, date, sid, status="failed", message=message, **draft)
        log(logger, logging.WARNING, "tldr_podcast_failed", podcastId=rid, category=category, date=date, storyId=sid,
            llmCalls=stats.llm_calls, durationMs=timer.ms(), **exc_fields(exc, frames=1))
        return "failed"
    except Exception as exc:
        _save(svc, category, date, sid, status="failed", message="The podcast could not be made right now. Try again later.", **draft)
        log(logger, logging.ERROR, "tldr_podcast_crashed", podcastId=rid, category=category, date=date, storyId=sid,
            durationMs=timer.ms(), **exc_fields(exc))
        raise
    _save(svc, category, date, sid, status="ready", audioPath=audio_path, durationSec=round(audio.duration_sec),
          lineTimings=line_timings(turns, audio.parts), **draft)
    log(logger, logging.INFO, "tldr_podcast_ready", podcastId=rid, coverage=report["status"], lines=report["lines"],
        words=report["words"], llmCalls=stats.llm_calls, ttsCalls=audio.tts_calls, audioChunks=len(audio.chunk_chars),
        seconds=round(audio.duration_sec), bytes=len(audio.wav), durationMs=timer.ms())
    return "ok"
