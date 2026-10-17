"""The recommender's tunables as one immutable value, so the ranking code stays pure and testable."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings

FACET_TYPES = ("project", "stack", "goal", "interest", "expertise", "mute")
POSITIVE_TYPES = ("project", "stack", "goal", "interest", "expertise")
MAX_NOVELTY_WEIGHT = 0.3


@dataclass(frozen=True)
class RecConfig:
    ranking_version: str = "semantic-v1"
    final_count: int = 12
    weights: dict[str, float] = field(default_factory=lambda: {
        "project": 1.0, "goal": 0.85, "stack": 0.8, "interest": 0.7, "expertise": 0.5})
    weight_min: float = 0.1
    weight_max: float = 1.5
    secondary_min_ratio: float = 0.85
    secondary_max: int = 2
    novelty_weight: float = 0.15
    heard_penalty: float = 0.25
    mmr_lambda: float = 0.7
    duplicate_threshold: float = 0.93
    mute_exclude_gap: float = 0.11
    mute_penalty_gap: float = 0.06
    mute_baseline: float = 0.54
    mute_min_candidates: int = 8
    mute_penalty: float = 0.6
    mute_incidental_penalty: float = 0.1
    wildcard_enabled: bool = True
    wildcard_min_relevance: float = 0.25
    explain_min_relevance: float = 0.4
    excerpt_chars: int = 600
    embed_batch_size: int = 50
    embed_max_retries: int = 2
    embed_backoff_seconds: float = 1.0
    embed_timeout_seconds: float = 30.0
    embed_price_per_million_tokens: float | None = None
    feedback_steps: dict[str, float] = field(default_factory=lambda: {
        "save": 0.10, "ask": 0.05, "deeper": 0.08, "lessLikeThis": -0.15})
    feedback_max_delta: float = 0.5
    feedback_half_life_days: float = 30.0
    knew_step: float = 0.25
    familiarity_cap: float = 0.6

    @classmethod
    def from_settings(cls, s: Settings) -> RecConfig:
        lo, hi = sorted((s.rec_weight_min, s.rec_weight_max))
        return cls(
            ranking_version=s.rec_ranking_version,
            final_count=max(1, s.rec_final_count),
            weights={"project": s.rec_weight_project, "goal": s.rec_weight_goal, "stack": s.rec_weight_stack,
                     "interest": s.rec_weight_interest, "expertise": s.rec_weight_expertise},
            weight_min=lo, weight_max=hi,
            secondary_min_ratio=s.rec_secondary_min_ratio, secondary_max=max(0, s.rec_secondary_max),
            novelty_weight=min(max(s.rec_novelty_weight, 0.0), MAX_NOVELTY_WEIGHT),
            heard_penalty=max(0.0, s.rec_heard_penalty),
            mmr_lambda=min(max(s.rec_mmr_lambda, 0.0), 1.0),
            duplicate_threshold=s.rec_duplicate_threshold,
            mute_exclude_gap=s.rec_mute_exclude_gap, mute_penalty_gap=min(s.rec_mute_penalty_gap, s.rec_mute_exclude_gap),
            mute_baseline=s.rec_mute_baseline, mute_min_candidates=max(1, s.rec_mute_min_candidates),
            mute_penalty=s.rec_mute_penalty, mute_incidental_penalty=s.rec_mute_incidental_penalty,
            wildcard_enabled=s.rec_wildcard_enabled, wildcard_min_relevance=s.rec_wildcard_min_relevance,
            explain_min_relevance=s.rec_explain_min_relevance,
            excerpt_chars=max(0, s.rec_excerpt_chars), embed_batch_size=max(1, min(100, s.rec_embed_batch_size)),
            embed_max_retries=max(0, s.rec_embed_max_retries), embed_backoff_seconds=max(0.0, s.rec_embed_backoff_seconds),
            embed_timeout_seconds=max(1.0, s.rec_embed_timeout_seconds),
            embed_price_per_million_tokens=s.rec_embed_price_per_million_tokens,
            feedback_steps={"save": s.rec_feedback_save, "ask": s.rec_feedback_ask, "deeper": s.rec_feedback_deeper,
                            "lessLikeThis": s.rec_feedback_less},
            feedback_max_delta=abs(s.rec_feedback_max_delta),
            feedback_half_life_days=max(0.1, s.rec_feedback_half_life_days),
            knew_step=max(0.0, s.rec_knew_step), familiarity_cap=min(max(s.rec_familiarity_cap, 0.0), 1.0),
        )
