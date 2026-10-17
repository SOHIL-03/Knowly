"""Profile writes: project, goals and mute topics with their embeddings (users/{uid}/profile/main)."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from app.ai.client import AiOutputInvalid, AiUnavailable
from app.logs import get_logger, log
from app.pipeline.enrich import fetch_article
from app.recommend.vectors import clean_vector
from app.services import Services
from app.store import now, unvec, vec

logger = get_logger(__name__)
MAX_GOALS = 5


class ProjectSummary(BaseModel):
    summary: str


def _short_list(values: list[str] | None, previous: Any, limit: int = 10) -> list[str]:
    """The cleaned list, or the previously stored one when the caller did not send this field."""
    if values is None:
        return [v for v in previous if isinstance(v, str)] if isinstance(previous, list) else []
    return [v.strip() for v in values if isinstance(v, str) and v.strip()][:limit]


def save_profile(svc: Services, uid: str, project_text: str, readme_url: str | None, goals: list[str],
                 mute_topics: list[str], stack: list[str] | None = None, interests: list[str] | None = None,
                 expertise: list[str] | None = None) -> dict[str, Any]:
    ref = svc.paths.profile(uid)
    prev = ref.get().to_dict() or {}
    goals = [g.strip() for g in goals if g.strip()][:MAX_GOALS]
    summary = (prev.get("project") or {}).get("summary") if (prev.get("project") or {}).get("readmeUrl") == readme_url else None
    if readme_url and summary is None:
        summary = _readme_summary(svc, readme_url)
    doc: dict[str, Any] = {
        "project": {"text": project_text.strip()[:4000], "readmeUrl": readme_url, "summary": summary, "embedding": None},
        "goals": [{"topic": g, "embedding": None} for g in goals],
        "muteTopics": [m.strip() for m in mute_topics if m.strip()][:30],
        "topicWeights": prev.get("topicWeights") or {},
        "lessLikeThisCounts": prev.get("lessLikeThisCounts") or {},
        # Declared extras for recommendations; omitted fields keep what was stored.
        "stack": _short_list(stack, prev.get("stack")),
        "interests": _short_list(interests, prev.get("interests")),
        "expertise": _short_list(expertise, prev.get("expertise")),
        # Facets with their embeddings, learned facet weights and familiarity are kept (app/recommend);
        # facets are rebuilt from the declarations above on the next briefing.
        "facets": prev.get("facets") or [],
        "facetPrefs": prev.get("facetPrefs") or {},
        "familiarity": prev.get("familiarity") or {},
        "updatedAt": now(),
    }
    ref.set(doc)
    status = embed_profile(svc, uid)
    return {"embeddingStatus": status}


def _readme_summary(svc: Services, url: str) -> str | None:
    res = fetch_article(url, timeout=svc.settings.enrich_timeout_seconds, max_chars=12000, client=svc.http)
    if not res.text:
        return None
    try:
        out = svc.llm.generate_json(
            "flash",
            "Summarise this project README in 3 sentences: what it does, its stack, and what is being built next. "
            "Use only facts from the text.",
            f"<article>\n{res.text}\n</article>", ProjectSummary, temperature=0.1,
        )
        return out.summary[:1200]
    except (AiUnavailable, AiOutputInvalid):
        return None


def embed_profile(svc: Services, uid: str) -> str:
    """Computes missing embeddings. Returns ok | missing (Gemini unavailable) | empty."""
    ref = svc.paths.profile(uid)
    prof = ref.get().to_dict() or {}
    project = prof.get("project") or {}
    goals = prof.get("goals") or []
    texts: list[str] = []
    want_project = bool((project.get("text") or "").strip()) and not unvec(project.get("embedding"))
    if want_project:
        texts.append(" ".join(filter(None, [project.get("text"), project.get("summary")])))
    missing_goals = [i for i, g in enumerate(goals) if not unvec(g.get("embedding"))]
    texts += [goals[i]["topic"] for i in missing_goals]
    if not texts:
        return "ok" if (want_project or goals or project.get("text")) else "empty"
    try:
        vecs = [clean_vector(v, svc.settings.embed_dim) for v in svc.embedder.embed(texts, task="query")]
    except AiUnavailable:
        log(logger, logging.WARNING, "profile_embedding_missing", uid=uid)
        return "missing"
    pos = 0
    if want_project:
        project["embedding"] = vec(vecs[0])
        pos = 1
    for i, v in zip(missing_goals, vecs[pos:], strict=True):
        goals[i]["embedding"] = vec(v)
    ref.update({"project": project, "goals": goals})
    return "ok"
