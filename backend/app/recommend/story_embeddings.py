"""B5: story embeddings, cached on the shared item (items/{id}) and reused by every user.

Document text = headline + the newsletter blurb + the opening of the linked article when one was
fetched (B4 runs first). A story is embedded once: the item stores a signature of the inputs (model,
dimension, headline, blurb, whether article text exists, excerpt length). It is embedded again only
when that signature changes. Items that predate the signature are embedded once more so every cached
vector uses the same representation. Article text is stored once per item and never changes, so the
signature tracks its presence, not its bytes.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.logs import Timer, get_logger, log
from app.recommend.embedding import EmbedResult, embed_resilient
from app.recommend.settings import RecConfig
from app.recommend.vectors import clean_vector
from app.services import Services
from app.store import now, unvec, vec

logger = get_logger(__name__)
SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s")


@dataclass
class StoryEmbedStats:
    stories: int = 0
    cached: int = 0
    embedded: int = 0
    invalid: int = 0
    skipped_empty: int = 0
    failed: int = 0
    api_calls: int = 0
    est_tokens: int = 0
    error: str | None = None

    @property
    def usable(self) -> int:
        return self.cached + self.embedded


def intro_excerpt(text: str | None, limit: int) -> str:
    """The opening of an article, at most `limit` characters, cut at a sentence (else word) boundary."""
    if not text or limit <= 0:
        return ""
    body = re.sub(r"\s+", " ", text).strip()
    if len(body) <= limit:
        return body
    cut = body[:limit]
    ends = [m.end() for m in SENTENCE_END.finditer(cut)]
    if ends and ends[-1] >= limit // 2:
        return cut[: ends[-1]].strip()
    return cut.rsplit(" ", 1)[0].strip() or cut


def story_document(headline: str, blurb: str, excerpt: str) -> str:
    return "\n".join(p for p in (headline.strip(), blurb.strip(), excerpt.strip()) if p)


def signature(it: dict[str, Any], model: str, dim: int, cfg: RecConfig) -> str:
    has_article = bool(it.get("fullTextPath")) and not it.get("linkOnly")
    raw = f"{model}|{dim}|{cfg.excerpt_chars}|{it.get('headline', '')}|{it.get('blurb', '')}|{int(has_article)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def ensure_story_embeddings(svc: Services, items: list[dict[str, Any]], cfg: RecConfig | None = None) -> StoryEmbedStats:
    """Makes sure every non-sponsor item has a valid, current embedding, calling the embedding API only
    for items that are new or whose content changed. Sets it["embedding"] on the in-memory items."""
    cfg = cfg or RecConfig.from_settings(svc.settings)
    model, dim = svc.settings.gemini_embed_model, svc.settings.embed_dim
    timer = Timer()
    stats = StoryEmbedStats()
    todo: list[tuple[dict[str, Any], str, str]] = []  # (item, signature, document)
    for it in items:
        if it.get("isSponsor"):
            continue
        stats.stories += 1
        sig = signature(it, model, dim, cfg)
        current = clean_vector(unvec(it.get("embedding")), dim) if it.get("embedding") is not None else None
        if current is not None and it.get("embeddingSig") == sig:
            it["embedding"] = current
            stats.cached += 1
            continue
        excerpt = ""
        if it.get("fullTextPath") and not it.get("linkOnly"):
            excerpt = intro_excerpt(svc.blobs.get_text(it["fullTextPath"]), cfg.excerpt_chars)
        doc = story_document(str(it.get("headline") or ""), str(it.get("blurb") or ""), excerpt)
        if not doc:
            stats.skipped_empty += 1
            it["embedding"] = None
            continue
        it["embedding"] = current  # a stale but valid vector is still better than none if the call fails
        todo.append((it, sig, doc))

    res = EmbedResult(vectors=[])
    if todo:
        res = embed_resilient(svc.embedder, [d for _, _, d in todo], "document", cfg, dim)
        batch = svc.db.batch()
        at = now()
        for (it, sig, _), v in zip(todo, res.vectors, strict=True):
            if v is None:
                stats.failed += 1
                continue
            it["embedding"], it["embeddingSig"] = v, sig
            batch.update(svc.paths.item(it["id"]), {
                "embedding": vec(v), "embeddingSig": sig, "embeddingModel": model, "embeddingDim": dim, "embeddedAt": at})
            stats.embedded += 1
        if stats.embedded:
            batch.commit()
    stats.invalid, stats.api_calls, stats.est_tokens, stats.error = res.invalid, res.api_calls, res.est_tokens, res.error
    log(logger, logging.INFO, "rec_story_embeddings", stories=stats.stories, cached=stats.cached, embedded=stats.embedded,
        invalid=stats.invalid, failed=stats.failed, skippedEmpty=stats.skipped_empty, apiCalls=stats.api_calls,
        estTokens=stats.est_tokens, estCostUsd=res.est_cost(cfg.embed_price_per_million_tokens), model=model, dim=dim,
        error=stats.error, durationMs=timer.ms())
    return stats
