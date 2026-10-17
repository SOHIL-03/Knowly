"""Glue between the briefing pipeline and the recommender (B5 facets, B6 ranking, B7 persistence)."""

from __future__ import annotations

import logging
from typing import Any

from app.logs import Timer, exc_fields, get_logger, log
from app.pipeline.score import Scored, Story, UserSignals
from app.recommend.facets import FacetState, load_state, sync_facets
from app.recommend.ranking import NEUTRAL_REASON, WILDCARD_REASON, RankedStory, rank_semantic
from app.recommend.settings import RecConfig
from app.services import Services
from app.store import now

logger = get_logger(__name__)
LEGACY_VERSION = "legacy-v1"


def prepare_profile(svc: Services, uid: str) -> FacetState | None:
    """B5/B6 preparation: build the user's facets and embed the ones that lack a vector. Never raises; with
    the feature off or on any failure the briefing continues with the original scorer."""
    if not svc.settings.rec_enabled:
        return None
    try:
        return sync_facets(svc, uid)
    except Exception as exc:  # noqa: BLE001 - recommendations must not fail a briefing
        log(logger, logging.WARNING, "rec_facets_failed", uid=uid, **exc_fields(exc, frames=1))
        return None


def semantic_rank(svc: Services, uid: str, briefing_id: str, stories: list[Story], signals: UserSignals,
                  wildcard_on: bool) -> list[RankedStory] | None:
    """Ranked stories from the semantic ranker, or None when it cannot run (feature off, no embedded
    interest facet, no usable story vectors, or an unexpected error); the caller then uses the original scorer."""
    if not svc.settings.rec_enabled or not stories:
        return None
    cfg = RecConfig.from_settings(svc.settings)
    timer = Timer()
    try:
        state = load_state(svc, uid)
        result = rank_semantic(stories, state.facets, state.prefs, state.familiarity, signals.known_concepts,
                               signals.heard_clusters, now(), cfg, wildcard_on=wildcard_on)
    except Exception as exc:  # noqa: BLE001
        log(logger, logging.WARNING, "rec_rank_failed", uid=uid, briefingId=briefing_id, **exc_fields(exc, frames=1))
        return None
    if result is None:
        log(logger, logging.INFO, "rec_rank_unavailable", uid=uid, briefingId=briefing_id, stories=len(stories),
            facets=len(state.positives), reason="no embedded interest facet or no story vectors")
        return None
    log(logger, logging.INFO, "rec_ranked", uid=uid, briefingId=briefing_id, version=cfg.ranking_version, candidates=result.candidates,
        ranked=len(result.ranked), droppedDuplicates=result.dropped_duplicates, droppedMuted=result.dropped_muted,
        withoutVector=result.without_vector, positiveFacets=len(state.positives), muteFacets=len(state.mutes),
        mmrLambda=cfg.mmr_lambda, wildcard=sum(1 for r in result.ranked if r.wildcard), durationMs=timer.ms(),
        top=[{"id": r.story.id, "score": r.score, "facet": r.facet_id} for r in result.ranked[:3]])
    return result.ranked


def _r(x: float | None) -> float | None:
    return None if x is None else round(float(x), 4)


def ranked_payload(ranked: list[Scored]) -> list[dict[str, Any]]:
    """briefings/{id}.ranked[]: what was recommended and which of the user's interests matched. Holds no
    story text (the item documents do) and no explanation beyond the deterministic reason line."""
    out: list[dict[str, Any]] = []
    for r in ranked:
        row: dict[str, Any] = {"itemId": r.story.id, "score": _r(r.score), "reason": r.reason, "wildcard": bool(r.wildcard)}
        if isinstance(r, RankedStory):
            row |= {"semanticScore": _r(r.semantic_score), "rawSimilarity": _r(r.raw_similarity), "relevance": _r(r.relevance),
                    "facetId": r.facet_id, "facetType": r.facet_type, "facetText": r.facet_text,
                    "matchedFacets": r.matched_facets, "noveltyScore": _r(r.novelty_score), "rankingVersion": r.ranking_version,
                    # True when the reason line names the matched facet (a weak match gets a neutral line instead)
                    "explained": r.reason not in (NEUTRAL_REASON, WILDCARD_REASON)}
        else:
            row["rankingVersion"] = LEGACY_VERSION
        out.append(row)
    return out


def ranking_mode(ranked: list[Scored]) -> str:
    return "semantic" if any(isinstance(r, RankedStory) for r in ranked) else "legacy"
