"""Shared, cached stages (Architecture v3 B2-B5).

Parsing, enrichment and embeddings happen once per newsletter issue / item and are reused by
every user who receives the same public newsletter. Only content that passed the newsletter
header check (List-Unsubscribe or List-Id) is ever written to the shared cache.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from app.ai import prompts
from app.ai.client import AiOutputInvalid, AiUnavailable
from app.ai.schemas import ExtractionResult, WholeEmailSummary
from app.logs import Timer, exc_fields, get_logger, log
from app.pipeline.dedupe import DedupeInput, cluster
from app.pipeline.enrich import fetch_article
from app.pipeline.text import clean_issue_html, is_newsletter, url_in_issue
from app.recommend.story_embeddings import ensure_story_embeddings
from app.services import Services
from app.store import now, stable_id, unvec, vec

logger = get_logger(__name__)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sample_issues"
LEASE_SECONDS = 180


@dataclass
class RawIssue:
    sender_id: str
    sender_name: str
    sender_domain: str
    subject: str
    received_at: datetime
    html: str
    headers: dict[str, str]
    message_id: str | None
    source: str  # sample | gmail
    is_known_tech: bool = False


def issue_id_for(sender_id: str, subject: str, day: str) -> str:
    # Cache key from the document: sender + issue subject + date.
    return stable_id(sender_id, subject.strip().lower(), day, prefix="is_")


def load_sample_issues(day: str) -> list[RawIssue]:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for entry in manifest["issues"]:
        s = entry["sender"]
        out.append(
            RawIssue(
                sender_id=s["id"],
                sender_name=s["name"],
                sender_domain=s["domain"],
                subject=entry["subject"],
                received_at=now(),
                html=(FIXTURES / entry["file"]).read_text(encoding="utf-8"),
                headers=entry["headers"],
                message_id=None,
                source="sample",
                is_known_tech=s.get("isKnownTech", False),
            )
        )
    return out


def store_issue(svc: Services, raw: RawIssue, day: str) -> str | None:
    """B2 output: issue doc + raw HTML in Storage (7-day lifecycle). Returns the issue id."""
    if not is_newsletter(raw.headers):
        log(logger, logging.INFO, "skipped_non_newsletter", sender=raw.sender_id, headers=sorted(raw.headers))
        return None
    p = svc.paths
    p.sender(raw.sender_id).set(
        {"name": raw.sender_name, "domain": raw.sender_domain, "isKnownTech": raw.is_known_tech,
         "listIdHeader": raw.headers.get("List-Id")},
        merge=True,
    )
    iid = issue_id_for(raw.sender_id, raw.subject, day)
    ref = p.issue(iid)
    snap = ref.get()
    if snap.exists:
        log(logger, logging.INFO, "issue_already_stored", issueId=iid, sender=raw.sender_id,
            parseStatus=(snap.to_dict() or {}).get("parseStatus"))
        return iid
    raw_path = f"raw/{iid}.html"
    svc.blobs.put_text(raw_path, raw.html, content_type="text/html; charset=utf-8")
    ref.set(
        {
            "senderId": raw.sender_id,
            "senderName": raw.sender_name,
            "receivedAt": raw.received_at,
            "issueDate": day,
            "subject": raw.subject,
            "messageId": raw.message_id,
            "rawHtmlPath": raw_path,
            "parseStatus": "pending",
            "itemCount": 0,
            "source": raw.source,
        },
        merge=False,
    )
    log(logger, logging.INFO, "issue_stored", issueId=iid, sender=raw.sender_id, source=raw.source,
        htmlChars=len(raw.html), rawHtmlPath=raw_path)
    return iid


def _claim(svc: Services, ref, done_field: str, done_values: set[str]) -> bool:
    """Transactional lease so only one worker parses an issue at a time."""
    transaction = svc.db.transaction()

    @firestore.transactional
    def txn(tx) -> bool:
        snap = ref.get(transaction=tx)
        data = snap.to_dict() or {}
        if data.get(done_field) in done_values:
            return False
        lease = data.get("leaseUntil")
        if lease and lease > now():
            return False
        tx.update(ref, {"leaseUntil": now() + timedelta(seconds=LEASE_SECONDS)})
        return True

    return txn(transaction)


def parse_issue(svc: Services, issue_id: str) -> str:
    """B3: the LLM splits the issue into items (schema 9.1). Returns the parse status."""
    p = svc.paths
    ref = p.issue(issue_id)
    done = {"parsed", "whole_email_fallback"}
    timer = Timer()
    for waited in range(60):
        data = ref.get().to_dict() or {}
        if not data:
            log(logger, logging.ERROR, "issue_missing", issueId=issue_id)
        if data.get("parseStatus") in done:
            log(logger, logging.INFO, "issue_parse_cached", issueId=issue_id, status=data["parseStatus"],
                items=data.get("itemCount"))
            return data["parseStatus"]
        if _claim(svc, ref, "parseStatus", done):
            break
        if waited % 5 == 0:
            log(logger, logging.INFO, "issue_parse_waiting_for_lease", issueId=issue_id, waitedSeconds=waited * 2,
                parseStatus=data.get("parseStatus"))
        time.sleep(2)  # another worker holds the lease
    else:
        log(logger, logging.ERROR, "issue_parse_lease_timeout", issueId=issue_id, waitedSeconds=timer.ms() // 1000)
        raise RuntimeError(f"issue {issue_id} is locked by another worker")

    data = ref.get().to_dict() or {}
    log(logger, logging.INFO, "issue_parse_start", issueId=issue_id, sender=data.get("senderName"),
        rawHtmlPath=data.get("rawHtmlPath"), previousStatus=data.get("parseStatus"))
    html = svc.blobs.get_text(data["rawHtmlPath"])
    if html is None:
        log(logger, logging.ERROR, "issue_raw_html_missing", issueId=issue_id, rawHtmlPath=data["rawHtmlPath"],
            hint="Blob is gone (emulator data reset or 7-day lifecycle); re-ingest the newsletter")
        ref.update({"parseStatus": "failed", "parseError": "raw HTML expired", "leaseUntil": None})
        return "failed"
    text, urls = clean_issue_html(html)
    log(logger, logging.INFO, "issue_cleaned", issueId=issue_id, htmlChars=len(html), textChars=len(text),
        sentChars=min(len(text), 30000), links=len(urls))
    items: list[dict[str, Any]] = []
    status = "parsed"
    try:
        result = svc.llm.generate_json(
            "flash", prompts.EXTRACT_SYSTEM, prompts.extract_prompt(data["senderName"], data["subject"], text[:30000]),
            ExtractionResult, temperature=0.1,
        )
        for idx, it in enumerate(result.items):
            items.append(
                {
                    "headline": it.headline.strip(),
                    "blurb": it.blurb.strip(),
                    "url": url_in_issue(it.url, urls),
                    "urlDropped": bool(it.url) and url_in_issue(it.url, urls) is None,
                    "isSponsor": it.is_sponsor,
                    "category": it.category,
                    "entities": [e.strip() for e in it.entities if e.strip()][:12],
                    "order": idx,
                }
            )
    except AiOutputInvalid as exc:
        status = "whole_email_fallback"
        log(logger, logging.WARNING, "issue_parse_fallback_whole_email", issueId=issue_id, **exc_fields(exc, frames=1))
        summary = svc.llm.generate_json(
            "flash", prompts.WHOLE_EMAIL_SYSTEM, f"<newsletter>\n{text[:30000]}\n</newsletter>", WholeEmailSummary,
            temperature=0.1,
        )
        items = [{
            "headline": summary.headline, "blurb": summary.blurb, "url": None, "urlDropped": False,
            "isSponsor": False, "category": summary.category, "entities": summary.entities, "order": 0,
        }]
    except AiUnavailable as exc:
        log(logger, logging.ERROR, "issue_parse_llm_unavailable", issueId=issue_id, category=exc.category, durationMs=timer.ms())
        ref.update({"leaseUntil": None})
        raise

    batch = svc.db.batch()
    for it in items:
        item_id = stable_id(issue_id, str(it["order"]), it["headline"], prefix="it_")
        batch.set(
            p.item(item_id),
            {
                **it,
                "issueId": issue_id,
                "senderId": data["senderId"],
                "senderName": data["senderName"],
                "issueDate": data["issueDate"],
                "linkOnly": not it["url"],
                "linkOnlyReason": None if it["url"] else "no_link",
                "enrichStatus": "pending" if it["url"] and not it["isSponsor"] else "skipped",
            },
        )
    batch.update(ref, {"parseStatus": status, "itemCount": len(items), "leaseUntil": None, "parsedAt": now()})
    batch.commit()
    log(logger, logging.INFO, "issue_parsed", issueId=issue_id, items=len(items), status=status,
        sponsors=sum(1 for it in items if it["isSponsor"]), withLink=sum(1 for it in items if it["url"]),
        linksDropped=sum(1 for it in items if it.get("urlDropped")), durationMs=timer.ms())
    return status


def issue_items(svc: Services, issue_ids: list[str]) -> list[dict[str, Any]]:
    out = []
    for iid in issue_ids:
        for snap in svc.paths.items().where(filter=FieldFilter("issueId", "==", iid)).stream():
            out.append({"id": snap.id, **snap.to_dict()})
    return out


def enrich_items(svc: Services, items: list[dict[str, Any]]) -> None:
    """B4: fetch linked article text for items that have not been enriched yet."""
    todo = [it for it in items if it.get("enrichStatus") == "pending"]
    s = svc.settings
    timer = Timer()
    log(logger, logging.INFO, "enrich_start", total=len(items), todo=len(todo), cached=len(items) - len(todo))

    def work(it: dict[str, Any]) -> None:
        res = fetch_article(it["url"], timeout=s.enrich_timeout_seconds, max_chars=s.enrich_max_chars, client=svc.http)
        update: dict[str, Any] = {"enrichStatus": "done", "linkOnly": res.link_only, "linkOnlyReason": None if not res.link_only else res.reason}
        if res.text:
            path = f"articles/{it['id']}.txt"
            svc.blobs.put_text(path, res.text)
            update["fullTextPath"] = path
            update["innerLinks"] = res.inner_links
        svc.paths.item(it["id"]).update(update)
        it.update(update)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, todo))
    reasons: dict[str, int] = {}
    for it in todo:
        key = "ok" if not it.get("linkOnly") else str(it.get("linkOnlyReason"))
        reasons[key] = reasons.get(key, 0) + 1
    log(logger, logging.INFO, "enrich_done", todo=len(todo), outcomes=reasons, durationMs=timer.ms())


def embed_and_cluster(svc: Services, items: list[dict[str, Any]]) -> tuple[dict[str, tuple[str, int]], str]:
    """B5: embeddings (cached on items) and clusters. Falls back to URL-only matching."""
    mode = "embedding"
    timer = Timer()
    missing = [it for it in items if not it.get("embedding")]
    log(logger, logging.INFO, "embed_start", items=len(items), missing=len(missing), cached=len(items) - len(missing))
    if svc.settings.rec_enabled:
        # Recommendations on: headline + blurb + article intro, validated, cached on the item by content signature.
        stats = ensure_story_embeddings(svc, items)
        if stats.stories and not stats.usable:
            mode = "url_only"
            log(logger, logging.WARNING, "embedding_failed_url_fallback", count=stats.stories, category=stats.error or "invalid_vectors")
    elif missing:
        try:
            vectors = svc.embedder.embed([f"{it['headline']}\n{it['blurb']}" for it in missing])
            batch = svc.db.batch()
            for it, v in zip(missing, vectors, strict=True):
                it["embedding"] = v
                batch.update(svc.paths.item(it["id"]), {"embedding": vec(v)})
            batch.commit()
        except AiUnavailable as exc:
            mode = "url_only"
            log(logger, logging.WARNING, "embedding_failed_url_fallback", count=len(missing), category=exc.category,
                **exc_fields(exc, frames=1))
    inputs = [
        DedupeInput(it["id"], it.get("url"), it["senderId"], unvec(it.get("embedding")) if mode == "embedding" else None)
        for it in items
        if not it.get("isSponsor")
    ]
    # With recommendations on, the duplicate threshold is REC_DUPLICATE_THRESHOLD: with gemini-embedding-001 distinct but
    # related stories reached cosine 0.91 in recsys_eval, so the original 0.88 would merge different stories.
    threshold = svc.settings.rec_duplicate_threshold if svc.settings.rec_enabled else svc.settings.dedupe_cosine_threshold
    clusters = cluster(inputs, threshold)
    batch = svc.db.batch()
    for iid, (cid, count) in clusters.items():
        batch.update(svc.paths.item(iid), {"clusterId": cid, "coveredByCount": count})
    batch.commit()
    log(logger, logging.INFO, "cluster_done", mode=mode, inputs=len(inputs), clusters=len({c for c, _ in clusters.values()}),
        threshold=threshold, durationMs=timer.ms())
    return clusters, mode
