"""Deterministic feedback learning: decay, bounds, familiarity and idempotency keys (app/recommend/feedback.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.recommend.facets import Facet
from app.recommend.feedback import (
    FEEDBACK_ACTIONS,
    decay,
    effective_weight,
    familiarity_for,
    idempotency_key,
    learned_delta,
    matched_facet,
    step_for,
    story_topics,
    update_facet_pref,
    update_familiarity,
)
from app.recommend.settings import RecConfig

AT = datetime(2026, 10, 15, tzinfo=UTC)
CFG = RecConfig()


def facet(weight=1.0):
    return Facet("f1", "LLM evals", "goal", weight, [1.0, 0.0], AT, AT)


def test_decay_halves_every_half_life_and_never_grows():
    assert decay(0.4, AT - timedelta(days=30), AT, 30) == pytest.approx(0.2)
    assert decay(0.4, AT - timedelta(days=60), AT, 30) == pytest.approx(0.1)
    assert decay(0.4, AT, AT, 30) == pytest.approx(0.4)
    assert decay(0.4, None, AT, 30) == 0.4
    assert decay(0.4, AT + timedelta(days=5), AT, 30) == 0.4  # a clock skewed into the future does not amplify
    assert decay(-0.3, AT - timedelta(days=30), AT, 30) == pytest.approx(-0.15)


def test_the_half_life_is_configurable():
    fast = RecConfig(feedback_half_life_days=7)
    prefs = {"f1": {"delta": 0.4, "updatedAt": AT - timedelta(days=7)}}
    assert learned_delta(prefs, "f1", AT, fast) == pytest.approx(0.2)
    assert learned_delta(prefs, "f1", AT, CFG) == pytest.approx(0.4 * 0.5 ** (7 / 30))


def test_each_action_moves_the_learned_weight_by_its_configured_step():
    assert [step_for(a, CFG) for a in ("save", "ask", "deeper", "lessLikeThis", "knewThis")] == [0.10, 0.05, 0.08, -0.15, 0.0]
    prefs = update_facet_pref({}, "f1", step_for("save", CFG), AT, CFG)
    prefs = update_facet_pref(prefs, "f1", step_for("ask", CFG), AT, CFG)
    assert prefs["f1"]["delta"] == pytest.approx(0.15) and prefs["f1"]["updatedAt"] == AT
    custom = RecConfig(feedback_steps={"save": 0.3, "ask": 0.0, "deeper": 0.0, "lessLikeThis": -0.4})
    assert step_for("save", custom) == 0.3 and step_for("lessLikeThis", custom) == -0.4


def test_the_learned_delta_is_bounded_in_both_directions():
    prefs: dict = {}
    for _ in range(50):
        prefs = update_facet_pref(prefs, "f1", 0.1, AT, CFG)
    assert prefs["f1"]["delta"] == CFG.feedback_max_delta
    for _ in range(100):
        prefs = update_facet_pref(prefs, "f1", -0.15, AT, CFG)
    assert prefs["f1"]["delta"] == -CFG.feedback_max_delta


def test_old_feedback_decays_before_new_feedback_is_added():
    prefs = {"f1": {"delta": 0.4, "updatedAt": AT - timedelta(days=30)}}
    assert update_facet_pref(prefs, "f1", 0.1, AT, CFG)["f1"]["delta"] == pytest.approx(0.3)
    assert prefs["f1"]["delta"] == 0.4  # the input is not mutated


def test_effective_weight_is_declared_plus_learned_and_stays_within_the_bounds():
    f = facet(1.0)
    assert effective_weight(f, {}, AT, CFG) == 1.0
    assert effective_weight(f, {"f1": {"delta": 0.3, "updatedAt": AT}}, AT, CFG) == pytest.approx(1.3)
    assert effective_weight(f, {"f1": {"delta": 0.5, "updatedAt": AT}}, AT, CFG) == CFG.weight_max  # 1.5 is the cap
    assert effective_weight(facet(0.2), {"f1": {"delta": -0.5, "updatedAt": AT}}, AT, CFG) == CFG.weight_min
    assert f.weight == 1.0  # declared weight is never rewritten


def test_a_month_later_the_learned_part_has_halved():
    prefs = {"f1": {"delta": 0.4, "updatedAt": AT - timedelta(days=30)}}
    assert effective_weight(facet(1.0), prefs, AT, CFG) == pytest.approx(1.2)


@pytest.mark.parametrize("entry", [None, "x", {"delta": "abc"}, {"delta": float("nan")}, {"delta": None}, {}])
def test_malformed_stored_preferences_count_as_no_feedback(entry):
    assert learned_delta({"f1": entry}, "f1", AT, CFG) == 0.0


def test_knew_this_raises_familiarity_with_a_cap_and_it_decays():
    fam = update_familiarity({}, ["tooling", "pgvector"], AT, CFG)
    assert fam["tooling"]["w"] == CFG.knew_step and fam["pgvector"]["w"] == CFG.knew_step
    again = update_familiarity(fam, ["tooling"], AT, CFG)
    assert again["tooling"]["w"] == pytest.approx(2 * CFG.knew_step) and again["pgvector"] == fam["pgvector"]
    capped = fam
    for _ in range(10):
        capped = update_familiarity(capped, ["tooling"], AT, CFG)
    assert capped["tooling"]["w"] == CFG.familiarity_cap
    old = {"tooling": {"w": 0.4, "updatedAt": AT - timedelta(days=30)}}
    assert familiarity_for(["tooling"], old, AT, CFG) == pytest.approx(0.2)


def test_familiarity_is_the_highest_among_the_story_topics_and_zero_when_unknown():
    fam = {"tooling": {"w": 0.2, "updatedAt": AT}, "pgvector": {"w": 0.5, "updatedAt": AT}}
    assert familiarity_for(["Tooling", "pgvector", "other"], fam, AT, CFG) == 0.5
    assert familiarity_for(["other"], fam, AT, CFG) == 0.0 and familiarity_for([], fam, AT, CFG) == 0.0


def test_idempotency_key_is_one_per_briefing_story_and_action():
    k = idempotency_key("2026-10-15", "it_1", "save")
    assert k == idempotency_key("2026-10-15", "it_1", "save") and k.startswith("fb_")
    assert len({k, idempotency_key("2026-10-16", "it_1", "save"), idempotency_key("2026-10-15", "it_2", "save"),
                idempotency_key("2026-10-15", "it_1", "ask"), idempotency_key(None, "it_1", "save")}) == 5


def test_the_matched_facet_comes_from_the_briefings_ranked_list():
    briefing = {"ranked": [{"itemId": "a", "facetId": "f1"}, {"itemId": "b", "facetId": None}, "junk", {"itemId": "c"}]}
    assert matched_facet(briefing, "a") == "f1"
    assert matched_facet(briefing, "b") is None and matched_facet(briefing, "c") is None and matched_facet(briefing, "zzz") is None
    assert matched_facet({}, "a") is None and matched_facet({"ranked": None}, "a") is None


def test_story_topics_are_the_category_then_entities():
    assert story_topics({"category": "tooling", "entities": ["pgvector", "Gemini"]}) == ["tooling", "pgvector", "Gemini"]
    assert story_topics({}) == [] and story_topics({"category": None, "entities": [None, 3, "ok"]}) == ["ok"]


def test_the_five_feedback_actions_are_the_ones_the_app_records():
    assert set(FEEDBACK_ACTIONS) == {"save", "ask", "deeper", "lessLikeThis", "knewThis"}
