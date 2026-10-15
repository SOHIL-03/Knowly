"""B6 relevance score (Architecture v3 section 7.1), computed in code with no model call.

score(story, user) =
    0.30 * project_sim     cosine(story, project)
  + 0.25 * goal_sim        max over goals of cosine(story, goal)
  + 0.20 * novelty_fit     bell curve around moderate novelty
  + 0.10 * source_trust    newsletters/{id}.trust
  + 0.10 * coverage        min(coveredByCount, 3) / 3
  + 0.05 * topic_weight    learned from events
  - 1.00 * mute_penalty    1 if the story matches a mute topic
  - 0.50 * already_heard   1 if the same cluster was in the last 7 briefings

novelty(story) = 1 - max cosine(story, concepts with mastery >= 0.7)
novelty_fit    = exp(-(novelty - 0.6)^2 / (2 * 0.2^2))
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.ai.client import cosine
from app.learner.model import effective_topic_weight

W_PROJECT = 0.30
W_GOAL = 0.25
W_NOVELTY = 0.20
W_TRUST = 0.10
W_COVERAGE = 0.10
W_TOPIC = 0.05
P_MUTE = 1.00
P_HEARD = 0.50
NOVELTY_CENTER = 0.6
NOVELTY_SD = 0.2
KNOWN_MASTERY = 0.7
DEFAULT_TRUST = 0.5


@dataclass
class Story:
    id: str
    headline: str
    blurb: str
    category: str
    entities: list[str]
    sender_id: str
    sender_name: str
    embedding: list[float] | None
    cluster_id: str
    covered_by: int
    link_only: bool = False


@dataclass
class UserSignals:
    project_text: str = ""
    project_embedding: list[float] | None = None
    goals: list[tuple[str, list[float] | None]] = field(default_factory=list)
    mute_topics: list[str] = field(default_factory=list)
    topic_weights: dict[str, Any] = field(default_factory=dict)
    trust_by_sender: dict[str, float] = field(default_factory=dict)
    known_concepts: list[tuple[str, list[float] | None]] = field(default_factory=list)  # mastery >= 0.7
    heard_clusters: set[str] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not self.project_embedding and not any(e for _, e in self.goals)


@dataclass
class Scored:
    story: Story
    score: float
    terms: dict[str, float]
    reason: str
    muted: bool
    wildcard: bool = False


def novelty(story_emb: list[float] | None, known: list[tuple[str, list[float] | None]]) -> float:
    sims = [cosine(story_emb, e) for _, e in known if e]
    return 1.0 - max(sims) if sims and story_emb else 1.0


def novelty_fit(n: float) -> float:
    return math.exp(-((n - NOVELTY_CENTER) ** 2) / (2 * NOVELTY_SD**2))


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


def matches_mute(story: Story, mute_topics: list[str]) -> str | None:
    hay = _norm(" ".join([story.headline, story.blurb, story.category, *story.entities]))
    for topic in mute_topics:
        t = _norm(topic).strip()
        if t and f" {t} " in hay:
            return topic
    return None


def story_topics(story: Story) -> list[str]:
    return [story.category, *story.entities]


def score_story(story: Story, user: UserSignals, at: datetime) -> Scored:
    muted_by = matches_mute(story, user.mute_topics)
    coverage = min(story.covered_by, 3) / 3
    heard = 1.0 if story.cluster_id in user.heard_clusters else 0.0
    mute = 1.0 if muted_by else 0.0

    if user.is_empty:
        # Empty profile: score by popularity (coveredByCount) only.
        terms = {"coverage": coverage, "mute_penalty": mute, "already_heard": heard}
        total = coverage - P_MUTE * mute - P_HEARD * heard
        reason = f"Covered by {story.covered_by} of your newsletters" if story.covered_by > 1 else "Popular in your newsletters today"
        return Scored(story, round(total, 4), terms, reason, bool(muted_by))

    project_sim = cosine(story.embedding, user.project_embedding)
    goal_sims = [(label, cosine(story.embedding, e)) for label, e in user.goals if e]
    best_goal = max(goal_sims, key=lambda g: g[1]) if goal_sims else ("", 0.0)
    nov = novelty(story.embedding, user.known_concepts)
    nfit = novelty_fit(nov)
    trust = user.trust_by_sender.get(story.sender_id, DEFAULT_TRUST)
    topic_w = effective_topic_weight(user.topic_weights, story_topics(story), at)

    terms = {
        "project_sim": project_sim,
        "goal_sim": best_goal[1],
        "novelty": nov,
        "novelty_fit": nfit,
        "source_trust": trust,
        "coverage": coverage,
        "topic_weight": topic_w,
        "mute_penalty": mute,
        "already_heard": heard,
    }
    total = (
        W_PROJECT * project_sim
        + W_GOAL * best_goal[1]
        + W_NOVELTY * nfit
        + W_TRUST * trust
        + W_COVERAGE * coverage
        + W_TOPIC * topic_w
        - P_MUTE * mute
        - P_HEARD * heard
    )
    reason = build_reason(story, user, terms, best_goal[0])
    return Scored(story, round(total, 4), terms, reason, bool(muted_by))


def build_reason(story: Story, user: UserSignals, terms: dict[str, float], goal_label: str) -> str:
    """One-line reason from the top contributing positive term."""
    contributions = {
        "project": W_PROJECT * terms["project_sim"],
        "goal": W_GOAL * terms["goal_sim"],
        "novelty": W_NOVELTY * terms["novelty_fit"],
        "trust": W_TRUST * terms["source_trust"],
        "coverage": W_COVERAGE * terms["coverage"],
        "topic": W_TOPIC * terms["topic_weight"],
    }
    top = max(contributions, key=lambda k: contributions[k])
    if top == "project":
        mention = _project_overlap(story, user.project_text)
        return f"Mentions {mention}, which your project uses" if mention else "Close to what you are building"
    if top == "goal" and goal_label:
        return f"Matches your goal: {goal_label}"
    if top == "coverage" and story.covered_by > 1:
        return f"Covered by {story.covered_by} of your newsletters"
    if top == "trust":
        return f"From {story.sender_name}, a source you engage with"
    if top == "topic":
        return "Similar to stories you asked about or saved"
    return "New to you, but close to what you know"


def _project_overlap(story: Story, project_text: str) -> str | None:
    proj = _norm(project_text)
    for ent in story.entities:
        e = _norm(ent).strip()
        if len(e) > 2 and f" {e} " in proj:
            return ent
    return None


def rank(stories: list[Story], user: UserSignals, at: datetime, *, wildcard_on: bool = True, top_n: int = 12) -> list[Scored]:
    """Ranks stories, drops muted ones, and marks one wildcard from outside the top matches.

    Wildcard: high coverage, low project and goal similarity, not muted, not heard (7.1).
    """
    scored = [score_story(s, user, at) for s in stories]
    kept = sorted((s for s in scored if not s.muted), key=lambda s: s.score, reverse=True)
    if wildcard_on and len(kept) > top_n:
        outside = kept[top_n:]
        candidates = [
            s
            for s in outside
            if s.terms.get("already_heard", 0) == 0
        ]
        if candidates:
            def wild_key(s: Scored) -> float:
                rel = s.terms.get("project_sim", 0.0) + s.terms.get("goal_sim", 0.0)
                return s.terms.get("coverage", 0.0) - 0.5 * rel

            pick = max(candidates, key=wild_key)
            pick.wildcard = True
            pick.reason = "Wildcard: outside your usual topics"
            return kept[:top_n] + [pick]
    if wildcard_on and len(kept) >= 4 and not user.is_empty:
        # Fewer candidates than top_n: take the least project/goal-similar story of the bottom half.
        bottom = kept[len(kept) // 2 :]
        pick = min(bottom, key=lambda s: s.terms.get("project_sim", 0.0) + s.terms.get("goal_sim", 0.0))
        pick.wildcard = True
        pick.reason = "Wildcard: outside your usual topics"
    return kept[:top_n]
