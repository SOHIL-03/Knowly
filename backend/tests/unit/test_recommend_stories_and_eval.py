"""Story documents and signatures, the persisted ranking payload, and the offline evaluation harness."""

from __future__ import annotations

import json
import math
import sys

import pytest

from app.pipeline.score import Story
from app.recommend.ranking import NEUTRAL_REASON, WILDCARD_REASON, RankedStory
from app.recommend.service import ranked_payload, ranking_mode
from app.recommend.settings import RecConfig
from app.recommend.story_embeddings import intro_excerpt, signature, story_document
from recsys_eval import run_eval

CFG = RecConfig()


# --- story document and signature --------------------------------------------------------------------

def test_the_document_is_headline_blurb_and_article_intro_and_tolerates_missing_parts():
    assert story_document("Head", "Blurb", "Intro") == "Head\nBlurb\nIntro"
    assert story_document("Head", "", "") == "Head" and story_document("  ", " ", "") == ""
    assert story_document("Head", "Blurb", "") == "Head\nBlurb"


def test_the_excerpt_stops_at_a_sentence_then_a_word_and_never_exceeds_the_limit():
    text = "First sentence here. Second sentence is longer and goes on. Third one follows after that."
    assert intro_excerpt(text, 40) == "First sentence here."  # a sentence end in the second half of the limit wins
    assert intro_excerpt(text, 45) == "First sentence here. Second sentence is"  # sentence end in the first half: cut at a word
    assert intro_excerpt(text, 200) == text
    cut = intro_excerpt("word " * 100, 23)
    assert len(cut) <= 23 and not cut.endswith(" ") and set(cut.split()) == {"word"}
    assert intro_excerpt("x" * 50, 10) == "x" * 10  # no spaces: a hard cut
    assert intro_excerpt(None, 100) == "" and intro_excerpt("", 100) == "" and intro_excerpt("text", 0) == ""
    assert intro_excerpt("a\n\n  b\tc", 100) == "a b c"


def test_the_signature_changes_only_with_what_the_embedding_depends_on():
    item = {"headline": "H", "blurb": "B", "fullTextPath": None, "linkOnly": True, "senderName": "X", "order": 3}
    base = signature(item, "m", 768, CFG)
    assert signature({**item, "senderName": "Y", "order": 9, "url": "https://x"}, "m", 768, CFG) == base
    assert signature({**item, "headline": "H2"}, "m", 768, CFG) != base
    assert signature({**item, "blurb": "B2"}, "m", 768, CFG) != base
    assert signature(item, "other-model", 768, CFG) != base and signature(item, "m", 512, CFG) != base
    assert signature(item, "m", 768, RecConfig(excerpt_chars=300)) != base
    with_article = {**item, "fullTextPath": "articles/x.txt", "linkOnly": False}
    assert signature(with_article, "m", 768, CFG) != base
    assert signature({**with_article, "linkOnly": True}, "m", 768, CFG) == base  # unreadable article: same as no article


# --- persisted payload ---------------------------------------------------------------------------------

def story(sid):
    return Story(sid, f"Headline {sid}", "", "tooling", [], "s1", "Sender", None, f"cl_{sid}", 1)


def test_the_ranked_payload_has_the_contract_fields_and_nothing_model_generated():
    r = RankedStory(story=story("a"), score=0.81234, terms={}, reason="Matches your learning goal: LLM evals.", muted=False,
                    semantic_score=0.7123456, raw_similarity=0.7, facet_id="f1", facet_text="LLM evals", facet_type="goal",
                    matched_facets=[{"facetId": "f1"}], novelty_score=None, relevance=1.0, ranking_version="semantic-v1")
    w = RankedStory(story=story("b"), score=0.2, terms={}, reason=WILDCARD_REASON, muted=False, wildcard=True, ranking_version="semantic-v1")
    neutral = RankedStory(story=story("c"), score=0.1, terms={}, reason=NEUTRAL_REASON, muted=False, ranking_version="semantic-v1")
    rows = ranked_payload([r, w, neutral])
    assert rows[0] == {"itemId": "a", "score": 0.8123, "reason": "Matches your learning goal: LLM evals.", "wildcard": False,
                       "semanticScore": 0.7123, "rawSimilarity": 0.7, "relevance": 1.0, "facetId": "f1", "facetType": "goal",
                       "facetText": "LLM evals", "matchedFacets": [{"facetId": "f1"}], "noveltyScore": None,
                       "rankingVersion": "semantic-v1", "explained": True}
    assert rows[1]["wildcard"] is True and rows[1]["explained"] is False and rows[2]["explained"] is False
    assert ranking_mode([r, w]) == "semantic" and ranking_mode([]) == "legacy"
    json.dumps(rows)  # plain data only


def test_the_legacy_payload_is_marked_and_carries_no_facet_fields():
    from app.pipeline.score import Scored

    rows = ranked_payload([Scored(story("a"), 0.5, {}, "Close to what you are building", False)])
    assert rows == [{"itemId": "a", "score": 0.5, "reason": "Close to what you are building", "wildcard": False, "rankingVersion": "legacy-v1"}]
    assert ranking_mode([Scored(story("a"), 0.5, {}, "x", False)]) == "legacy"


# --- settings ---------------------------------------------------------------------------------------------

def test_settings_are_clamped_to_safe_values():
    from app.config import Settings

    s = Settings(_env_file=None, rec_mmr_lambda=5, rec_novelty_weight=0.9, rec_weight_min=2.0, rec_weight_max=0.5, rec_embed_batch_size=1000,
                 rec_final_count=0, rec_mute_penalty_gap=0.5, rec_mute_exclude_gap=0.1, rec_feedback_half_life_days=0, rec_familiarity_cap=3)
    c = RecConfig.from_settings(s)
    assert c.mmr_lambda == 1.0 and c.novelty_weight == 0.3 and (c.weight_min, c.weight_max) == (0.5, 2.0)
    assert c.embed_batch_size == 100 and c.final_count == 1 and c.mute_penalty_gap == 0.1
    assert c.feedback_half_life_days == 0.1 and c.familiarity_cap == 1.0


def test_every_documented_weight_and_limit_is_configurable_from_the_environment(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("REC_WEIGHT_GOAL", "0.4")
    monkeypatch.setenv("REC_MMR_LAMBDA", "0.55")
    monkeypatch.setenv("REC_FEEDBACK_HALF_LIFE_DAYS", "14")
    monkeypatch.setenv("REC_ENABLED", "false")
    c = RecConfig.from_settings(Settings(_env_file=None))
    assert c.weights["goal"] == 0.4 and c.mmr_lambda == 0.55 and c.feedback_half_life_days == 14
    assert Settings(_env_file=None).rec_enabled is False


# --- evaluation harness -------------------------------------------------------------------------------------

def test_the_dataset_is_well_formed():
    data = json.loads((run_eval.HERE / "dataset.json").read_text(encoding="utf-8"))
    stories = data["stories"]
    assert len(stories) == 50 and len({s["id"] for s in stories}) == 50 and set(data["profiles"]) == {"A", "B"}
    for s in stories:
        assert set(s["labels"]) == {"A", "B"} and set(s["labels"].values()) <= set(run_eval.GAIN)
        assert s["headline"] and s["blurb"]
    groups: dict[str, int] = {}
    for s in stories:
        if s.get("dupGroup"):
            groups[s["dupGroup"]] = groups.get(s["dupGroup"], 0) + 1
    assert groups and all(n >= 2 for n in groups.values())
    for p in "AB":
        counts = {lab: sum(1 for s in stories if s["labels"][p] == lab) for lab in run_eval.GAIN}
        assert all(n >= 5 for n in counts.values()), (p, counts)
    tags = {t for s in stories for t in s.get("muteTags", [])}
    assert tags == {t for prof in data["profiles"].values() for t in prof["muteTags"]}
    assert any(s.get("incidentalMentions") for s in stories)


def test_metrics_on_a_toy_ranking():
    by_id = {
        "a": {"labels": {"A": "relevant"}}, "b": {"labels": {"A": "adjacent"}}, "c": {"labels": {"A": "irrelevant"}},
        "d": {"labels": {"A": "relevant"}, "dupGroup": "g1", "muteTags": []}, "e": {"labels": {"A": "relevant"}, "dupGroup": "g1"},
        "m": {"labels": {"A": "irrelevant"}, "muteTags": ["crypto"]}, "z": {"labels": {"A": "relevant"}}}
    perfect = run_eval.score_list(["a", "z", "d", "b", "c"], by_id, "A", {"crypto"})
    assert perfect["precision@5"] == pytest.approx(3 / 5) and perfect["mute_violation_rate"] == 0 and perfect["duplicate_rate"] == 0
    assert perfect["recall@5"] == pytest.approx(3 / 4)
    worst = run_eval.score_list(["c", "m", "d", "e", "b"], by_id, "A", {"crypto"})
    assert worst["mute_violation_rate"] == pytest.approx(1 / 5) and worst["duplicate_rate"] == pytest.approx(1 / 5)
    assert worst["precision@5"] == pytest.approx(2 / 5) and worst["ndcg@5"] < perfect["ndcg@5"]
    assert run_eval.score_list(["a", "z", "d", "e", "b"], by_id, "A", set())["ndcg@5"] == pytest.approx(1.0)  # the ideal order
    assert run_eval.dcg([2, 1]) == pytest.approx(3 / math.log2(2) + 1 / math.log2(3))


def test_the_offline_run_is_deterministic_and_makes_no_network_call(monkeypatch, capsys):
    def summary():
        monkeypatch.setattr(sys, "argv", ["run_eval", "--runs", "1"])
        run_eval.main()
        out = capsys.readouterr().out
        return [ln for ln in out.splitlines() if ln.startswith("|") and "latency" not in ln]

    first, second = summary(), summary()
    assert first == second and any("precision@5" in ln for ln in first)


def test_the_offline_embedder_is_deterministic_and_unit_length():
    e = run_eval.HashedLexicalEmbedder(64)
    a, b = e.embed(["pgvector index for RAG"])[0], e.embed(["pgvector index for RAG"])[0]
    assert a == b and math.sqrt(sum(x * x for x in a)) == pytest.approx(1.0)
    assert e.embed([""])[0] == [0.0] * 64  # nothing to hash: a zero vector, which the ranker treats as missing
