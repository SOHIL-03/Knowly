"""Runs one briefing through B1-B10. Called by the Cloud Run Job, the Pub/Sub push handler or
the local inline dispatcher. Safe to call twice for the same briefing (idempotent job key)."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from google.cloud import firestore

from app.ai.client import QUOTA_MESSAGE, AiQuotaExceeded, AiUnavailable
from app.learner import service as learner
from app.logs import Timer, get_logger, log
from app.pipeline import ingest, personal
from app.services import Services
from app.store import DEFAULT_PREFS, local_date, now, stable_id

logger = get_logger(__name__)
LEASE_MINUTES = 15


class BriefingSkipped(Exception):
    pass


def _acquire(svc: Services, uid: str, bid: str, owner: str) -> dict[str, Any]:
    ref = svc.paths.briefing(uid, bid)
    transaction = svc.db.transaction()

    @firestore.transactional
    def txn(tx) -> dict[str, Any]:
        snap = ref.get(transaction=tx)
        if not snap.exists:
            raise BriefingSkipped("briefing document missing")
        data = snap.to_dict() or {}
        if data.get("status") in ("ready", "failed"):
            raise BriefingSkipped(f"already {data['status']}")
        lease = data.get("leaseUntil")
        if lease and lease > now() and data.get("leaseOwner") != owner:
            raise BriefingSkipped("another worker holds the lease")
        tx.update(ref, {"leaseOwner": owner, "leaseUntil": now() + timedelta(minutes=LEASE_MINUTES)})
        return data

    return txn(transaction)


class Progress:
    def __init__(self, svc: Services, uid: str, bid: str) -> None:
        self.ref = svc.paths.briefing(uid, bid)
        self.counts: dict[str, Any] = {}
        self.timings: dict[str, int] = {}

    def stage(self, stage: str, message: str, **counts: Any) -> None:
        self.counts.update(counts)
        self.ref.update({"progress": {"stage": stage, "message": message, **self.counts}, "updatedAt": now()})

    def timed(self, name: str, timer: Timer) -> None:
        self.timings[name] = timer.ms()
        self.ref.update({"timings": self.timings})


def run_briefing(svc: Services, uid: str, bid: str) -> str:
    owner = uuid.uuid4().hex
    try:
        briefing = _acquire(svc, uid, bid, owner)
    except BriefingSkipped as exc:
        log(logger, logging.INFO, "briefing_skipped", uid=uid, briefingId=bid, reason=str(exc))
        return "skipped"

    prog = Progress(svc, uid, bid)
    p = svc.paths
    try:
        user = p.user(uid).get().to_dict() or {}
        prefs = {**DEFAULT_PREFS, **(user.get("prefs") or {})}
        tz = user.get("timezone") or "UTC"
        day = briefing.get("date") or local_date(tz)
        kind = briefing.get("kind", "daily")

        # B2 Fetch
        t = Timer()
        prog.stage("fetch", "Reading your newsletters")
        if kind == "review":
            raws = []
        elif user.get("mode") == "sample":
            raws = ingest.load_sample_issues(day)
        else:
            from app.gmail.fetch import fetch_new_issues

            raws = fetch_new_issues(svc, uid, user)
        issue_ids = [iid for raw in raws if (iid := ingest.store_issue(svc, raw, day))]
        prog.timed("fetch", t)
        prog.stage("fetch", f"Reading {len(issue_ids)} newsletters", issues=len(issue_ids))

        if not issue_ids and kind != "review":
            prog.ref.update({"status": "empty", "progress.stage": "empty",
                             "progress.message": "No new issues today", "leaseUntil": None})
            return "empty"

        # B3 Parse (cached per issue)
        t = Timer()
        prog.stage("parse", "Finding the stories")
        statuses = {iid: ingest.parse_issue(svc, iid) for iid in issue_ids}
        items = ingest.issue_items(svc, issue_ids)
        prog.timed("parse", t)
        prog.stage("parse", f"Found {sum(1 for i in items if not i.get('isSponsor'))} stories",
                   storiesFound=sum(1 for i in items if not i.get("isSponsor")),
                   wholeEmailFallback=[i for i, s in statuses.items() if s == "whole_email_fallback"])

        # B4 Enrich (cached per item)
        t = Timer()
        prog.stage("enrich", "Opening the linked articles")
        ingest.enrich_items(svc, items)
        prog.timed("enrich", t)

        # B5 De-duplicate
        t = Timer()
        clusters, dedupe_mode = ingest.embed_and_cluster(svc, items)
        prog.timed("dedupe", t)

        # B6 Score
        t = Timer()
        prog.stage("rank", "Picking what matters to you")
        from app.profile import embed_profile

        embed_profile(svc, uid)  # fills embeddings that were missing when the profile was saved
        ranked, signals = personal.score_stories(svc, uid, bid, items, clusters, bool(prefs.get("wildcardOn", True)))
        prog.timed("rank", t)
        review, _ = personal.due_review_material(svc, uid, now(), recent_saves=(kind == "review"))
        if not ranked and not review:
            message = "Nothing saved this week to review" if kind == "review" else "Every story today matched your mute list"
            prog.ref.update({"status": "empty", "progress.stage": "empty",
                             "progress.message": message, "leaseUntil": None})
            return "empty"

        # B7 Plan (+ review material)
        t = Timer()
        concepts = learner.load_concepts(svc, uid)
        plan, plan_mode, plan_problems = personal.plan_segments(svc, ranked, int(prefs["lengthMin"]), concepts, review)
        prog.timed("plan", t)
        picked = sum(len(s.item_ids) for s in plan.segments)
        prog.stage("plan", f"Picked {picked} for you", storiesPicked=picked)

        # B8 Grounded podcast scripts, segment documents
        t = Timer()
        prog.stage("brief", "Writing your briefing")
        style = prefs.get("style", "twoHost")
        hosts = personal.hosts_for(svc)
        all_ids = {iid for s in plan.segments for iid in s.item_ids}
        item_map = {i["id"]: i for i in items}
        for iid in all_ids - set(item_map):
            snap = p.item(iid).get()
            if snap.exists:
                item_map[iid] = {"id": iid, **snap.to_dict()}
        reason_by_id = {s.story.id: s.reason for s in ranked}
        mastery_by_name = {c["name"]: float(c.get("mastery", 0.1)) for c in concepts}
        scripts = []
        for order, seg in enumerate(plan.segments):
            seg_items = {i: item_map[i] for i in seg.item_ids}
            concept_ids = learner.match_or_create(svc, uid, seg.concept_names, seg.item_ids)
            if seg.type == "review":
                concept_ids = list(dict.fromkeys(concept_ids + [r["conceptId"] for r in review if r.get("conceptId")]))
            seg_mastery = {n: mastery_by_name.get(n, 0.1) for n in seg.concept_names}
            script, script_mode, script_problems, faith = personal.write_script(
                svc, seg, seg_items, seg_mastery, style, signals.project_text, plan.wildcard_item_id,
            )
            scripts.append(script)
            sid = f"s{order + 1}"
            p.segment(uid, bid, sid).set(
                {
                    "order": order + 1,
                    "type": seg.type,
                    "title": seg.title,
                    "whyChosen": seg.why_chosen if seg.type != "review" else "Due for spaced review",
                    "itemReasons": {i: reason_by_id.get(i, "Due for review") for i in seg.item_ids},
                    "wildcardItemId": plan.wildcard_item_id if plan.wildcard_item_id in seg.item_ids else None,
                    **personal.script_doc(script, hosts),
                    "style": style,
                    "scriptMode": script_mode,
                    "scriptProblems": script_problems[:5],
                    "faithfulness": faith["verdict"],
                    "unsupportedClaims": faith["unsupportedClaims"],
                    "itemIds": seg.item_ids,
                    "conceptIds": concept_ids,
                    "targetSeconds": seg.target_seconds,
                    "durationSec": seg.target_seconds,
                    "audioStatus": "pending",
                    "audioAttempts": 0,
                    "removedByUser": False,
                    "createdAt": now(),
                }
            )
        prog.timed("brief", t)

        # B10 Publish the text briefing now; audio follows per segment (B9).
        also = []
        for iid in plan.also_in_inbox:
            it = item_map.get(iid)
            if it:
                also.append({"itemId": iid, "headline": it["headline"], "url": it.get("url"), "senderName": it.get("senderName")})
        cluster_ids = sorted({clusters[i][0] for i in all_ids if i in clusters})
        p.briefing(uid, bid).update(
            {
                "status": "ready",
                "readyAt": now(),
                "totalDurationSec": sum(s.target_seconds for s in plan.segments),
                "itemIds": sorted(all_ids),
                "clusterIds": cluster_ids,
                "alsoInInbox": also,
                "recap": {"takeaways": personal.recap_takeaways(scripts), "quizScore": None, "savedCount": 0},
                "planMode": plan_mode,
                "planProblems": plan_problems[:10],
                "dedupeMode": dedupe_mode,
                "progress": {**prog.counts, "stage": "ready", "message": "Your briefing is ready"},
                "leaseUntil": None,
            }
        )
        for order in range(len(plan.segments)):
            svc.dispatcher.task("segment-audio", {"uid": uid, "briefingId": bid, "segId": f"s{order + 1}"},
                                dedupe_key=f"audio-{stable_id(uid, bid, str(order))}-0")
        notify_ready(svc, uid, bid, prefs)
        log(logger, logging.INFO, "briefing_ready", uid=uid, briefingId=bid, segments=len(plan.segments), timings=prog.timings)
        return "ready"
    except AiQuotaExceeded as exc:
        _fail(prog, QUOTA_MESSAGE, exc)
        return "failed"
    except AiUnavailable as exc:
        _fail(prog, "Gemini is unavailable, so the briefing could not be written. Try again shortly.", exc)
        return "failed"
    except Exception as exc:
        _fail(prog, "Something went wrong while making your briefing.", exc)
        raise


def _fail(prog: Progress, message: str, exc: Exception) -> None:
    logger.exception("briefing failed")
    prog.ref.update({"status": "failed", "error": {"message": message, "type": type(exc).__name__},
                     "progress.stage": "failed", "progress.message": message, "leaseUntil": None})


def notify_ready(svc: Services, uid: str, bid: str, prefs: dict[str, Any]) -> None:
    if prefs.get("notify") != "push" or svc.notifier is None:
        return
    user_ref = svc.paths.user(uid)
    tokens = list((user_ref.get().to_dict() or {}).get("fcmTokens") or [])
    if not tokens:
        return
    invalid = svc.notifier.send(tokens, "Your Knowly briefing is ready", "Tap to listen.", {"briefingId": bid})
    if invalid:
        user_ref.update({"fcmTokens": firestore.ArrayRemove(invalid)})
