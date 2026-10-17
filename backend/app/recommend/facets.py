"""Interest facets: the user's declared projects, goals, stack, interests, expertise and muted topics,
each with its own weight and embedding (users/{uid}/profile/main.facets[]).

Facets come only from what the user declared in their profile. Nothing is inferred or invented, no
language model is involved, and project, goals and the rest stay separate vectors (never averaged).
An embedding is reused while a facet's text, the model and the dimension are unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.logs import get_logger, log
from app.recommend.embedding import embed_resilient
from app.recommend.settings import FACET_TYPES, POSITIVE_TYPES, RecConfig
from app.recommend.vectors import clean_vector
from app.services import Services
from app.store import now, stable_id, unvec, vec

logger = get_logger(__name__)

PROJECT_ID = "f_project"
MAX_TEXT = {"project": 4000}
DEFAULT_MAX_TEXT = 200
MAX_PER_TYPE = {"project": 1, "goal": 5, "stack": 10, "interest": 10, "expertise": 10, "mute": 30}
# Profile field -> facet type for the list-valued declarations.
LIST_FIELDS = {"stack": "stack", "interests": "interest", "expertise": "expertise"}


@dataclass
class Facet:
    id: str
    text: str
    type: str
    weight: float
    embedding: list[float] | None
    created_at: datetime
    updated_at: datetime
    embed_text: str | None = None  # what was embedded when it differs from the displayed text
    text_hash: str = ""

    @property
    def positive(self) -> bool:
        return self.type in POSITIVE_TYPES


@dataclass
class FacetState:
    facets: list[Facet] = field(default_factory=list)
    prefs: dict[str, Any] = field(default_factory=dict)  # facetId -> {delta, updatedAt}: learned, kept apart from declared weights
    familiarity: dict[str, Any] = field(default_factory=dict)  # topic key -> {w, updatedAt}
    embedded: int = 0
    api_calls: int = 0
    embed_error: str | None = None

    @property
    def positives(self) -> list[Facet]:
        return [f for f in self.facets if f.positive]

    @property
    def mutes(self) -> list[Facet]:
        return [f for f in self.facets if f.type == "mute"]


def norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def _clean(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit] if text else None


def facet_id(ftype: str, text: str) -> str:
    return PROJECT_ID if ftype == "project" else stable_id(ftype, norm_text(text), prefix="f_")


def text_hash(model: str, dim: int, embed_text: str) -> str:
    return hashlib.sha256(f"{model}|{dim}|{embed_text}".encode()).hexdigest()[:16]


def declared_facets(profile: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """(type, text, embed_text) for every valid declaration in the profile, duplicates and malformed
    entries dropped. A positive facet whose text equals a muted topic is dropped: the mute wins."""
    out: list[tuple[str, str, str | None]] = []
    project = profile.get("project") if isinstance(profile.get("project"), dict) else {}
    ptext = _clean(project.get("text"), MAX_TEXT["project"])
    if ptext:
        summary = _clean(project.get("summary"), 1200)
        out.append(("project", ptext, " ".join(filter(None, [ptext, summary])) if summary else None))
    goals = profile.get("goals") if isinstance(profile.get("goals"), list) else []
    for g in goals:
        text = _clean(g.get("topic") if isinstance(g, dict) else g, DEFAULT_MAX_TEXT)
        if text:
            out.append(("goal", text, None))
    for key, ftype in LIST_FIELDS.items():
        raw = profile.get(key) if isinstance(profile.get(key), list) else []
        out += [(ftype, t, None) for x in raw if (t := _clean(x, DEFAULT_MAX_TEXT))]
    mutes = profile.get("muteTopics") if isinstance(profile.get("muteTopics"), list) else []
    mute_texts = [t for x in mutes if (t := _clean(x, DEFAULT_MAX_TEXT))]
    muted = {norm_text(t) for t in mute_texts}
    out = [(ft, t, e) for ft, t, e in out if norm_text(t) not in muted]
    out += [("mute", t, None) for t in mute_texts]
    seen: set[tuple[str, str]] = set()
    counts: dict[str, int] = {}
    kept = []
    for ftype, text, embed in out:
        key = (ftype, norm_text(text))
        if not key[1] or key in seen or counts.get(ftype, 0) >= MAX_PER_TYPE[ftype]:
            continue
        seen.add(key)
        counts[ftype] = counts.get(ftype, 0) + 1
        kept.append((ftype, text, embed))
    return kept


def parse_facets(raw: Any, dim: int | None = None) -> list[Facet]:
    """Stored facets, tolerating missing or malformed entries (they are skipped)."""
    out: list[Facet] = []
    for d in raw if isinstance(raw, list) else []:
        if not isinstance(d, dict) or d.get("type") not in FACET_TYPES or not isinstance(d.get("text"), str) or not d.get("id"):
            continue
        try:
            weight = float(d.get("weight", 1.0))
        except (TypeError, ValueError):
            continue
        at = d.get("updatedAt") if isinstance(d.get("updatedAt"), datetime) else now()
        out.append(Facet(
            id=str(d["id"]), text=d["text"], type=d["type"], weight=weight, embedding=clean_vector(unvec(d.get("embedding")), dim),
            created_at=d.get("createdAt") if isinstance(d.get("createdAt"), datetime) else at, updated_at=at,
            embed_text=d.get("embedText") if isinstance(d.get("embedText"), str) else None,
            text_hash=str(d.get("textHash") or "")))
    return out


def to_doc(f: Facet, model: str, dim: int) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": f.id, "text": f.text, "type": f.type, "weight": f.weight, "embedding": vec(f.embedding),
        "textHash": f.text_hash, "model": model, "dim": dim, "createdAt": f.created_at, "updatedAt": f.updated_at}
    if f.embed_text:
        doc["embedText"] = f.embed_text
    return doc


def reconcile(declared: list[tuple[str, str, str | None]], previous: list[Facet], legacy: dict[str, list[float]],
              cfg: RecConfig, model: str, dim: int, at: datetime) -> list[Facet]:
    """Facets for the declarations, reusing stored embeddings (and the legacy project/goal embeddings)
    whose text, model and dimension are unchanged. Facets that still need an embedding have embedding None."""
    prev = {f.id: f for f in previous}
    out: list[Facet] = []
    for ftype, text, embed in declared:
        fid = facet_id(ftype, text)
        etext = embed or text
        h = text_hash(model, dim, etext)
        old = prev.get(fid)
        reusable = old is not None and old.text_hash == h and old.embedding is not None
        embedding = old.embedding if reusable else clean_vector(legacy.get(etext), dim)
        out.append(Facet(
            id=fid, text=text, type=ftype,
            weight=old.weight if old else (cfg.weights.get(ftype, 1.0) if ftype != "mute" else 1.0),
            embedding=embedding, embed_text=embed, text_hash=h,
            created_at=old.created_at if old else at,
            updated_at=old.updated_at if (reusable and old and old.text == text) else at))
    return out


def sync_facets(svc: Services, uid: str, cfg: RecConfig | None = None) -> FacetState:
    """Builds the user's facets from the profile, embeds only the ones without a valid embedding, and
    stores the result when it changed. Embedding failures leave those facets without a vector."""
    cfg = cfg or RecConfig.from_settings(svc.settings)
    model, dim = svc.settings.gemini_embed_model, svc.settings.embed_dim
    ref = svc.paths.profile(uid)
    profile = ref.get().to_dict() or {}
    project = profile.get("project") if isinstance(profile.get("project"), dict) else {}
    legacy: dict[str, list[float]] = {}
    pv = unvec(project.get("embedding")) if project.get("embedding") is not None else None
    ptext = (project.get("text") or "").strip()
    if pv and ptext:
        legacy[" ".join(filter(None, [ptext, project.get("summary")]))] = pv
    for g in profile.get("goals") or []:
        gv = unvec(g.get("embedding")) if isinstance(g, dict) and g.get("embedding") is not None else None
        if gv and isinstance(g.get("topic"), str):
            legacy[g["topic"].strip()] = gv

    declared = declared_facets(profile)
    previous = parse_facets(profile.get("facets"), dim)
    at = now()
    facets = reconcile(declared, previous, legacy, cfg, model, dim, at)
    state = FacetState(facets=facets, prefs=dict(profile.get("facetPrefs") or {}), familiarity=dict(profile.get("familiarity") or {}))

    todo = [f for f in facets if f.embedding is None]
    if todo:
        res = embed_resilient(svc.embedder, [f.embed_text or f.text for f in todo], "query", cfg, dim)
        state.api_calls, state.embed_error = res.api_calls, res.error
        for f, v in zip(todo, res.vectors, strict=True):
            f.embedding = v
        state.embedded = sum(1 for v in res.vectors if v is not None)
        log(logger, logging.INFO, "rec_facets_embedded", uid=uid, facets=len(todo), embedded=state.embedded,
            invalid=res.invalid, apiCalls=res.api_calls, estTokens=res.est_tokens, error=res.error)

    docs = [to_doc(f, model, dim) for f in facets]
    stored = profile.get("facets") if isinstance(profile.get("facets"), list) else []
    changed = bool(todo) or len(stored) != len(docs) or any(
        (s.get("id"), s.get("text"), s.get("textHash"), s.get("weight")) != (d["id"], d["text"], d["textHash"], d["weight"])
        for s, d in zip(stored, docs, strict=False) if isinstance(s, dict))
    if changed and (declared or stored):
        ref.set({"facets": docs}, merge=True)
    log(logger, logging.INFO, "rec_facets_ready", uid=uid, positive=len(state.positives), mutes=len(state.mutes),
        withoutEmbedding=sum(1 for f in facets if f.embedding is None), written=changed)
    return state


def load_state(svc: Services, uid: str) -> FacetState:
    """The stored facets, preferences and familiarity, without embedding anything."""
    profile = svc.paths.profile(uid).get().to_dict() or {}
    return FacetState(facets=parse_facets(profile.get("facets"), svc.settings.embed_dim),
                      prefs=dict(profile.get("facetPrefs") or {}), familiarity=dict(profile.get("familiarity") or {}))
