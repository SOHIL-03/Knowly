"""Interview-style podcast of one TLDR section: one host asks the questions, the other answers.

TLDR groups stories into sections ("Big Tech & Startups", "Science & Futuristic Technology", ...). One podcast
covers one section: every non-sponsored story in it, one after another. For each story the service fetches the
linked article and builds a coverage manifest of its important points and facts (app/tldr/coverage.py); the AI
writes the question and answer script part by part (a part never spans two stories and is written from that
story's own text), the script is validated against all the manifests and only the gaps are regenerated, then
Gemini multi-speaker TTS speaks it chunk by chunk (app/tldr/audio.py). A story whose article cannot be fetched
(paywall, blocked, too short) falls back to TLDR's own summary of it and the podcast says so (`basis`: "article",
"summary" or "mixed"; `sources` lists each story's basis). Results are cached per section in blob storage, so each
section is generated once and then served to everyone.

State lives in `podcasts/tldr/{category}/{date}/{sectionId}.json` (status none | processing | ready | failed),
audio in `.wav` next to it. `sectionId` is the slug of the section name (see scraper.slugify).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from google.cloud import firestore

from app.ai.client import QUOTA_MESSAGE, AiOutputInvalid, AiQuotaExceeded, AiUnavailable
from app.audio.tts import Turn, line_timings
from app.logs import Timer, exc_fields, get_logger, log
from app.pipeline.enrich import fetch_article
from app.services import Services
from app.store import now
from app.tldr import coverage
from app.tldr.audio import synthesize_all
from app.tldr.scraper import Issue, Section, Story, TldrError, scraper

logger = get_logger(__name__)
PREFIX = "podcasts/tldr"
STALE = timedelta(minutes=15)  # a "processing" state older than this is treated as a dead worker
DEFAULT_MAX_STORIES = 8  # stories read per section; TLDR sections hold two to five
LOCKS = "tldrPodcastLocks"  # server-only: one document per podcast being generated
MAX_PREFERENCE_CHARS = 1000  # longest listener preference text accepted


def _path(category: str, date: str, sid: str, ext: str) -> str:
    return f"{PREFIX}/{category}/{date}/{sid}.{ext}"


def _save(svc: Services, category: str, date: str, sid: str, **state) -> None:
    state["updatedAt"] = now().isoformat()
    svc.blobs.put_text(_path(category, date, sid, "json"), json.dumps(state, ensure_ascii=False), "application/json")


def _load(svc: Services, category: str, date: str, sid: str) -> dict:
    raw = svc.blobs.get_text(_path(category, date, sid, "json"))
    return json.loads(raw) if raw else {"status": "none"}


def _lock_ref(svc: Services, category: str, date: str, key: str):
    return svc.db.collection(LOCKS).document(f"{category}__{date}__{key}")


def _claim(svc: Services, category: str, date: str, key: str) -> bool:
    """Atomically takes the right to generate this podcast. Two requests that arrive together (a double click, two tabs)
    both see 'not started'; only one wins here, so the podcast is never generated twice at once. A claim older than STALE
    belongs to a dead worker and can be taken over."""
    if getattr(svc, "db", None) is None:
        return True
    ref = _lock_ref(svc, category, date, key)
    tx = svc.db.transaction()

    @firestore.transactional
    def txn(t) -> bool:
        at = (ref.get(transaction=t).to_dict() or {}).get("at")
        if at and now() - at < STALE:
            return False
        t.set(ref, {"at": now()})
        return True

    try:
        return txn(tx)
    except Exception as exc:  # contended lock (ABORTED after retries): another request holds it, so it is the one generating
        log(logger, logging.WARNING, "podcast_claim_contended", error=type(exc).__name__)
        return False


def _release(svc: Services, category: str, date: str, key: str) -> None:
    if getattr(svc, "db", None) is not None:
        try:
            _lock_ref(svc, category, date, key).delete()
        except Exception as exc:
            log(logger, logging.WARNING, "podcast_lock_release_failed", error=type(exc).__name__)


def _is_live(state: dict) -> bool:
    started = state.get("updatedAt")
    return state.get("status") == "processing" and bool(started) and now() - datetime.fromisoformat(started) < STALE


def find_section(issue: Issue, sid: str) -> Section:
    """The section with this id, which must contain at least one story that is not a sponsor."""
    for sec in issue.sections:
        if sec.key == sid:
            if any(not s.sponsored for s in sec.stories):
                return sec
            break
    raise TldrError("That section is not in this issue")


def section_stories(svc: Services, section: Section) -> tuple[list[Story], int]:
    """The stories the podcast covers (sponsors never), and how many were left out by the per-section cap."""
    cap = max(1, getattr(svc.settings, "tldr_section_max_stories", DEFAULT_MAX_STORIES))
    stories = [s for s in section.stories if not s.sponsored]
    return stories[:cap], max(0, len(stories) - cap)


def clean_preferences(text: str | None) -> str | None:
    """None for no preferences; otherwise the text without control characters and surrounding blanks.
    Raises TldrError when text is given but blank, so an empty preference is never silently turned into the default."""
    if text is None:
        return None
    cleaned = " ".join("".join(c if c.isprintable() or c in "\n\t" else " " for c in text).split())
    if not cleaned:
        raise TldrError("Describe your preference, or choose No for the standard podcast")
    return cleaned[:MAX_PREFERENCE_CHARS]


def variant_key(sid: str, preferences: str | None, owner: str | None = None, rev: int = 0) -> str:
    """Storage key of a podcast. Personalized episodes are stored apart, so they never replace, or get served as, the shared
    default podcast of the section. Without a signed-in owner the variant is one per distinct preference text ("p…").
    For a signed-in owner it is private to them ("u…") and also depends on their learning revision, so an episode made
    before an assessment is not reused after it."""
    if owner:
        raw = "|".join([owner, (preferences or "").lower(), str(rev)])
        return f"{sid}.u{hashlib.sha256(raw.encode()).hexdigest()[:10]}"
    if not preferences:
        return sid
    return f"{sid}.p{hashlib.sha256(preferences.lower().encode()).hexdigest()[:10]}"


PID_RE = re.compile(r"^([a-z0-9]{1,30})\.(\d{4}-\d{2}-\d{2})\.([a-z0-9-]{1,50})(?:\.([pu][0-9a-f]{10}))?$")


def make_pid(category: str, date: str, sid: str, variant: str | None = None) -> str:
    """The persistent id of a podcast, used in /podcast/{id}: category.date.section[.variant]."""
    return f"{category}.{date}.{sid}" + (f".{variant}" if variant else "")


def parse_pid(pid: str) -> tuple[str, str, str, str | None]:
    m = PID_RE.match(pid or "")
    if not m:
        raise TldrError("That podcast was not found")
    return m.group(1), m.group(2), m.group(3), m.group(4)


def podcast_key(category: str, date: str, sid: str, variant: str | None) -> str:
    """Blob path prefix (no extension) of a podcast's files."""
    return f"{PREFIX}/{category}/{date}/{sid}" + (f".{variant}" if variant else "")


def _visible(st: dict, uid: str | None) -> bool:
    """A podcast made for one user is visible to that user only."""
    return not st.get("ownerUid") or st.get("ownerUid") == uid


def load_for(svc: Services, pid: str, uid: str | None) -> tuple[dict, str]:
    """(stored state, storage key) of a podcast by id. Unknown ids and other users' private podcasts are both 'not found'."""
    category, date, sid, variant = parse_pid(pid)
    st = _load(svc, category, date, f"{sid}.{variant}" if variant else sid)
    if st["status"] == "none" or not _visible(st, uid):
        raise TldrError("That podcast was not found")
    return st, podcast_key(category, date, sid, variant)


def view(svc: Services, category: str, date: str, sid: str, variant: str | None = None, uid: str | None = None) -> dict:
    pid = make_pid(category, date, sid, variant)
    st = _load(svc, category, date, f"{sid}.{variant}" if variant else sid)
    if not _visible(st, uid):
        return {"status": "none", "podcastId": pid}
    if st["status"] == "processing" and not _is_live(st):
        return {"status": "failed", "message": "Generation stopped unexpectedly. Try again.", "podcastId": pid}
    out = {k: st.get(k) for k in ("status", "phase", "message", "title", "sectionName", "basis", "sources", "hosts", "lines",
                                  "takeaways", "durationSec", "report", "lineTimings", "conceptsStatus")}
    out["podcastId"] = pid
    if variant:
        out["variant"] = variant
    if st["status"] == "ready":
        out["audioUrl"] = svc.blobs.read_url(st["audioPath"])
    return out


def request(svc: Services, category: str, date: str, sid: str, preferences: str | None = None, uid: str | None = None) -> dict:
    """Starts generation unless the podcast already exists or is being made. Returns the current view (with podcastId).
    With preferences, or for a signed-in user who has a verified learning profile, the episode is personalized and stored
    under its own `variant` (returned; pass it to view). `uid` is the verified caller, never a request field."""
    find_section(scraper.get_issue(date, category), sid)  # only real sections of real TLDR issues
    prefs = clean_preferences(preferences)
    owner, rev = None, 0
    if uid:
        try:
            from app.learning import profile

            rev = profile.revision(svc, uid)
        except Exception as exc:  # a lookup failure means no profile, never an invented one
            log(logger, logging.WARNING, "learning_lookup_failed", error=type(exc).__name__)
        if prefs or rev:
            owner = uid
    key = variant_key(sid, prefs, owner, rev)
    variant = key[len(sid) + 1:] if key != sid else None
    st = _load(svc, category, date, key)
    if st["status"] == "ready" or _is_live(st):
        return view(svc, category, date, sid, variant, uid)
    if not _claim(svc, category, date, key):  # someone else started it in the same moment
        return {"status": "processing", "podcastId": make_pid(category, date, sid, variant)} | ({"variant": variant} if variant else {})
    own = {"ownerUid": owner} if owner else {}
    _save(svc, category, date, key, status="processing", **own)
    if owner and prefs:
        svc.paths.podcast_meta(owner, make_pid(category, date, sid, variant)).set({"preferences": prefs, "createdAt": now()})
    payload = {"category": category, "date": date, "sectionId": sid} | ({"preferences": prefs} if prefs else {}) | ({"uid": owner, "variant": variant} if owner else {})
    svc.dispatcher.task("tldr-podcast", payload, dedupe_key=f"tldr-podcast-{category}-{date}-{key}-{now().isoformat()}")
    return {"status": "processing", "podcastId": make_pid(category, date, sid, variant)} | ({"variant": variant} if variant else {})


def article_text(svc: Services, story: Story) -> tuple[str, str]:
    """(text, basis): the full linked article when it can be fetched, else TLDR's summary of it."""
    s = svc.settings
    if story.url.startswith(("http://", "https://")):
        res = fetch_article(story.url, timeout=s.enrich_timeout_seconds, max_chars=s.enrich_max_chars, client=svc.http)
        if res.text:
            return res.text, "article"
        log(logger, logging.INFO, "tldr_article_fallback", storyId=story.id, reason=res.reason)
    return story.summary, "summary"


def overall_basis(bases: list[str]) -> str:
    return bases[0] if len(set(bases)) == 1 else "mixed"


def generate(svc: Services, category: str, date: str, sid: str, preferences: str | None = None, uid: str | None = None,
             variant: str | None = None) -> str:
    """Task handler. Runs the generation and always releases the claim taken by request()."""
    key = f"{sid}.{variant}" if variant else variant_key(sid, preferences)
    try:
        return _generate(svc, category, date, sid, preferences, uid, variant)
    finally:
        _release(svc, category, date, key)


def _generate(svc: Services, category: str, date: str, sid: str, preferences: str | None = None, uid: str | None = None,
              variant: str | None = None) -> str:
    """Articles -> manifests -> script -> validation/repair -> script preview -> audio. articles -> manifests -> script -> validation/repair -> script preview -> audio.
    Failures are recorded (with the script and report when they exist), not raised.
    `preferences` personalizes the questions and answers; the episode is then stored under its own key."""
    section_id, preferences = sid, (preferences or None)
    sid = f"{section_id}.{variant}" if variant else variant_key(section_id, preferences)  # storage key from here on
    own = {"ownerUid": uid} if uid else {}
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
        section = find_section(issue, section_id)
        stories, omitted = section_stories(svc, section)
        with ThreadPoolExecutor(max_workers=4) as pool:  # the articles are independent
            fetched = list(pool.map(lambda st: article_text(svc, st), stories))
        sources = [{"id": st.id, "title": st.title, "url": st.url, "basis": basis} for st, (_, basis) in zip(stories, fetched, strict=True)]
        basis = overall_basis([x["basis"] for x in sources])
        blocks = {st.id: f"=== Article [{st.id}]: {st.title} ===\n{text}" for st, (text, _) in zip(stories, fetched, strict=True)}
        text = "\n\n".join(blocks.values())
        log(logger, logging.INFO, "tldr_podcast_start", podcastId=rid, category=category, date=date, sectionId=sid, articles=len(stories),
            omittedStories=omitted, personalized=bool(preferences or uid), preferenceChars=len(preferences or ""), basis=basis, bases={x["id"]: x["basis"] for x in sources}, inputChars=len(text),
            inputWords=len(text.split()), model=getattr(s, "openai_model", None), maxRepairs=s.tldr_max_repairs)

        points: list[dict] = []
        for st, (body, _) in zip(stories, fetched, strict=True):
            points += coverage.build_manifest(svc.llm, st.title, body, stats, rid, offset=len(points), source=st.id)
        learning = None
        if uid:  # read from the database for the verified caller, never from the request; a failure means no profile
            try:
                from app.learning import profile

                learning = profile.build_context(svc, uid, [p["topic"] for p in points],
                                                 " ".join(f"{p['topic']} {p['key_points']}" for p in points))
            except Exception as exc:
                log(logger, logging.WARNING, "learning_context_failed", podcastId=rid, error=type(exc).__name__)
        ctx = coverage.Ctx(svc.llm, section.name, text, hosts, points, stats, rid, sources=blocks,
                           source_titles={st.id: st.title for st in stories}, preferences=preferences, learning=learning)
        outline = coverage.plan_outline(ctx.points)
        ctx.outline_titles = [" & ".join(ctx.by_id[i]["topic"] for i in ids[:2]) for ids in outline]
        log(logger, logging.INFO, "tldr_manifest", podcastId=rid, points=len(ctx.points), parts=len(outline),
            singleCall=len(outline) == 1, estWords=sum(coverage.est_words(p) for p in ctx.points),
            pointsPerStory={st.id: sum(1 for p in points if p["source"] == st.id) for st in stories})
        parts = coverage.generate_parts(ctx, outline)
        parts, v = coverage.repair(ctx, parts, coverage.validate(ctx, parts), s.tldr_max_repairs)
        lines = coverage.flat_lines(parts)
        report = coverage.report_for(ctx.points, parts, v, stats, {
            "podcastId": rid, "basis": basis, "stories": len(stories), "omittedStories": omitted, "sourceChars": len(text),
            "sourceWords": len(text.split()), "singleCall": len(outline) == 1, "ttsCalls": 0})
        title = next((p.title for p in reversed(parts) if p.title), section.name)
        takeaways = next((p.takeaways for p in reversed(parts) if p.takeaways), [])
        draft = {"title": title, "sectionName": section.name, "basis": basis, "sources": sources, "hosts": hosts,
                 "takeaways": takeaways, "report": report,
                 "lines": [{"speaker": hosts[ln.speaker], "role": "question" if ln.speaker == "A" else "answer", "text": ln.text,
                            "ids": ln.item_ids} for ln in lines]}
        log(logger, logging.INFO, "tldr_script_done", podcastId=rid, status=report["status"], points=report["expectedPoints"],
            covered=report["coveredPoints"], missingIds=[m["id"] for m in report["missing"]], repairs=stats.repairs,
            words=report["words"], chars=report["chars"], llmCalls=stats.llm_calls, finishReasons=stats.finish,
            tokensIn=stats.tokens_in, tokensOut=stats.tokens_out, durationMs=timer.ms())
        # Optional stage: concepts for assessments. A failure is recorded and never stops the podcast or its audio.
        from app.learning import concepts as concept_store

        pkey = podcast_key(category, date, section_id, variant)
        svc.blobs.put_text(f"{pkey}.source.txt", text[:20000], "text/plain")
        draft["conceptsStatus"] = concept_store.extract_and_save(
            svc, pkey, make_pid(category, date, section_id, variant), section_id, draft["lines"], text, stats.hook(rid, "concepts"))
        _save(svc, category, date, sid, status="processing", phase="audio", **own, **draft)  # script preview before any TTS

        turns = [Turn(speaker=hosts[ln.speaker], text=ln.text, style=ln.style) for ln in lines]
        audio = synthesize_all(svc.tts, turns, {hosts["A"]: s.tts_host_a_voice, hosts["B"]: s.tts_host_b_voice},
                               chunk_chars=getattr(s, "tts_chunk_chars", 2500), rid=rid,
                               concurrency=getattr(s, "tts_concurrency", 1))
        audio_path = _path(category, date, sid, "wav")
        svc.blobs.put_bytes(audio_path, audio.wav, "audio/wav")
        report |= {"ttsCalls": audio.tts_calls, "ttsChunkChars": audio.chunk_chars, "audioChunks": len(audio.chunk_chars),
                   "audioSeconds": round(audio.duration_sec, 1), "audioBytes": len(audio.wav)}
        draft["report"] = report
    except (AiUnavailable, AiOutputInvalid, TldrError, coverage.ScriptIncomplete) as exc:
        quota = isinstance(exc, AiQuotaExceeded)
        message = QUOTA_MESSAGE if quota else (str(exc) if isinstance(exc, TldrError | coverage.ScriptIncomplete)
                                               else "The podcast could not be made right now. Try again later.")
        _save(svc, category, date, sid, status="failed", message=message, **own, **draft)
        log(logger, logging.WARNING, "tldr_podcast_failed", podcastId=rid, category=category, date=date, sectionId=sid,
            llmCalls=stats.llm_calls, durationMs=timer.ms(), **exc_fields(exc, frames=1))
        return "failed"
    except Exception as exc:
        _save(svc, category, date, sid, status="failed", message="The podcast could not be made right now. Try again later.", **own, **draft)
        log(logger, logging.ERROR, "tldr_podcast_crashed", podcastId=rid, category=category, date=date, sectionId=sid,
            durationMs=timer.ms(), **exc_fields(exc))
        raise
    _save(svc, category, date, sid, status="ready", audioPath=audio_path, durationSec=round(audio.duration_sec),
          lineTimings=line_timings(turns, audio.parts), **own, **draft)
    log(logger, logging.INFO, "tldr_podcast_ready", podcastId=rid, coverage=report["status"], lines=report["lines"],
        words=report["words"], llmCalls=stats.llm_calls, ttsCalls=audio.tts_calls, audioChunks=len(audio.chunk_chars),
        seconds=round(audio.duration_sec), bytes=len(audio.wav), durationMs=timer.ms())
    return "ok"
