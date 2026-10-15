"""Validators that sit between Gemini and storage (sections 9.2, 9.3, 9.4)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.ai.schemas import PlannedSegment, PodcastScript, SegmentPlan

SEGMENT_ORDER = {"project": 0, "learning": 1, "quickhits": 2, "review": 3}


@dataclass
class PlanCheck:
    plan: SegmentPlan
    problems: list[str]


def repair_plan(
    plan: SegmentPlan,
    ranked_ids: list[str],
    length_seconds: int,
    wildcard_id: str | None,
    review_ids: set[str],
) -> PlanCheck:
    """Enforces: only candidate ids, no duplicates, 3-5 segments where possible, fixed skeleton
    order, wildcard in quickhits, total length within 10% (lowest-ranked stories go to Also in your
    inbox when the plan is too long)."""
    problems: list[str] = []
    allowed = set(ranked_ids) | review_ids
    rank_pos = {iid: i for i, iid in enumerate(ranked_ids)}
    used: set[str] = set()
    segments: list[PlannedSegment] = []
    for seg in plan.segments:
        ids = []
        for iid in seg.item_ids:
            if iid not in allowed:
                problems.append(f"unknown item id {iid}")
                continue
            if iid in used:
                problems.append(f"duplicate item id {iid}")
                continue
            if seg.type == "review" and iid not in review_ids:
                problems.append(f"review segment uses non-review item {iid}")
                continue
            ids.append(iid)
            used.add(iid)
        if ids:
            segments.append(seg.model_copy(update={"item_ids": ids}))
        else:
            problems.append(f"segment '{seg.title}' had no valid items")

    if wildcard_id and wildcard_id in allowed and wildcard_id not in used:
        quick = next((s for s in segments if s.type == "quickhits"), None)
        if quick:
            quick.item_ids.append(wildcard_id)
            used.add(wildcard_id)
        else:
            problems.append("wildcard not placed")

    segments.sort(key=lambda s: SEGMENT_ORDER.get(s.type, 9))
    segments = segments[:5]

    # Length: drop lowest-ranked news items until within +10%.
    def total() -> int:
        return sum(s.target_seconds for s in segments)

    overflow: list[str] = [i for i in plan.also_in_inbox if i in rank_pos and i not in used]
    while total() > length_seconds * 1.10:
        candidates = [
            (rank_pos.get(i, -1), s, i) for s in segments if s.type != "review" for i in s.item_ids if len(s.item_ids) > 1
        ]
        if not candidates:
            # Shrink the longest segment's time budget instead.
            longest = max(segments, key=lambda s: s.target_seconds)
            longest.target_seconds = max(20, int(longest.target_seconds - (total() - length_seconds)))
            break
        _, seg, iid = max(candidates, key=lambda c: c[0])
        per_item = max(15, seg.target_seconds // max(1, len(seg.item_ids)))
        seg.item_ids.remove(iid)
        seg.target_seconds = max(20, seg.target_seconds - per_item)
        used.discard(iid)
        overflow.append(iid)
    if total() < length_seconds * 0.9 and segments:
        problems.append("plan shorter than requested length")

    for iid in ranked_ids:
        if iid not in used and iid not in overflow:
            overflow.append(iid)
    final = SegmentPlan(
        segments=segments or plan.segments[:1],
        wildcard_item_id=wildcard_id if wildcard_id in used else None,
        also_in_inbox=overflow,
    )
    return PlanCheck(final, problems)


MAX_TRANSITION_WORDS = 25
MAX_TRANSITION_SHARE = 0.4


def script_problems(script: PodcastScript, allowed_ids: set[str], target_seconds: int, single_speaker: bool) -> list[str]:
    """Grounding gate for a podcast script, applied before any audio is made:
    - every cited id belongs to this segment;
    - lines without ids are short transitions or questions, and not most of the script;
    - every item of the segment is covered by at least one line;
    - single-narrator scripts use one speaker;
    - total length is not far from the target."""
    problems: list[str] = []
    uncited = 0
    covered: set[str] = set()
    for i, line in enumerate(script.lines):
        bad = [x for x in line.item_ids if x not in allowed_ids]
        if bad:
            problems.append(f"line {i} cites ids outside this segment ({bad})")
        covered.update(x for x in line.item_ids if x in allowed_ids)
        if not line.item_ids:
            uncited += 1
            if len(line.text.split()) > MAX_TRANSITION_WORDS:
                problems.append(f"line {i} states content but cites no item id")
        if single_speaker and line.speaker != "A":
            problems.append(f"line {i} uses speaker {line.speaker} in a single-narrator script")
    if uncited > max(1, int(len(script.lines) * MAX_TRANSITION_SHARE)):
        problems.append(f"{uncited} of {len(script.lines)} lines cite no source")
    missing = allowed_ids - covered
    if missing:
        problems.append(f"items not covered: {sorted(missing)}")
    words = sum(len(line.text.split()) for line in script.lines)
    target_words = target_seconds * 2.5
    if words > target_words * 1.8 + 60 or words < min(40, target_words * 0.35):
        problems.append(f"script is {words} words; target is about {int(target_words)}")
    return problems


def render_script_text(script: PodcastScript, item_meta: dict[str, dict], hosts: dict[str, str]) -> str:
    """Plain-text script with speaker names and sources, for the faithfulness check and export."""
    out = [script.title, ""]
    for line in script.lines:
        src = ", ".join(item_meta[i].get("senderName", "") for i in line.item_ids if i in item_meta)
        out.append(f"{hosts.get(line.speaker, line.speaker)}: {line.text}" + (f"  [source: {src}]" if src else ""))
    out += ["", "Takeaways:", *(f"- {t}" for t in script.takeaways)]
    return "\n".join(out).strip() + "\n"


CITATION_RE = re.compile(r"\[(it_[A-Za-z0-9_]+)\]")


def extract_citations(text: str, allowed_ids: set[str]) -> tuple[str, list[str], list[str]]:
    """Returns (text with invalid markers removed, valid ids in order, invalid ids)."""
    valid: list[str] = []
    invalid: list[str] = []

    def repl(m: re.Match[str]) -> str:
        iid = m.group(1)
        if iid in allowed_ids:
            if iid not in valid:
                valid.append(iid)
            return m.group(0)
        invalid.append(iid)
        return ""

    cleaned = CITATION_RE.sub(repl, text)
    return re.sub(r"\s+([.,;])", r"\1", cleaned).strip(), valid, invalid
