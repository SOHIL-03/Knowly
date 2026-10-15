"""B6 relevance score: exact formula from Architecture v3 section 7.1."""

import math
from datetime import UTC, datetime

import pytest

from app.pipeline.score import Story, UserSignals, novelty, novelty_fit, rank, score_story

AT = datetime(2026, 10, 15, tzinfo=UTC)


def unit(*xs: float) -> list[float]:
    n = math.sqrt(sum(x * x for x in xs))
    return [x / n for x in xs]


def story(sid="it_1", emb=None, covered=1, cluster=None, cat="tooling", ents=None, headline="Headline", blurb="Blurb", sender="s1"):
    return Story(sid, headline, blurb, cat, ents or [], sender, "Sender", emb, cluster or f"cl_{sid}", covered)


def test_novelty_fit_bell_curve_peaks_at_moderate_novelty():
    assert novelty_fit(0.6) == pytest.approx(1.0)
    assert novelty_fit(0.4) == pytest.approx(math.exp(-0.5))
    assert novelty_fit(0.0) == pytest.approx(math.exp(-(0.6**2) / 0.08))
    assert novelty_fit(0.6) > novelty_fit(0.9) > novelty_fit(1.0)


def test_novelty_uses_only_concepts_the_user_knows():
    s = unit(1, 0, 0)
    assert novelty(s, [("known", unit(1, 0, 0))]) == pytest.approx(0.0)
    assert novelty(s, [("orthogonal", unit(0, 1, 0))]) == pytest.approx(1.0)
    assert novelty(s, []) == 1.0


def test_exact_weighted_sum():
    user = UserSignals(
        project_embedding=unit(1, 0, 0),
        goals=[("evals", unit(0, 1, 0)), ("rag", unit(1, 1, 0))],
        trust_by_sender={"s1": 0.8},
        known_concepts=[("k", unit(0, 0, 1))],
    )
    emb = unit(1, 1, 0)
    sc = score_story(story(emb=emb, covered=2), user, AT)
    project_sim = 1 / math.sqrt(2)
    goal_sim = 1.0
    nfit = novelty_fit(1.0)  # orthogonal to the only known concept
    expected = 0.30 * project_sim + 0.25 * goal_sim + 0.20 * nfit + 0.10 * 0.8 + 0.10 * (2 / 3) + 0.05 * 0.0
    assert sc.score == pytest.approx(expected, abs=1e-4)
    assert sc.terms["goal_sim"] == pytest.approx(1.0)
    assert sc.reason == "Matches your goal: rag"


def test_mute_and_already_heard_penalties():
    user = UserSignals(project_embedding=unit(1, 0), mute_topics=["crypto"], heard_clusters={"cl_x"})
    base = score_story(story(emb=unit(1, 0)), user, AT)
    muted = score_story(story(emb=unit(1, 0), headline="Crypto exchange news"), user, AT)
    heard = score_story(story(emb=unit(1, 0), cluster="cl_x"), user, AT)
    assert muted.muted and muted.score == pytest.approx(base.score - 1.0, abs=1e-4)
    assert heard.score == pytest.approx(base.score - 0.5, abs=1e-4)


def test_mute_matches_whole_words_only():
    user = UserSignals(project_embedding=unit(1, 0), mute_topics=["AI"])
    assert not score_story(story(emb=unit(1, 0), headline="Maintaining big repos"), user, AT).muted
    assert score_story(story(emb=unit(1, 0), headline="New AI model"), user, AT).muted


def test_empty_profile_scores_by_popularity_only():
    user = UserSignals()
    a = score_story(story("a", covered=3), user, AT)
    b = score_story(story("b", covered=1), user, AT)
    assert a.score == pytest.approx(1.0) and b.score == pytest.approx(1 / 3, abs=1e-4)
    assert set(a.terms) == {"coverage", "mute_penalty", "already_heard"}


def test_missing_story_embedding_does_not_crash():
    user = UserSignals(project_embedding=unit(1, 0), goals=[("g", unit(0, 1))])
    sc = score_story(story(emb=None), user, AT)
    assert sc.terms["project_sim"] == 0.0 and sc.terms["novelty"] == 1.0


def test_project_reason_names_the_shared_entity():
    user = UserSignals(project_text="RAG app on pgvector and FastAPI", project_embedding=unit(1, 0))
    sc = score_story(story(emb=unit(1, 0), ents=["pgvector"]), user, AT)
    assert sc.reason == "Mentions pgvector, which your project uses"


def test_rank_drops_muted_and_marks_one_wildcard_outside_top():
    user = UserSignals(project_embedding=unit(1, 0, 0), goals=[("g", unit(1, 0, 0))], mute_topics=["funding"])
    stories = [story(f"it_{i}", emb=unit(1, 0.1 * i, 0)) for i in range(14)]
    stories.append(story("it_far", emb=unit(0, 0, 1), covered=3))
    stories.append(story("it_muted", emb=unit(1, 0, 0), cat="funding"))
    ranked = rank(stories, user, AT, top_n=12)
    ids = [s.story.id for s in ranked]
    assert "it_muted" not in ids
    wild = [s for s in ranked if s.wildcard]
    assert len(wild) == 1 and wild[0].story.id == "it_far"
    assert wild[0].reason.startswith("Wildcard")
    assert len(ranked) == 13


def test_rank_wildcard_off():
    user = UserSignals(project_embedding=unit(1, 0))
    ranked = rank([story(f"it_{i}", emb=unit(1, i)) for i in range(6)], user, AT, wildcard_on=False)
    assert not any(s.wildcard for s in ranked)
