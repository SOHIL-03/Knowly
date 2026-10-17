"""Deterministic semantic ranking: facet scoring, mutes, duplicates, MMR, novelty, wildcard (app/recommend/ranking.py)."""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.pipeline.score import Story
from app.recommend.facets import Facet
from app.recommend.ranking import (
    NEUTRAL_REASON,
    WILDCARD_REASON,
    dedupe,
    explain,
    mute_verdict,
    rank_semantic,
    shorten,
)
from app.recommend.settings import RecConfig

AT = datetime(2026, 10, 15, tzinfo=UTC)
DIM = 6


def unit(*xs: float) -> list[float]:
    xs = list(xs) + [0.0] * (DIM - len(xs))
    n = math.sqrt(sum(x * x for x in xs))
    return [x / n for x in xs]


def S(sid, emb, headline=None, blurb="", url=None, covered=1, cluster=None, cat="tooling", ents=None, link_only=False):
    return Story(sid, headline or f"Story {sid}", blurb, cat, ents or [], "s1", "Sender", emb, cluster or f"cl_{sid}", covered,
                 link_only, url)


def F(fid, ftype, text, emb, weight=1.0):
    return Facet(fid, text, ftype, weight, emb, AT, AT)


def rank(stories, facets, *, prefs=None, fam=None, known=(), heard=(), wildcard=False, **cfg):
    cfg = RecConfig(**{"wildcard_enabled": wildcard, "mmr_lambda": 1.0, "duplicate_threshold": 1.01, **cfg})
    return rank_semantic(stories, facets, prefs or {}, fam or {}, list(known), set(heard), AT, cfg, wildcard_on=wildcard)


def ids(result):
    return [r.story.id for r in result.ranked]


# --- facet scoring ------------------------------------------------------------------------------

def test_facet_score_is_cosine_times_weight_and_semantic_score_is_the_best_facet():
    facets = [F("p", "project", "RAG bot", unit(1, 0), 1.0), F("g", "goal", "LLM evals", unit(0, 1), 0.85)]
    story = S("a", unit(1, 1))
    [row] = rank([story], facets).ranked
    c = 1 / math.sqrt(2)
    assert row.raw_similarity == pytest.approx(c)
    assert row.semantic_score == pytest.approx(c * 1.0)  # project: cos 0.707 x 1.0 beats goal: 0.707 x 0.85
    assert (row.facet_id, row.facet_type, row.facet_text) == ("p", "project", "RAG bot")
    assert [m["facetId"] for m in row.matched_facets] == ["p", "g"]
    assert row.matched_facets[1]["score"] == pytest.approx(c * 0.85, abs=1e-4)


def test_facets_are_not_averaged_a_goal_match_is_not_diluted_by_an_unrelated_project():
    facets = [F("p", "project", "Kubernetes ops", unit(1, 0, 0)), F("g", "goal", "LLM evals", unit(0, 1, 0))]
    [row] = rank([S("a", unit(0, 1, 0))], facets).ranked
    assert row.semantic_score == pytest.approx(1.0) and row.facet_id == "g"
    assert [m["facetId"] for m in row.matched_facets] == ["g"]  # the unrelated project is not listed as a match


def test_secondary_matches_are_kept_only_when_close_and_never_add_to_the_score():
    facets = [F("a", "goal", "A", unit(1, 0, 0)), F("b", "goal", "B", unit(1, 0.2, 0)), F("c", "goal", "C", unit(0, 0, 1))]
    [row] = rank([S("s", unit(1, 0.1, 0))], facets, secondary_min_ratio=0.9).ranked
    listed = [m["facetId"] for m in row.matched_facets]
    assert sorted(listed) == ["a", "b"] and "c" not in listed
    assert row.semantic_score == pytest.approx(max(m["score"] for m in row.matched_facets), abs=1e-4)
    one = rank([S("s", unit(1, 0.1, 0))], facets, secondary_max=0).ranked[0]
    assert len(one.matched_facets) == 1 and one.semantic_score == pytest.approx(row.semantic_score)


def test_negative_cosine_never_counts_as_relevance():
    [row] = rank([S("a", unit(-1, 0))], [F("g", "goal", "G", unit(1, 0))]).ranked
    assert row.raw_similarity == pytest.approx(-1.0) and row.semantic_score == 0.0


def test_learned_weights_change_the_ranking_but_not_the_declared_weight():
    facets = [F("p", "project", "P", unit(1, 0)), F("g", "goal", "G", unit(0, 1))]
    stories = [S("near_p", unit(1, 0.1)), S("near_g", unit(0.2, 1))]
    assert ids(rank(stories, facets)) == ["near_p", "near_g"]
    prefs = {"g": {"delta": 0.5, "updatedAt": AT}, "p": {"delta": -0.5, "updatedAt": AT}}
    assert ids(rank(stories, facets, prefs=prefs)) == ["near_g", "near_p"]
    assert facets[0].weight == 1.0 and facets[1].weight == 1.0


def test_relevance_is_normalised_over_the_candidates_not_a_fixed_cosine_scale():
    facets = [F("g", "goal", "G", unit(1, 0))]
    stories = [S("hi", unit(1, 0.1)), S("mid", unit(1, 1)), S("lo", unit(1, 3))]
    rows = {r.story.id: r for r in rank(stories, facets).ranked}
    assert rows["hi"].relevance == pytest.approx(1.0) and rows["lo"].relevance == pytest.approx(0.0)
    assert 0 < rows["mid"].relevance < 1
    [only] = rank([S("x", unit(1, 5))], facets).ranked  # a single candidate has no spread: neutral, not 0 or 1
    assert only.relevance == 0.5


# --- explanations -------------------------------------------------------------------------------

def test_explanations_come_from_the_matched_facet_text_only():
    assert explain("project", "Building a RAG chatbot using Python.") == "Recommended because it matches your project: Building a RAG chatbot using Python."
    assert explain("goal", "LLM evaluation") == "Matches your learning goal: LLM evaluation."
    assert explain("stack", "PostgreSQL and pgvector") == "Related to your technology stack: PostgreSQL and pgvector."
    assert explain("interest", "Vector search").startswith("Matches your interest")
    assert explain("expertise", "Python").startswith("Builds on your expertise")
    assert explain(None, None) == NEUTRAL_REASON and explain("mute", "crypto") == NEUTRAL_REASON
    assert shorten("word " * 60).endswith("…") and len(shorten("word " * 60)) <= 92


def test_weakly_matched_stories_get_a_neutral_reason():
    facets = [F("g", "goal", "LLM evals", unit(1, 0))]
    rows = {r.story.id: r for r in rank([S("hi", unit(1, 0.1)), S("lo", unit(1, 4))], facets).ranked}
    assert rows["hi"].reason == "Matches your learning goal: LLM evals." and rows["lo"].reason == NEUTRAL_REASON


# --- mutes ----------------------------------------------------------------------------------------

MUTE = F("m", "mute", "Cryptocurrency news", unit(0, 0, 1))
GOAL = F("g", "goal", "LLM evals", unit(1, 0, 0))
CFG = RecConfig(mute_exclude_gap=0.11, mute_penalty_gap=0.06, mute_baseline=0.54, mute_min_candidates=8, mute_penalty=0.6,
                mute_incidental_penalty=0.1)


def test_a_story_mainly_about_a_muted_topic_is_dropped_whatever_its_relevance():
    stories = [S("ok", unit(1, 0, 0)), S("muted", unit(1, 0, 1.2))]  # similar to the goal AND to the mute (cos 0.77)
    result = rank(stories, [GOAL, MUTE])
    assert ids(result) == ["ok"] and result.dropped_muted == 1


def test_a_headline_that_names_the_muted_topic_is_dropped_even_without_embeddings_agreeing():
    stories = [S("ok", unit(1, 0, 0)), S("title", unit(1, 0, 0), headline="Cryptocurrency news roundup")]
    assert ids(rank(stories, [GOAL, MUTE])) == ["ok"]


def test_a_muted_phrase_among_the_extracted_entities_or_category_is_treated_as_the_subject():
    stories = [S("ok", unit(1, 0, 0)), S("ent", unit(1, 0.2, 0), ents=["Cryptocurrency news", "Gemini"]),
               S("cat", unit(1, 0.4, 0), cat="cryptocurrency news")]
    assert ids(rank(stories, [GOAL, MUTE])) == ["ok"]


def test_a_muted_phrase_only_in_the_blurb_is_an_incidental_mention_and_is_penalised_not_dropped():
    plain, incidental = S("plain", unit(1, 0, 0)), S("inc", unit(1, 0, 0), blurb="Sponsors include a cryptocurrency news site.")
    rows = {r.story.id: r for r in rank([plain, incidental], [GOAL, MUTE]).ranked}
    assert set(rows) == {"plain", "inc"} and rows["inc"].score < rows["plain"].score
    assert rows["plain"].score - rows["inc"].score == pytest.approx(CFG.mute_incidental_penalty, abs=1e-3)


def test_penalty_grows_between_the_penalty_and_exclude_gaps():
    def verdict(cosine):  # a story whose cosine to the muted topic is `cosine`; the baseline is 0.54
        v = unit(math.sqrt(1 - cosine**2), 0, cosine)
        return mute_verdict(S("x", v), v, [MUTE], CFG)

    assert verdict(0.55).penalty == 0 and not verdict(0.55).excluded  # not above the baseline by the penalty gap
    assert 0 < verdict(0.62).penalty < verdict(0.64).penalty < CFG.mute_penalty
    assert verdict(0.62).gap == pytest.approx(0.08) and verdict(0.62).similarity == pytest.approx(0.62)
    assert verdict(0.66).excluded and verdict(0.70).excluded


def test_the_gap_is_measured_from_the_candidates_median_when_there_are_enough_of_them():
    facets = [GOAL, MUTE]
    fillers = [S(f"f{i}", unit(1, 0.01 * i, 0.9)) for i in range(10)]  # every candidate is fairly close to the muted topic
    outlier = S("far_out", unit(1, 0.2, 1.8))
    result = rank(fillers + [outlier], facets, duplicate_threshold=1.01)
    assert "far_out" not in ids(result) and len(result.ranked) == 10  # only the story that stands out is dropped
    assert rank(fillers[:3] + [outlier], facets, duplicate_threshold=1.01).dropped_muted >= 3  # too few candidates: the fixed baseline


def test_a_highly_relevant_story_does_not_override_a_mute():
    stories = [S("best", unit(1, 0, 0.1)), S("worse", unit(1, 1, 0))]
    penalised = S("pen", unit(1, 0, 0.8), headline="Great LLM evals guide", blurb="by a cryptocurrency news writer")
    rows = {r.story.id: r for r in rank(stories + [penalised], [GOAL, MUTE]).ranked}
    assert rows["pen"].terms["mute_penalty"] > 0 and rows["pen"].score < rows["best"].score


def test_a_mute_without_an_embedding_still_works_by_phrase_and_a_wrong_dimension_mute_is_ignored_for_similarity():
    no_vec = F("m", "mute", "Cryptocurrency news", None)
    bad_dim = F("m2", "mute", "Politics", [1.0, 0.0])
    stories = [S("ok", unit(1, 0, 0)), S("t", unit(1, 0.2, 0), headline="Cryptocurrency news today"),
               S("p", unit(1, 0.4, 0), headline="Elections today")]
    result = rank(stories, [GOAL, no_vec, bad_dim])
    assert sorted(ids(result)) == ["ok", "p"] and all(r.mute_similarity is None for r in result.ranked)


def test_positive_and_mute_signals_stay_separate_in_the_output():
    [row] = rank([S("a", unit(1, 0, 0.3))], [GOAL, MUTE]).ranked
    assert row.semantic_score == pytest.approx(cosine(unit(1, 0, 0.3), unit(1, 0, 0)) * GOAL.weight)
    assert row.mute_similarity == pytest.approx(cosine(unit(1, 0, 0.3), unit(0, 0, 1)))


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


# --- duplicates -------------------------------------------------------------------------------------

def test_dedupe_merges_same_url_same_headline_and_near_identical_vectors_but_keeps_related_stories():
    a = S("a", unit(1, 0), headline="pgvector 0.8 released", url="https://x.example/a/")
    same_url = S("b", unit(0, 1), headline="Different words", url="https://X.example/a")
    same_head = S("c", unit(0, 0, 1), headline="PgVector 0.8 Released!")
    near = S("d", unit(1, 0.05), headline="Totally other title")
    related = S("e", unit(1, 0.6), headline="Postgres vector indexes explained")
    kept, dropped = dedupe([a, same_url, same_head, near, related], {s.id: s.embedding for s in (a, same_url, same_head, near, related)}, 0.93)
    assert [s.id for s in kept] == ["a", "e"] and dropped == {"b": "a", "c": "a", "d": "a"}


def test_dedupe_keeps_the_copy_with_article_text_then_wider_coverage():
    link_only = S("a", unit(1, 0), url="https://x.example/p", link_only=True, covered=3)
    full = S("b", unit(1, 0), url="https://x.example/p", link_only=False, covered=1)
    assert [s.id for s in dedupe([link_only, full], {}, 0.93)[0]] == ["b"]
    wide = S("c", unit(1, 0), url="https://y.example/p", covered=3)
    narrow = S("d", unit(1, 0), url="https://y.example/p", covered=1)
    assert [s.id for s in dedupe([narrow, wide], {}, 0.93)[0]] == ["c"]


def test_duplicates_are_removed_from_the_ranking():
    facets = [F("g", "goal", "G", unit(1, 0))]
    stories = [S("a", unit(1, 0.1), headline="Same story"), S("b", unit(1, 0.1), headline="same STORY"), S("c", unit(0.2, 1))]
    result = rank(stories, facets, duplicate_threshold=0.93)
    assert ids(result) == ["a", "c"] and result.dropped_duplicates == 1


# --- MMR ---------------------------------------------------------------------------------------------

def test_mmr_demotes_a_near_copy_of_an_already_picked_story():
    facets = [F("g", "goal", "G", unit(1, 0, 0))]
    stories = [S("top", unit(1, 0.05, 0)), S("twin", unit(1, 0.08, 0)), S("other", unit(0.8, 0, 1)), S("floor", unit(0, 0, 0, 1))]
    assert ids(rank(stories, facets, mmr_lambda=1.0)) == ["top", "twin", "other", "floor"]
    diverse = ids(rank(stories, facets, mmr_lambda=0.4))
    assert diverse[0] == "top" and diverse.index("other") < diverse.index("twin")


def test_lambda_one_is_pure_relevance_order():
    facets = [F("g", "goal", "G", unit(1, 0, 0))]
    stories = [S(f"s{i}", unit(1, 0.1 * i, 0.3 * (i % 2))) for i in range(8)]
    result = rank(stories, facets, mmr_lambda=1.0, duplicate_threshold=1.01)
    scores = [r.score for r in result.ranked]
    assert scores == sorted(scores, reverse=True)


def test_final_count_limits_the_list():
    facets = [F("g", "goal", "G", unit(1, 0))]
    stories = [S(f"s{i}", unit(1, 0.1 * i)) for i in range(10)]
    assert len(rank(stories, facets, final_count=4, duplicate_threshold=1.01).ranked) == 4


def test_ranking_is_deterministic_and_independent_of_input_order():
    facets = [F("p", "project", "P", unit(1, 0, 0)), F("g", "goal", "G", unit(0, 1, 0)), MUTE]
    rng = random.Random(7)
    stories = [S(f"s{i:02d}", unit(rng.random(), rng.random(), rng.random()), covered=1 + i % 3) for i in range(25)]
    stories += [S("tie1", unit(1, 1, 1)), S("tie2", unit(1, 1, 1))]
    first = [(r.story.id, r.score) for r in rank(stories, facets, mmr_lambda=0.7, final_count=10, wildcard=True).ranked]
    for seed in range(5):
        shuffled = stories[:]
        random.Random(seed).shuffle(shuffled)
        assert [(r.story.id, r.score) for r in rank(shuffled, facets, mmr_lambda=0.7, final_count=10, wildcard=True).ranked] == first


# --- novelty, familiarity, repeats -------------------------------------------------------------------

def test_without_novelty_data_the_score_is_pure_relevance_and_no_novelty_is_claimed():
    [row] = rank([S("a", unit(1, 0))], [F("g", "goal", "G", unit(1, 0))]).ranked
    assert row.novelty_score is None and "novelty" not in row.terms and row.score == row.relevance


def test_known_concepts_lower_novelty_of_similar_stories_only():
    facets = [F("g", "goal", "G", unit(1, 0, 0))]
    known = [("embeddings", unit(1, 0, 0))]
    rows = {r.story.id: r for r in rank([S("familiar", unit(1, 0.01, 0)), S("new", unit(0.2, 0, 1))], facets, known=known).ranked}
    assert rows["familiar"].novelty_score < 0.05 and rows["new"].novelty_score > 0.7
    assert rows["familiar"].relevance > rows["new"].relevance  # relevance itself is untouched


def test_novelty_stays_within_zero_and_one_even_when_a_known_concept_points_the_opposite_way():
    facets = [F("g", "goal", "G", unit(1, 0))]
    rows = rank([S("a", unit(1, 0.1)), S("b", unit(1, 0.5))], facets, known=[("opposite", unit(-1, 0))]).ranked
    assert all(0.0 <= r.novelty_score <= 1.0 for r in rows) and all(r.score <= 1.0 for r in rows)


def test_knew_this_already_feedback_lowers_novelty_not_relevance():
    facets = [F("g", "goal", "G", unit(1, 0))]
    fam = {"tooling": {"w": 0.6, "updatedAt": AT}}
    plain = rank([S("a", unit(1, 0.1))], facets).ranked[0]
    known = rank([S("a", unit(1, 0.1))], facets, fam=fam).ranked[0]
    assert plain.novelty_score is None and known.novelty_score == pytest.approx(0.4)
    assert known.relevance == plain.relevance and known.semantic_score == plain.semantic_score


def test_novelty_cannot_outweigh_strong_relevance_even_at_the_largest_allowed_weight():
    facets = [F("g", "goal", "G", unit(1, 0, 0))]
    known = [("x", unit(1, 0, 0))]
    stories = [S("relevant_but_known", unit(1, 0.02, 0)), S("novel_but_unrelated", unit(0.1, 0, 1))]
    result = rank(stories, facets, known=known, novelty_weight=0.3)
    assert ids(result)[0] == "relevant_but_known"


def test_the_settings_cap_the_novelty_weight():
    from app.config import Settings

    assert RecConfig.from_settings(Settings(rec_novelty_weight=0.9, _env_file=None)).novelty_weight == 0.3
    assert RecConfig.from_settings(Settings(rec_novelty_weight=-1, _env_file=None)).novelty_weight == 0.0


def test_stories_heard_in_recent_briefings_are_penalised():
    facets = [F("g", "goal", "G", unit(1, 0))]
    stories = [S("heard", unit(1, 0.01), cluster="cl_old"), S("fresh", unit(1, 0.1)), S("floor", unit(0, 1))]
    assert ids(rank(stories, facets, heard={"cl_old"}))[:2] == ["fresh", "heard"]
    assert ids(rank(stories, facets))[:2] == ["heard", "fresh"]
    [heard] = [r for r in rank(stories, facets, heard={"cl_old"}).ranked if r.story.id == "heard"]
    assert heard.terms["already_heard"] == 1.0 and heard.score == pytest.approx(1.0 - RecConfig().heard_penalty)


# --- wildcard -------------------------------------------------------------------------------------------

def test_wildcard_comes_from_outside_the_picks_when_there_are_more_candidates_than_slots():
    facets = [F("g", "goal", "G", unit(1, 0, 0))]
    stories = [S(f"s{i}", unit(1, 0.05 * i, 0)) for i in range(6)] + [S("far", unit(0.6, 0, 1), covered=3)]
    result = rank(stories, facets, final_count=4, wildcard=True, duplicate_threshold=1.01, wildcard_min_relevance=0.0)
    wild = [r for r in result.ranked if r.wildcard]
    assert len(result.ranked) == 5 and len(wild) == 1 and wild[0].story.id == "far" and wild[0].reason == WILDCARD_REASON
    assert result.ranked[-1] is wild[0]


def test_wildcard_flags_a_weaker_pick_when_everything_already_fits():
    facets = [F("g", "goal", "G", unit(1, 0, 0))]
    stories = [S(f"s{i}", unit(1, 0.1 * i, 0)) for i in range(5)] + [S("odd", unit(0.7, 0, 1))]
    result = rank(stories, facets, final_count=12, wildcard=True, duplicate_threshold=1.01, wildcard_min_relevance=0.0)
    wild = [r for r in result.ranked if r.wildcard]
    assert len(result.ranked) == 6 and len(wild) == 1 and result.ranked.index(wild[0]) >= 3


def test_no_wildcard_when_disabled_when_too_few_stories_or_when_nothing_clears_the_floor():
    facets = [F("g", "goal", "G", unit(1, 0))]
    stories = [S(f"s{i}", unit(1, 0.1 * i)) for i in range(6)]
    assert not any(r.wildcard for r in rank(stories, facets, wildcard=False).ranked)
    assert not any(r.wildcard for r in rank(stories[:3], facets, wildcard=True, wildcard_min_relevance=0.0).ranked)
    assert not any(r.wildcard for r in rank(stories, facets, wildcard=True, wildcard_min_relevance=2.0, final_count=3).ranked)
    assert not any(r.wildcard for r in rank(stories, facets, wildcard=True, wildcard_enabled=False).ranked)


def test_a_muted_or_recently_heard_story_is_never_the_wildcard():
    facets = [F("g", "goal", "G", unit(1, 0, 0)), MUTE]
    stories = [S(f"s{i}", unit(1, 0.05 * i, 0)) for i in range(4)] + [
        S("pen", unit(0.8, 0, 0.7), blurb="cryptocurrency news"), S("heard", unit(0.9, 0, 1), cluster="cl_h")]
    result = rank(stories, facets, final_count=4, wildcard=True, heard={"cl_h"}, wildcard_min_relevance=0.0, duplicate_threshold=1.01)
    assert not any(r.wildcard and r.story.id in ("pen", "heard") for r in result.ranked)


# --- unusable input -----------------------------------------------------------------------------------

def test_semantic_ranking_declines_without_embedded_facets_or_story_vectors():
    stories = [S("a", unit(1, 0))]
    assert rank(stories, []) is None
    assert rank(stories, [F("g", "goal", "G", None)]) is None
    assert rank(stories, [MUTE]) is None  # only a mute: nothing positive to rank by
    assert rank([S("a", None), S("b", [0.0] * DIM)], [F("g", "goal", "G", unit(1, 0))]) is None
    assert rank([], [F("g", "goal", "G", unit(1, 0))]) is None


def test_stories_with_missing_zero_or_wrong_size_vectors_are_kept_but_ranked_last_without_a_semantic_score():
    facets = [F("g", "goal", "G", unit(1, 0))]
    stories = [S("good", unit(1, 0.1)), S("none", None), S("zero", [0.0] * DIM), S("short", [1.0, 0.0, 0.0]), S("nan", [float("nan")] * DIM)]
    result = rank(stories, facets, duplicate_threshold=1.01)
    assert ids(result)[0] == "good" and set(ids(result)) == {"good", "none", "zero", "short", "nan"}
    for r in result.ranked[1:]:
        assert r.semantic_score is None and r.facet_id is None and r.reason == NEUTRAL_REASON
    assert result.without_vector == 4


def test_a_facet_with_an_incompatible_dimension_is_ignored():
    facets = [F("g", "goal", "G", unit(1, 0)), F("bad", "goal", "Bad", [1.0, 0.0, 0.0])]
    [row] = rank([S("a", unit(1, 0.2))], facets).ranked
    assert row.facet_id == "g"


# --- no language model in the recommender -------------------------------------------------------------------

def test_the_recommender_makes_no_language_model_calls():
    root = Path(__file__).resolve().parents[2] / "app" / "recommend"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for banned in ("svc.llm", ".generate_json", ".stream_text", "openai", "genai", "rerank_score"):
            assert banned not in text, f"{path.name} mentions {banned}"
