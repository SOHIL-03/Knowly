"""B6, semantic version: deterministic ranking of candidate stories from embeddings.

For one user and one candidate set (about 20 to 60 stories):

1. Facet match. facet_score = max(0, cosine(story, facet)) x effective facet weight, for every positive
   facet separately (facets are never averaged into one profile vector). semantic_score is the best
   facet's score; the next best facets within a ratio of it are kept as secondary matches for display and
   never add to the score, so an unrelated interest cannot dilute a strong match.
2. Mutes, separate from relevance. A story whose headline, category or extracted entities contain a muted
   phrase, or whose cosine to a muted topic is far above the candidates' median cosine to it (the exclude
   gap), is dropped: it is mainly about the muted topic. A smaller gap, or a muted phrase only in the blurb
   (an incidental mention), is penalised instead. Relevance never overrides a mute.
3. Relevance is min-max normalised over the candidates that survive, because raw cosines have no universal
   scale. Novelty (only where there is data: known concepts or "knew this already" feedback) is a small
   share of the final score; stories from clusters heard in recent briefings are penalised.
4. Near-duplicate stories (same id, URL, headline text or very high cosine) collapse to one.
5. Selection by maximal marginal relevance: lambda x score - (1 - lambda) x (highest cosine to a story
   already picked). An optional wildcard is a different-looking story that still clears a relevance floor.

Everything is plain Python on vectors already in memory: no model or API call happens here.
Limitation: embeddings cannot tell a story about a muted topic from one that only discusses it at length,
so mutes are conservative (exclusion needs a large gap or a headline, category or entity match). The gaps are
calibrated for one embedding model and must be re-checked when it changes; a story that discusses a muted
topic at length but is mainly about something else can still be penalised or dropped.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime

from app.pipeline.score import Scored, Story, novelty
from app.pipeline.text import url_key
from app.recommend.facets import Facet, norm_text
from app.recommend.feedback import effective_weight, familiarity_for, story_topics
from app.recommend.settings import RecConfig
from app.recommend.vectors import clean_vector, cos, dot, unit

REASON_TEMPLATES = {
    "project": "Recommended because it matches your project: {t}.",
    "goal": "Matches your learning goal: {t}.",
    "stack": "Related to your technology stack: {t}.",
    "interest": "Matches your interest: {t}.",
    "expertise": "Builds on your expertise: {t}.",
}
NEUTRAL_REASON = "Selected from today's newsletters"
WILDCARD_REASON = "Wildcard: outside your usual topics"
REASON_CHARS = 90


@dataclass
class RankedStory(Scored):
    semantic_score: float | None = None
    raw_similarity: float | None = None
    facet_id: str | None = None
    facet_text: str | None = None
    facet_type: str | None = None
    matched_facets: list[dict] = field(default_factory=list)
    novelty_score: float | None = None
    relevance: float = 0.0
    mute_similarity: float | None = None
    ranking_version: str = ""


@dataclass
class SemanticResult:
    ranked: list[RankedStory]
    dropped_duplicates: int = 0
    dropped_muted: int = 0
    candidates: int = 0
    without_vector: int = 0


# --- explanations ------------------------------------------------------------------------------

def shorten(text: str, limit: int = REASON_CHARS) -> str:
    t = " ".join(text.split()).rstrip(".!?;, ")
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0].rstrip(".!?;, ") + "…"


def explain(facet_type: str | None, facet_text: str | None) -> str:
    """Reason built only from the user's own facet text; neutral when no facet qualifies."""
    template = REASON_TEMPLATES.get(facet_type or "")
    return template.format(t=shorten(facet_text)) if template and facet_text else NEUTRAL_REASON


# --- duplicates ---------------------------------------------------------------------------------

def content_hash(story: Story) -> str:
    return hashlib.sha256(norm_text(story.headline).encode()).hexdigest()[:16]


def dedupe(stories: list[Story], vectors: dict[str, list[float]], threshold: float) -> tuple[list[Story], dict[str, str]]:
    """Collapses stories that are the same story: equal id, canonical URL or normalised headline, or a
    cosine at or above the threshold. Returns (kept, dropped id -> kept id). The kept story prefers one with
    fetched article text, then wider coverage, then the smaller id. Merely related stories are kept."""
    parent = {s.id: s.id for s in stories}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    seen_url: dict[str, str] = {}
    seen_hash: dict[str, str] = {}
    for s in stories:
        for table, key in ((seen_url, url_key(s.url)), (seen_hash, content_hash(s) if norm_text(s.headline) else None)):
            if key:
                if key in table:
                    union(s.id, table[key])
                else:
                    table[key] = s.id
    units = {k: u for k, v in vectors.items() if (u := unit(v)) is not None}
    ordered = sorted((s for s in stories if s.id in units), key=lambda s: s.id)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if dot(units[a.id], units[b.id]) >= threshold:
                union(a.id, b.id)
    groups: dict[str, list[Story]] = {}
    for s in stories:
        groups.setdefault(find(s.id), []).append(s)
    kept: list[Story] = []
    dropped: dict[str, str] = {}
    for members in groups.values():
        best = min(members, key=lambda s: (s.link_only, -s.covered_by, s.id))
        kept.append(best)
        dropped.update({m.id: best.id for m in members if m.id != best.id})
    order = {s.id: n for n, s in enumerate(stories)}
    return sorted(kept, key=lambda s: order[s.id]), dropped


# --- mutes --------------------------------------------------------------------------------------

def _contains(haystack: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {haystack} "


@dataclass
class MuteVerdict:
    similarity: float | None  # raw cosine to the closest muted topic (by gap)
    gap: float | None  # that cosine minus the candidates' median cosine to the same topic
    headline_hit: bool
    incidental_hit: bool
    penalty: float
    excluded: bool


def mute_baselines(vectors: dict[str, list[float]], mutes: list[Facet], cfg: RecConfig) -> dict[str, float]:
    """Per muted topic, the median cosine of the candidates to it (the configured baseline when there are too
    few candidates for a stable median). Measuring against it makes the rule independent of how the topic is
    phrased and of the embedding model's usual cosine range."""
    out: dict[str, float] = {}
    for m in mutes:
        sims = [cos(v, m.embedding) for v in vectors.values()] if m.embedding else []
        out[m.id] = statistics.median(sims) if len(sims) >= cfg.mute_min_candidates else cfg.mute_baseline
    return out


def mute_verdict(story: Story, vector: list[float] | None, mutes: list[Facet], cfg: RecConfig,
                 baselines: dict[str, float] | None = None) -> MuteVerdict:
    scored = [(cos(vector, m.embedding) - (baselines or {}).get(m.id, cfg.mute_baseline), cos(vector, m.embedding))
              for m in mutes if m.embedding and vector]
    gap, similarity = max(scored) if scored else (None, None)
    head = norm_text(story.headline)
    subjects = norm_text(" ".join([story.category, *story.entities]))  # what the parser says the story is about
    body = norm_text(story.blurb)
    phrases = [norm_text(m.text) for m in mutes]
    headline_hit = any(_contains(head, p) for p in phrases)
    subject_hit = any(_contains(subjects, p) for p in phrases)
    incidental = not (headline_hit or subject_hit) and any(_contains(body, p) for p in phrases)
    excluded = headline_hit or subject_hit or (gap is not None and gap >= cfg.mute_exclude_gap)
    penalty = 0.0
    if not excluded:
        if gap is not None and gap >= cfg.mute_penalty_gap:
            span = max(cfg.mute_exclude_gap - cfg.mute_penalty_gap, 1e-9)
            penalty += cfg.mute_penalty * min(1.0, (gap - cfg.mute_penalty_gap) / span)
        if incidental:
            penalty += cfg.mute_incidental_penalty
    return MuteVerdict(similarity, gap, headline_hit, incidental, penalty, excluded)


# --- ranking ------------------------------------------------------------------------------------

def _minmax(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def rank_semantic(stories: list[Story], facets: list[Facet], prefs: dict, familiarity: dict, known_concepts: list,
                  heard_clusters: set[str], at: datetime, cfg: RecConfig, *, wildcard_on: bool = True) -> SemanticResult | None:
    """The final ranked list, or None when semantic ranking is not possible (no embedded positive facet, or
    no story with a usable vector); the caller then falls back to the original scorer."""
    positives = [f for f in facets if f.positive and f.embedding]
    if not positives:
        return None
    dim = len(positives[0].embedding or [])
    positives = [f for f in positives if len(f.embedding or []) == dim]  # incompatible dimensions are ignored
    mutes = [replace(m, embedding=m.embedding if m.embedding and len(m.embedding) == dim else None)
             for m in facets if m.type == "mute"]
    vectors = {s.id: v for s in stories if (v := clean_vector(s.embedding, dim)) is not None}
    if not vectors:
        return None

    units = {k: u for k, v in vectors.items() if (u := unit(v)) is not None}
    facet_units = {f.id: unit(f.embedding) for f in positives}
    order = {f.id: n for n, f in enumerate(positives)}
    kept, dropped = dedupe(stories, vectors, cfg.duplicate_threshold)
    weights = {f.id: effective_weight(f, prefs, at, cfg) for f in positives}
    known = [(n, e) for n, e in known_concepts if e]

    baselines = mute_baselines({s.id: vectors[s.id] for s in kept if s.id in vectors}, mutes, cfg)
    rows: list[RankedStory] = []
    dropped_muted = 0
    for s in kept:
        v = vectors.get(s.id)
        verdict = mute_verdict(s, v, mutes, cfg, baselines)
        if verdict.excluded:
            dropped_muted += 1
            continue
        row = RankedStory(story=s, score=0.0, terms={}, reason=NEUTRAL_REASON, muted=False, ranking_version=cfg.ranking_version,
                          mute_similarity=verdict.similarity)
        if v is not None:
            su = units[s.id]
            scored = sorted(((max(0.0, raw) * weights[f.id], raw, f) for raw, f in ((dot(su, facet_units[f.id]), f) for f in positives)),
                            key=lambda t: (-t[0], order[t[2].id]))
            best_score, best_raw, best = scored[0]
            row.semantic_score, row.raw_similarity = best_score, best_raw
            row.facet_id, row.facet_text, row.facet_type = best.id, best.text, best.type
            row.matched_facets = [
                {"facetId": f.id, "type": f.type, "text": f.text, "similarity": round(raw, 4), "score": round(sc, 4)}
                for sc, raw, f in scored[: 1 + cfg.secondary_max]
                if f.id == best.id or (best_score > 0 and sc >= cfg.secondary_min_ratio * best_score)]
        fam = familiarity_for(story_topics({"category": s.category, "entities": s.entities}), familiarity, at, cfg)
        if known or fam > 0:
            seen = min(1.0, max(0.0, novelty(v, known))) if known and v is not None else 1.0  # novelty() can leave [0, 1] when cosines are negative
            row.novelty_score = seen * (1.0 - fam)
        row.terms = {"mute_penalty": round(verdict.penalty, 4), "already_heard": 1.0 if s.cluster_id in heard_clusters else 0.0}
        rows.append(row)

    with_sem = [r for r in rows if r.semantic_score is not None]
    for r, rel in zip(with_sem, _minmax([r.semantic_score or 0.0 for r in with_sem]), strict=True):
        r.relevance = rel
    for r in rows:
        penalty = r.terms["mute_penalty"] + cfg.heard_penalty * r.terms["already_heard"]
        base = r.relevance if r.novelty_score is None else (1 - cfg.novelty_weight) * r.relevance + cfg.novelty_weight * r.novelty_score
        r.score = round(base - penalty, 4)
        r.terms |= {"relevance": round(r.relevance, 4)} | ({"novelty": round(r.novelty_score, 4)} if r.novelty_score is not None else {})
        if r.semantic_score is not None and r.relevance >= cfg.explain_min_relevance and (r.raw_similarity or 0) > 0:
            r.reason = explain(r.facet_type, r.facet_text)

    selected = _mmr(rows, units, cfg)
    if wildcard_on and cfg.wildcard_enabled:
        found = _wildcard(rows, selected, units, cfg)
        if found is not None:
            wild, extra = found
            wild.wildcard = True
            wild.reason = WILDCARD_REASON
            if extra:
                selected.append(wild)
    return SemanticResult(ranked=selected, dropped_duplicates=len(dropped), dropped_muted=dropped_muted, candidates=len(stories),
                          without_vector=sum(1 for s in stories if s.id not in vectors))


def _mmr(rows: list[RankedStory], vectors: dict[str, list[float]], cfg: RecConfig) -> list[RankedStory]:
    """Maximal marginal relevance: repeatedly take the story with the best lambda x score minus
    (1 - lambda) x its highest cosine to the stories already taken. Ties break on coverage, then id.
    `vectors` are unit vectors, so each cosine is one dot product; a story's redundancy is updated once per pick."""
    pool = sorted(rows, key=lambda r: (-r.score, -r.story.covered_by, r.story.id))
    redundancy = {r.story.id: 0.0 for r in pool}
    selected: list[RankedStory] = []
    while pool and len(selected) < cfg.final_count:
        def mmr(r: RankedStory) -> float:
            return cfg.mmr_lambda * r.score - (1 - cfg.mmr_lambda) * redundancy[r.story.id]

        best = max(pool, key=mmr)  # the pool is sorted, and max() keeps the first of equal values
        best.terms["mmr"] = round(mmr(best), 4)
        selected.append(best)
        pool.remove(best)
        taken = vectors.get(best.story.id)
        if taken is not None:
            for r in pool:
                sim = dot(taken, vectors.get(r.story.id))
                if sim > redundancy[r.story.id]:
                    redundancy[r.story.id] = sim
    return selected


def _wildcard(rows: list[RankedStory], selected: list[RankedStory], vectors: dict[str, list[float]],
              cfg: RecConfig) -> tuple[RankedStory, bool] | None:
    """A story that looks different from the strongest picks yet still clears the relevance floor and has no
    mute penalty or recent-briefing repeat. Returns (story, is_extra): a story from outside the picked list is
    added after it; when every candidate already fits in the list (the common small case) one of the weaker
    picks in the lower half is flagged instead, as the original scorer did. Never forced."""
    if not selected:
        return None
    chosen = {r.story.id for r in selected}

    def eligible(r: RankedStory) -> bool:
        return (r.story.id in vectors and r.relevance >= cfg.wildcard_min_relevance
                and r.terms["mute_penalty"] == 0 and r.terms["already_heard"] == 0)

    def pick(candidates: list[RankedStory], anchors: list[RankedStory]) -> RankedStory | None:
        candidates = sorted(candidates, key=lambda r: r.story.id)
        if not candidates or not anchors:
            return None

        def key(r: RankedStory) -> tuple[float, float]:
            distance = 1.0 - max(max(0.0, dot(vectors[r.story.id], vectors.get(a.story.id))) for a in anchors)
            return (distance + 0.5 * min(r.story.covered_by, 3) / 3, r.relevance)

        return max(candidates, key=key)  # sorted by id above, so equal keys resolve to the smaller id

    outside = pick([r for r in rows if r.story.id not in chosen and eligible(r)], selected)
    if outside is not None:
        return outside, True
    if len(selected) >= 4:
        half = len(selected) // 2
        inside = pick([r for r in selected[half:] if eligible(r)], selected[:half])
        if inside is not None:
            return inside, False
    return None
