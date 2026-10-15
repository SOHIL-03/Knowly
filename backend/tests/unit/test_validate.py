"""Grounding gates: plan repair (9.2), script validator (B8) and citation checking (9.4)."""

from app.ai.schemas import PlannedSegment, PodcastScript, ScriptLine, SegmentPlan
from app.pipeline.validate import extract_citations, render_script_text, repair_plan, script_problems


def seg(type_, ids, secs, title="t"):
    return PlannedSegment(type=type_, title=title, why_chosen="w", item_ids=ids, target_seconds=secs)


RANKED = ["it_1", "it_2", "it_3", "it_4", "it_5", "it_6"]


def test_planner_cannot_introduce_unknown_or_duplicate_stories():
    plan = SegmentPlan(segments=[seg("project", ["it_1", "it_999"], 240), seg("quickhits", ["it_1", "it_2"], 240)])
    check = repair_plan(plan, RANKED, 600, None, set())
    ids = [i for s in check.plan.segments for i in s.item_ids]
    assert ids == ["it_1", "it_2"]
    assert any("unknown item id it_999" in p for p in check.problems)
    assert any("duplicate" in p for p in check.problems)
    assert set(check.plan.also_in_inbox) == {"it_3", "it_4", "it_5", "it_6"}


def test_segments_follow_fixed_skeleton_and_wildcard_goes_to_quickhits():
    plan = SegmentPlan(segments=[seg("quickhits", ["it_3"], 200), seg("learning", ["it_2"], 150), seg("project", ["it_1"], 250)])
    check = repair_plan(plan, RANKED, 600, "it_6", set())
    assert [s.type for s in check.plan.segments] == ["project", "learning", "quickhits"]
    assert "it_6" in check.plan.segments[2].item_ids
    assert check.plan.wildcard_item_id == "it_6"


def test_overlong_plan_drops_lowest_ranked_to_also_in_inbox():
    plan = SegmentPlan(segments=[seg("project", ["it_1", "it_2"], 400), seg("quickhits", ["it_3", "it_4", "it_5"], 500)])
    check = repair_plan(plan, RANKED, 600, None, set())
    total = sum(s.target_seconds for s in check.plan.segments)
    assert total <= 660
    assert "it_5" in check.plan.also_in_inbox  # lowest ranked goes first
    assert "it_1" in check.plan.segments[0].item_ids


def test_review_segment_only_uses_review_items():
    plan = SegmentPlan(segments=[seg("project", ["it_1"], 300), seg("review", ["it_2", "it_r"], 45)])
    check = repair_plan(plan, RANKED, 360, None, {"it_r"})
    review = [s for s in check.plan.segments if s.type == "review"][0]
    assert review.item_ids == ["it_r"]


def line(text, ids=(), speaker="A"):
    return ScriptLine(speaker=speaker, text=text, item_ids=list(ids))


def script(lines, takeaways=("a",)):
    return PodcastScript(title="T", opening_question="Q?", lines=lines, takeaways=list(takeaways))


WORDS = " ".join(["word"] * 120)


def test_script_validator_accepts_grounded_script():
    s = script([line("What changed?"), line(WORDS, ["it_1"], "B"), line(WORDS, ["it_2"])])
    assert script_problems(s, {"it_1", "it_2"}, 120, single_speaker=False) == []


def test_script_validator_rejects_foreign_ids_uncited_content_and_missing_items():
    s = script([line(WORDS), line(WORDS, ["it_x"], "B")])
    problems = script_problems(s, {"it_1"}, 120, single_speaker=False)
    assert any("outside this segment" in p for p in problems)
    assert any("cites no item id" in p for p in problems)
    assert any("not covered" in p for p in problems)


def test_script_validator_limits_uncited_lines():
    s = script([line("Hi."), line("Right."), line("So."), line(WORDS, ["it_1"])])
    assert any("cite no source" in p for p in script_problems(s, {"it_1"}, 120, single_speaker=False))


def test_single_narrator_must_use_one_speaker():
    s = script([line("What changed?"), line(WORDS, ["it_1"], "B")])
    assert any("single-narrator" in p for p in script_problems(s, {"it_1"}, 120, single_speaker=True))


def test_script_validator_rejects_wrong_size():
    tiny = script([line("too short", ["it_1"]), line("tiny", ["it_1"])])
    huge = script([line(" ".join(["w"] * 300), ["it_1"]) for _ in range(5)])
    assert any("words" in p for p in script_problems(tiny, {"it_1"}, 180, single_speaker=False))
    assert any("words" in p for p in script_problems(huge, {"it_1"}, 60, single_speaker=False))


def test_render_script_names_speakers_and_sources():
    s = script([line("Hello."), line("Facts.", ["it_1"], "B")])
    text = render_script_text(s, {"it_1": {"senderName": "Cloud Weekly"}}, {"A": "Maya", "B": "Theo"})
    assert "Theo: Facts.  [source: Cloud Weekly]" in text


def test_extract_citations_strips_invented_ids():
    text, valid, invalid = extract_citations("Fact one [it_1]. Fact two [it_fake].", {"it_1"})
    assert valid == ["it_1"] and invalid == ["it_fake"]
    assert text == "Fact one [it_1]. Fact two."
