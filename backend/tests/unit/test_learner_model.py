"""Mastery and review rules: Architecture v3 sections 7.2-7.4."""

from datetime import UTC, datetime, timedelta

import pytest

from app.learner import model as lm

AT = datetime(2026, 10, 15, 9, tzinfo=UTC)


@pytest.mark.parametrize("rating,m", [("new", 0.10), ("heard", 0.40), ("use", 0.75)])
def test_self_rating_seed(rating, m):
    assert lm.mastery_from_self_rating(rating) == m


def test_quiz_updates_exact():
    assert lm.mastery_after_quiz(0.4, "correct") == pytest.approx(0.4 + 0.25 * 0.6)
    assert lm.mastery_after_quiz(0.4, "partly") == pytest.approx(0.4 + 0.10 * 0.6)
    assert lm.mastery_after_quiz(0.4, "not_yet") == pytest.approx(0.28)
    assert lm.mastery_after_quiz(1.0, "correct") == 1.0


def test_simpler_caps_mastery():
    assert lm.mastery_after_simpler(0.8) == 0.35
    assert lm.mastery_after_simpler(0.2) == 0.2


def test_review_ladder_1_3_7_days():
    step, due = lm.next_review(-1, "correct", AT)
    assert (step, due) == (0, AT + timedelta(days=1))
    step, due = lm.next_review(step, "correct", AT)
    assert (step, due) == (1, AT + timedelta(days=3))
    step, due = lm.next_review(step, "correct", AT)
    assert (step, due) == (2, AT + timedelta(days=7))
    step, due = lm.next_review(step, "correct", AT)
    assert (step, due) == (2, AT + timedelta(days=7))


@pytest.mark.parametrize("grade", ["partly", "not_yet"])
def test_missed_reviews_again_in_one_day(grade):
    assert lm.next_review(2, grade, AT) == (0, AT + timedelta(days=1))


@pytest.mark.parametrize("m,band", [(0.1, "define"), (0.34, "define"), (0.35, "remind"), (0.7, "remind"), (0.71, "skip")])
def test_explanation_bands(m, band):
    assert lm.explanation_band(m) == band


def test_topic_weight_half_life_30_days():
    assert lm.decayed(0.8, AT - timedelta(days=30), AT) == pytest.approx(0.4)
    entry = lm.apply_topic_delta({"w": 0.8, "updatedAt": AT - timedelta(days=30)}, 0.1, AT)
    assert entry["w"] == pytest.approx(0.5) and entry["updatedAt"] == AT


def test_topic_weight_clamped():
    e = None
    for _ in range(10):
        e = lm.apply_topic_delta(e, -0.3, AT)
    assert e["w"] == -1.0


def test_effective_topic_weight_averages_matching_topics():
    w = {"vector_search": {"w": 0.4, "updatedAt": AT}, "rag": {"w": -0.2, "updatedAt": AT}}
    assert lm.effective_topic_weight(w, ["Vector search", "RAG", "unknown"], AT) == pytest.approx(0.1)
    assert lm.effective_topic_weight(w, ["none"], AT) == 0.0
