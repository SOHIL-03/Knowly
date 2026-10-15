"""Per-user stages (Architecture v3 B6-B8 and B10)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from app.ai import prompts, trace
from app.ai.client import AiOutputInvalid
from app.ai.schemas import FaithfulnessResult, PlannedSegment, PodcastScript, ScriptLine, SegmentPlan
from app.learner import model as lm
from app.learner import service as learner
from app.logs import exc_fields, get_logger, log
from app.pipeline.score import Scored, Story, UserSignals, rank
from app.pipeline.validate import render_script_text, repair_plan, script_problems
from app.services import Services
from app.store import now, unvec

logger = get_logger(__name__)

ARTICLE_EXCERPT_CHARS = 4000


# --- B6 ---------------------------------------------------------------------------------

def user_signals(svc: Services, uid: str, briefing_id: str) -> UserSignals:
    p = svc.paths
    profile = p.profile(uid).get().to_dict() or {}
    project = profile.get("project") or {}
    goals = [(g.get("topic", ""), unvec(g.get("embedding"))) for g in profile.get("goals") or []]
    concepts = learner.load_concepts(svc, uid)
    known = [(c["name"], unvec(c.get("embedding"))) for c in concepts if float(c.get("mastery", 0)) >= 0.7]
    trust = {s.id: float((s.to_dict() or {}).get("trust", 0.5)) for s in p.newsletters(uid).stream()}
    heard: set[str] = set()
    recent = p.briefings(uid).order_by("date", direction="DESCENDING").limit(8).stream()
    count = 0
    for b in recent:
        if b.id == briefing_id:
            continue
        heard.update((b.to_dict() or {}).get("clusterIds") or [])
        count += 1
        if count >= 7:
            break
    return UserSignals(
        project_text=" ".join(filter(None, [project.get("text"), project.get("summary")])),
        project_embedding=unvec(project.get("embedding")),
        goals=goals,
        mute_topics=list(profile.get("muteTopics") or []),
        topic_weights=dict(profile.get("topicWeights") or {}),
        trust_by_sender=trust,
        known_concepts=known,
        heard_clusters=heard,
    )


def to_story(it: dict[str, Any], clusters: dict[str, tuple[str, int]]) -> Story:
    cid, count = clusters.get(it["id"], (it.get("clusterId") or it["id"], it.get("coveredByCount") or 1))
    return Story(
        id=it["id"], headline=it["headline"], blurb=it["blurb"], category=it.get("category", "other"),
        entities=list(it.get("entities") or []), sender_id=it["senderId"], sender_name=it.get("senderName", ""),
        embedding=unvec(it.get("embedding")), cluster_id=cid, covered_by=count, link_only=bool(it.get("linkOnly")),
    )


def unique_stories(items: list[dict[str, Any]], clusters: dict[str, tuple[str, int]]) -> list[Story]:
    """One story per cluster; prefer the representative with fetched article text."""
    best: dict[str, dict[str, Any]] = {}
    for it in items:
        if it.get("isSponsor"):
            continue
        cid = clusters.get(it["id"], (it["id"], 1))[0]
        cur = best.get(cid)
        if cur is None or (cur.get("linkOnly") and not it.get("linkOnly")):
            best[cid] = it
    return [to_story(it, clusters) for it in best.values()]


def score_stories(svc: Services, uid: str, briefing_id: str, items: list[dict[str, Any]],
                  clusters: dict[str, tuple[str, int]], wildcard_on: bool) -> tuple[list[Scored], UserSignals]:
    signals = user_signals(svc, uid, briefing_id)
    stories = unique_stories(items, clusters)
    ranked = rank(stories, signals, now(), wildcard_on=wildcard_on)
    log(logger, logging.INFO, "score_done", uid=uid, briefingId=briefing_id, items=len(items), stories=len(stories),
        ranked=len(ranked), muted=len(stories) - len(ranked), goals=len(signals.goals), muteTopics=len(signals.mute_topics),
        knownConcepts=len(signals.known_concepts), heardClusters=len(signals.heard_clusters),
        hasProject=bool(signals.project_text), hasProjectEmbedding=signals.project_embedding is not None,
        top=[{"id": r.story.id, "score": round(getattr(r, "score", 0.0), 3)} for r in ranked[:3]])
    return ranked, signals


# --- Review material ---------------------------------------------------------------------

def due_review_material(svc: Services, uid: str, at: datetime, *, recent_saves: bool = False) -> tuple[list[dict[str, Any]], set[str]]:
    """Concepts and saved items due for spaced review that still have a source item. At most two.

    recent_saves=True (review-only briefing) also takes items saved in the last 7 days."""
    p = svc.paths
    out: list[dict[str, Any]] = []
    ids: set[str] = set()
    if recent_saves:
        week = at - timedelta(days=7)
        for snap in p.library(uid).where(filter=FieldFilter("savedAt", ">=", week)).order_by("savedAt", direction="DESCENDING").limit(5).stream():
            lib = snap.to_dict() or {}
            iid = lib.get("itemId")
            if iid and iid not in ids and p.item(iid).get().exists:
                out.append({"kind": "library", "libId": snap.id, "itemId": iid, "headline": lib.get("headline")})
                ids.add(iid)
            if len(out) >= lm.MAX_REVIEWS_PER_BRIEFING:
                return out, ids
    for snap in p.concepts(uid).where(filter=FieldFilter("nextDueAt", "<=", at)).order_by("nextDueAt").limit(5).stream():
        c = snap.to_dict() or {}
        src = (c.get("sourceItemIds") or [])[-1:] if c.get("sourceItemIds") else []
        if src and p.item(src[0]).get().exists:
            out.append({"kind": "concept", "conceptId": snap.id, "name": c.get("name"), "itemId": src[0]})
            ids.add(src[0])
        if len(out) >= lm.MAX_REVIEWS_PER_BRIEFING:
            break
    if len(out) < lm.MAX_REVIEWS_PER_BRIEFING:
        for snap in p.library(uid).where(filter=FieldFilter("reviewDueAt", "<=", at)).order_by("reviewDueAt").limit(3).stream():
            lib = snap.to_dict() or {}
            iid = lib.get("itemId")
            if iid and iid not in ids and p.item(iid).get().exists:
                out.append({"kind": "library", "libId": snap.id, "itemId": iid, "headline": lib.get("headline")})
                ids.add(iid)
            if len(out) >= lm.MAX_REVIEWS_PER_BRIEFING:
                break
    return out, ids


# --- B7 ---------------------------------------------------------------------------------

def level_summary(concepts: list[dict[str, Any]]) -> str:
    if not concepts:
        return "No level information yet; assume an engineer new to these topics."
    parts = [f"{c['name']}={float(c.get('mastery', 0)):.2f}" for c in sorted(concepts, key=lambda c: c["name"])[:20]]
    return "Concept mastery: " + ", ".join(parts)


def fallback_plan(ranked: list[Scored], length_seconds: int, wildcard_id: str | None, review_ids: list[str]) -> SegmentPlan:
    """Deterministic plan used only when the model returns an invalid plan twice."""
    segs: list[PlannedSegment] = []
    news = [s for s in ranked if not s.wildcard]
    if news[:2]:
        segs.append(PlannedSegment(type="project", title="For your project", why_chosen=news[0].reason,
                                   item_ids=[s.story.id for s in news[:2]], target_seconds=int(length_seconds * 0.4)))
    if news[2:3]:
        segs.append(PlannedSegment(type="learning", title="Something to learn", why_chosen=news[2].reason,
                                   item_ids=[news[2].story.id], target_seconds=int(length_seconds * 0.25)))
    quick = [s.story.id for s in news[3:7]] + ([wildcard_id] if wildcard_id else [])
    if quick:
        segs.append(PlannedSegment(type="quickhits", title="Quick hits", why_chosen="Short items worth knowing",
                                   item_ids=quick, target_seconds=int(length_seconds * 0.25)))
    if review_ids:
        segs.append(PlannedSegment(type="review", title="Review", why_chosen="Due for spaced review",
                                   item_ids=review_ids[:2], target_seconds=45))
    return SegmentPlan(segments=segs or [PlannedSegment(type="quickhits", title="Today", why_chosen="Your newsletters",
                       item_ids=[s.story.id for s in ranked[:5]], target_seconds=length_seconds)],
                       wildcard_item_id=wildcard_id, also_in_inbox=[])


def plan_segments(svc: Services, ranked: list[Scored], length_min: int, concepts: list[dict[str, Any]],
                  review: list[dict[str, Any]]) -> tuple[SegmentPlan, str, list[str]]:
    wildcard = next((s.story.id for s in ranked if s.wildcard), None)
    candidates = [
        {"id": s.story.id, "headline": s.story.headline, "reason": s.reason, "category": s.story.category,
         "wildcard": s.wildcard}
        for s in ranked
    ]
    review_ids = [r["itemId"] for r in review]
    if review:
        candidates += [{"id": r["itemId"], "headline": r.get("name") or r.get("headline"), "reason": "Due for review",
                        "category": "review"} for r in review]
    length_seconds = length_min * 60
    mode = "model"
    log(logger, logging.INFO, "plan_start", candidates=len(candidates), review=len(review), lengthMin=length_min,
        wildcard=wildcard)
    try:
        plan = svc.llm.generate_json(
            "pro", prompts.PLAN_SYSTEM,
            prompts.plan_prompt(candidates, length_min, level_summary(concepts), wildcard, bool(review)),
            SegmentPlan, temperature=0.3,
        )
    except AiOutputInvalid as exc:
        log(logger, logging.WARNING, "plan_invalid_using_fallback", candidates=len(candidates), **exc_fields(exc, frames=1))
        plan = fallback_plan(ranked, length_seconds, wildcard, review_ids)
        mode = "fallback"
    check = repair_plan(plan, [s.story.id for s in ranked], length_seconds, wildcard, set(review_ids))
    if check.problems:
        log(logger, logging.INFO, "plan_repaired", problems=check.problems[:10])
    return check.plan, mode, check.problems


# --- B8 ---------------------------------------------------------------------------------

def item_payload(svc: Services, it: dict[str, Any], wildcard_id: str | None) -> dict[str, Any]:
    excerpt = None
    if it.get("fullTextPath") and not it.get("linkOnly"):
        text = svc.blobs.get_text(it["fullTextPath"]) or ""
        excerpt = text[: it.get("_excerptChars", ARTICLE_EXCERPT_CHARS)]
    return {
        "id": it["id"],
        "newsletter": it.get("senderName"),
        "headline": it["headline"],
        "newsletter_note": it["blurb"],
        "url": it.get("url"),
        "link_only": bool(it.get("linkOnly")),
        "article_excerpt": excerpt,
        "wildcard": it["id"] == wildcard_id,
        "linked_pages": it.get("_inner") or None,
    }


def hosts_for(svc: Services) -> dict[str, str]:
    s = svc.settings
    return {"A": s.tts_host_a_name, "B": s.tts_host_b_name}


def fallback_script(seg: PlannedSegment, items: dict[str, dict[str, Any]], single: bool) -> PodcastScript:
    """Grounded by construction: one line per item, built from the newsletter's own note."""
    first = items[seg.item_ids[0]]["headline"].rstrip(".")
    lines = [ScriptLine(speaker="A", text=f"Here is what is new: {first}.", style="clear")]
    for n, iid in enumerate(seg.item_ids):
        it = items[iid]
        text = f"{it['headline'].rstrip('.')}. {it['blurb']}"
        if it.get("linkOnly"):
            text += " Only the newsletter's short note was available; the full article is linked in the app."
        speaker = "A" if single or n % 2 else "B"
        lines.append(ScriptLine(speaker=speaker, text=text, item_ids=[iid], style="informative"))
    return PodcastScript(
        title=seg.title,
        opening_question=f"What is new in {first}?",
        lines=lines,
        takeaways=[items[i]["headline"] for i in seg.item_ids[:3]],
    )


def write_script(svc: Services, seg: PlannedSegment, items: dict[str, dict[str, Any]], mastery: dict[str, float],
                 style: str, project_text: str, wildcard_id: str | None,
                 seg_type: str | None = None) -> tuple[PodcastScript, str, list[str], dict[str, Any]]:
    """B8: script -> validator -> faithfulness check against the sources; one regeneration with the
    problems, then a script built directly from the newsletter notes.

    Returns (script, mode, problems, faithfulness)."""
    hosts = hosts_for(svc)
    single = style == "brief"
    payload = [item_payload(svc, items[i], wildcard_id) for i in seg.item_ids]
    prompt = prompts.script_prompt(seg_type or seg.type, seg.title, seg.why_chosen, payload, mastery, style,
                                   seg.target_seconds, project_text, hosts)
    allowed = set(seg.item_ids)
    problems: list[str] = []
    for attempt in range(2):
        p = prompt if not problems else f"{prompt}\n\nThe previous script was rejected: {'; '.join(problems)}. Fix it."
        try:
            script = svc.llm.generate_json("pro", prompts.SCRIPT_SYSTEM, p, PodcastScript, temperature=0.4)
        except AiOutputInvalid as exc:
            problems = [str(exc)[:300]]
            log(logger, logging.WARNING, "script_output_invalid", segTitle=seg.title, attempt=attempt, problems=problems)
            continue
        problems = script_problems(script, allowed, seg.target_seconds, single)
        trace.annotate("PodcastScript", appValidation={"passed": not problems, "problems": problems[:10]})
        if problems:
            log(logger, logging.WARNING, "script_rejected", segTitle=seg.title, attempt=attempt, problems=problems[:5])
            continue
        faith = check_faithfulness(svc, script, payload, items, hosts)
        trace.annotate("PodcastScript", faithfulness=faith["verdict"])
        if faith["verdict"] == "fail":
            problems = [f"unsupported: {c['quote']}" for c in faith["unsupportedClaims"][:5]] or ["unsupported claims"]
            log(logger, logging.WARNING, "script_unfaithful", segTitle=seg.title, attempt=attempt,
                unsupported=len(faith["unsupportedClaims"]))
            continue
        log(logger, logging.INFO, "script_accepted", segTitle=seg.title, attempt=attempt, lines=len(script.lines),
            faithfulness=faith["verdict"])
        return script, "model", [], faith
    log(logger, logging.WARNING, "script_using_fallback", segTitle=seg.title, items=len(seg.item_ids), problems=problems[:5])
    fb = fallback_script(seg, items, single)
    return fb, "fallback", problems, {"verdict": "pass", "unsupportedClaims": [], "note": "built from newsletter notes"}


def check_faithfulness(svc: Services, script: PodcastScript, payload: list[dict[str, Any]],
                       items: dict[str, dict[str, Any]], hosts: dict[str, str]) -> dict[str, Any]:
    """Section 9.7, applied to the script before audio: TTS reads the script verbatim, so checking
    the script checks what will be said."""
    text = render_script_text(script, items, hosts)
    try:
        res = svc.llm.generate_json(
            "flash", prompts.FAITHFULNESS_SYSTEM,
            f"SOURCES:\n<source>\n{json.dumps(payload)}\n</source>\n\nSCRIPT:\n<article>\n{text}\n</article>",
            FaithfulnessResult, temperature=0.0,
        )
    except AiOutputInvalid as exc:
        log(logger, logging.WARNING, "faithfulness_unchecked", **exc_fields(exc, frames=1))
        return {"verdict": "unchecked", "unsupportedClaims": []}
    return {"verdict": res.verdict, "unsupportedClaims": [c.model_dump() for c in res.unsupported_claims[:10]]}


def script_doc(script: PodcastScript, hosts: dict[str, str]) -> dict[str, Any]:
    """Segment fields derived from a script (stored in the segment document)."""
    return {
        "openingQuestion": script.opening_question,
        "script": [{**line.model_dump(), "speakerName": hosts.get(line.speaker, line.speaker)} for line in script.lines],
        "takeaways": script.takeaways,
        "sourceMap": [{"lineIdx": i, "itemId": iid} for i, line in enumerate(script.lines) for iid in line.item_ids],
    }


# --- B10 --------------------------------------------------------------------------------

def recap_takeaways(scripts: list[PodcastScript]) -> list[str]:
    out: list[str] = []
    for b in scripts:
        for t in b.takeaways:
            if t not in out:
                out.append(t)
                break
    return out[:3]


def review_due_after(at: datetime) -> datetime:
    return at + timedelta(days=1)
