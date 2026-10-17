"""The mastery policy and concept normalization: pure functions, no database."""

from __future__ import annotations

import pytest

from app.ai.schemas import PodcastConcepts
from app.learning import concepts, policy
from app.learning.policy import Record, classify, next_status


def feed(evidence: list[str], start: Record | None = None):
    """Applies counting evidence one by one, each from a different question. Returns the status after each step."""
    rec = start or Record()
    out = []
    for i, ev in enumerate(evidence):
        status, _ = next_status(rec, ev, f"a:{i}")  # type: ignore[arg-type]
        recent = [*(rec.recent or []), ev][-policy.RECENT_WINDOW:] if ev in policy.COUNTING else list(rec.recent or [])
        keys = list(rec.demonstrated_keys or []) + ([f"a:{i}"] if ev == "demonstrated" else [])
        rec = Record(status, recent, keys)
        out.append(status)
    return out


# --- what one answer shows ---------------------------------------------------------------------------------------------

ANSWER = "Vector search finds nearest neighbours by similarity"


@pytest.mark.parametrize("score,conf,expected", [
    (1.0, 0.9, "demonstrated"), (0.8, 0.9, "demonstrated"), (0.79, 0.9, "partial"), (0.4, 0.9, "partial"),
    (0.39, 0.9, "incorrect"), (0.0, 0.9, "incorrect"),
    (1.0, 0.59, "insufficient"),  # not sure: a high score without confidence is not evidence
])
def test_free_text_is_classified_by_score_and_confidence(score, conf, expected):
    assert classify(score, conf, ANSWER, "Vector search finds nearest neighbours") == expected


def test_a_demonstrated_score_needs_a_quote_that_is_really_in_the_answer():
    assert classify(1.0, 0.9, ANSWER, "") == "insufficient"
    assert classify(1.0, 0.9, ANSWER, "the model invented this quote") == "insufficient"
    assert classify(1.0, 0.9, ANSWER, "NEAREST   neighbours") == "demonstrated"  # case and spacing do not matter


def test_blank_and_too_short_answers_never_count():
    assert classify(1.0, 1.0, "") == "not_assessed"
    assert classify(1.0, 1.0, "   ") == "not_assessed"
    assert classify(1.0, 1.0, "yes", "yes") == "insufficient"


def test_multiple_choice_is_exact():
    assert classify(1.0, 1.0, "1", objective=True) == "demonstrated"
    assert classify(0.0, 1.0, "0", objective=True) == "incorrect"


# --- concept status transitions ----------------------------------------------------------------------------------------

def test_new_concepts_are_unknown_and_insufficient_or_skipped_evidence_keeps_them_unknown():
    assert Record().status == "unknown"
    assert feed(["insufficient", "not_assessed", "insufficient"]) == ["unknown"] * 3


def test_first_evidence_moves_to_learning_even_when_correct():
    assert feed(["demonstrated"]) == ["learning"]
    assert feed(["incorrect"]) == ["learning"]


def test_understood_needs_two_consecutive_demonstrated_answers_to_different_questions():
    assert feed(["demonstrated", "demonstrated"]) == ["learning", "understood"]
    assert feed(["demonstrated", "partial", "demonstrated"]) == ["learning", "learning", "learning"]
    assert feed(["demonstrated", "incorrect", "demonstrated", "demonstrated"])[-1] == "understood"


def test_the_same_question_answered_twice_is_not_two_questions():
    rec = Record("learning", ["demonstrated"], ["a:q1"])
    assert next_status(rec, "demonstrated", "a:q1")[0] == "learning"
    assert next_status(rec, "demonstrated", "a:q2")[0] == "understood"


def test_one_partial_answer_does_not_invalidate_understanding_but_a_clear_error_asks_for_review():
    understood = Record("understood", ["demonstrated", "demonstrated"], ["a:1", "a:2"])
    assert next_status(understood, "partial", "b:1")[0] == "understood"
    assert next_status(understood, "insufficient", "b:1")[0] == "understood"
    status, reason = next_status(understood, "incorrect", "b:1")
    assert status == "needs_review" and "incorrect" in reason


def test_review_ends_only_after_two_consecutive_demonstrated_answers():
    rec = Record("needs_review", ["demonstrated", "demonstrated", "incorrect"], ["a:1", "a:2"])
    assert next_status(rec, "partial", "c:1")[0] == "needs_review"
    after_one, _ = next_status(rec, "demonstrated", "c:1")
    assert after_one == "needs_review"
    assert next_status(Record("needs_review", ["incorrect", "demonstrated"], ["a:1", "c:1"]), "demonstrated", "c:2")[0] == "understood"


def test_nothing_ever_moves_a_concept_back_to_unknown():
    for start in ("learning", "understood", "needs_review"):
        for ev in ("demonstrated", "partial", "incorrect", "insufficient", "not_assessed"):
            assert next_status(Record(start), ev, "x:1")[0] != "unknown"  # type: ignore[arg-type]


def test_assessment_length_follows_the_content():
    assert policy.questions_for(300, 2) == policy.QUESTIONS_MIN
    assert policy.QUESTIONS_MIN <= policy.questions_for(1500, 4) <= policy.QUESTIONS_MAX
    assert policy.questions_for(20000, 12) == policy.QUESTIONS_MAX


def test_mastery_score_is_informational():
    assert policy.mastery_score(0, 0, 0) is None
    assert policy.mastery_score(2, 2, 0) == 0.75


# --- concept normalization and validation ------------------------------------------------------------------------------

def test_equivalent_names_share_a_key_and_different_meanings_do_not():
    assert concepts.normalize_name("Vector databases") == concepts.normalize_name("the vector database")
    assert concepts.normalize_name("Chunking", "rag") != concepts.normalize_name("Chunking", "memory management")
    assert concepts.normalize_name("Chunking", "rag") != concepts.normalize_name("Chunking")
    assert concepts.normalize_name("!!!") == "concept"


LINES = [{"speaker": "Maya", "text": "What is vector search?"}, {"speaker": "Theo", "text": "It finds nearest vectors."}]


def _extracted(*items):
    return PodcastConcepts.model_validate({"concepts": list(items)})


def concept(name, lines, importance="major", **kw):
    return {"name": name, "description": f"About {name}", "importance": importance, "lines": lines, **kw}


def test_concepts_must_point_at_real_script_lines_and_are_deduplicated():
    out = concepts.build("tech.2026-10-16.x", "x", LINES, _extracted(
        concept("vector search", [0]), concept("Vector Searches", [1], "supporting"),
        concept("invented thing", [9]), concept("embeddings", [1], prerequisites=["vector search", "nope"])))
    names = [c["name"] for c in out]
    assert names == ["vector search", "embeddings"]  # the invented one has no script line behind it
    first = next(c for c in out if c["concept_id"] == "vector_search")
    assert [s["line"] for s in first["evidence_spans"]] == [0, 1] and first["importance"] == "major"
    assert first["podcast_id"] == "tech.2026-10-16.x" and first["source_section_id"] == "x"
    assert next(c for c in out if c["name"] == "embeddings")["prerequisite_concepts"] == ["vector_search"]


def test_each_major_concept_gets_two_questions_before_supporting_ones():
    cs = [{"name": "a", "importance": "major"}, {"name": "b", "importance": "major"}, {"name": "c", "importance": "supporting"}]
    assert policy.question_targets(cs, 4) == ["a", "b", "a", "b"]
    assert policy.question_targets(cs, 6) == ["a", "b", "a", "b", "c", "a"]
    assert policy.question_targets([{"name": "x", "importance": "supporting"}], 3) == ["x", "x", "x"]
    assert policy.question_targets([], 3) == []
