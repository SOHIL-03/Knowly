"""Concepts a podcast covers. Extraction only records that a concept was PRESENTED; it says nothing about the listener.

Stored next to the podcast state at `podcasts/tldr/{category}/{date}/{key}.concepts.json`
({"status": "ready" | "failed", "concepts": [...], "message"}). A failure never touches the podcast itself:
`ensure` can be called again later and only re-runs this one step.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.ai import prompts
from app.ai.client import AiOutputInvalid
from app.ai.schemas import PodcastConcepts
from app.logs import get_logger, log
from app.services import Services
from app.store import now

logger = get_logger(__name__)

MAX_CONCEPTS = 12
SOURCE_CHARS = 6000
ARTICLES = {"a", "an", "the"}


def concepts_path(podcast_key: str) -> str:
    """podcast_key is the storage path prefix without extension, e.g. podcasts/tldr/tech/2026-10-16/big-tech."""
    return f"{podcast_key}.concepts.json"


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def normalize_name(name: str, domain: str | None = None) -> str:
    """Stable concept key. Equivalent spellings ("Vector databases", "the vector database") share a key; the same
    name used in two different fields stays apart because the domain is part of the key."""
    words = [_singular(w) for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in ARTICLES]
    key = "_".join(words)[:50] or "concept"
    dom = "_".join(re.findall(r"[a-z0-9]+", (domain or "").lower()))[:24]
    return f"{key}__{dom}" if dom else key


def build(podcast_id: str, section_id: str | None, lines: list[dict[str, Any]], extracted: PodcastConcepts) -> list[dict[str, Any]]:
    """Validated concept records from the model's output: every concept must point at real script lines."""
    by_name: dict[str, dict[str, Any]] = {}
    rank = {"major": 0, "supporting": 1, "mention": 2}
    keyed = {c.name.strip().lower(): normalize_name(c.name, c.domain) for c in extracted.concepts}
    for c in extracted.concepts:
        spans = [{"line": n, "quote": lines[n]["text"][:200]} for n in dict.fromkeys(c.lines) if 0 <= n < len(lines)]
        if not spans:
            continue  # nothing in the script backs it: not extracted from the content
        key = normalize_name(c.name, c.domain)
        prior = by_name.get(key)
        if prior:  # the same concept twice in one podcast: merge
            prior["evidence_spans"] += [s for s in spans if s not in prior["evidence_spans"]][:4]
            if rank[c.importance] < rank[prior["importance"]]:
                prior["importance"] = c.importance
            continue
        by_name[key] = {
            "concept_id": key, "name": c.name.strip(), "normalized_name": key, "description": c.description.strip(),
            "podcast_id": podcast_id, "source_section_id": section_id, "importance": c.importance,
            "prerequisite_concepts": [keyed[p.strip().lower()] for p in c.prerequisites if p.strip().lower() in keyed and keyed[p.strip().lower()] != key],
            "evidence_spans": spans[:6], "created_at": now().isoformat(),
        }
    out = sorted(by_name.values(), key=lambda c: rank[c["importance"]])[:MAX_CONCEPTS]
    return out


def extract(svc: Services, podcast_id: str, section_id: str | None, lines: list[dict[str, Any]], source: str = "",
            on_result=None) -> list[dict[str, Any]]:
    script = "\n".join(f"{i}. {ln['speaker']}: {ln['text']}" for i, ln in enumerate(lines))
    prompt = f"<script>\n{script}\n</script>"
    if source:
        prompt += f"\n\n<source>\n{source[:SOURCE_CHARS]}\n</source>"
    extracted = svc.llm.generate_json("flash", prompts.CONCEPTS_EXTRACT_SYSTEM, prompt, PodcastConcepts, temperature=0.1,
                                      max_output_tokens=2500, on_result=on_result)
    found = build(podcast_id, section_id, lines, extracted)
    if not found:
        raise AiOutputInvalid("No concept could be tied to the script")
    return found


def save(svc: Services, podcast_key: str, status: str, concepts: list[dict[str, Any]] | None = None, message: str | None = None) -> None:
    svc.blobs.put_text(concepts_path(podcast_key), json.dumps({"status": status, "concepts": concepts or [], "message": message,
                                                              "updatedAt": now().isoformat()}, ensure_ascii=False), "application/json")


def load(svc: Services, podcast_key: str) -> dict[str, Any]:
    raw = svc.blobs.get_text(concepts_path(podcast_key))
    return json.loads(raw) if raw else {"status": "none", "concepts": []}


def extract_and_save(svc: Services, podcast_key: str, podcast_id: str, section_id: str | None, lines: list[dict[str, Any]],
                     source: str = "", on_result=None) -> str:
    """Extraction as an optional stage: records the outcome and never raises for provider or output problems."""
    try:
        found = extract(svc, podcast_id, section_id, lines, source, on_result)
    except Exception as exc:  # optional stage: any failure is recorded, never raised
        save(svc, podcast_key, "failed", message="Concepts could not be extracted. Try again.")
        log(logger, logging.WARNING, "concepts_failed", podcastId=podcast_id, error=type(exc).__name__)
        return "failed"
    save(svc, podcast_key, "ready", found)
    log(logger, logging.INFO, "concepts_ready", podcastId=podcast_id, concepts=len(found),
        major=sum(1 for c in found if c["importance"] == "major"))
    return "ready"
