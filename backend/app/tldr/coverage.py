"""Coverage-based script generation for a TLDR podcast (one section = several articles, or a single article).

articles -> coverage manifest (every point and required fact, with stable ids k1, k2, ...)
        -> outline (points grouped into parts; one part when the content is small)
        -> script (one LLM call per part, each with its own output-token budget)
        -> validation (deterministic ids and figures + an LLM semantic audit)
        -> bounded repair (only the parts with gaps are regenerated)

The episode length follows from the content: nothing here forces a duration or word count. Word
estimates only size the output-token budget of each call. A result is "complete" only when validation
finds no missing points, no missing facts and no unsupported lines; otherwise it is "partial" and says
exactly what is missing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai import prompts
from app.ai.client import AiOutputTruncated
from app.ai.schemas import (
    CoverageManifest,
    CoverageReport,
    ScriptLine,
    ScriptPart,
)
from app.logs import Timer, get_logger, log

logger = get_logger(__name__)

MAX_PART_POINTS = 5  # points per generation call
MAX_PART_WORDS = 700  # estimated spoken words per generation call
MANIFEST_CHUNK_CHARS = 15_000  # articles longer than this are read in paragraph-aligned chunks
MIN_TOKENS, MAX_TOKENS = 1_000, 12_000
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class Stats:
    """Per-request counters, reported in logs and in the completeness report."""
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    finish: list[str] = field(default_factory=list)
    truncations: int = 0
    repairs: int = 0

    def hook(self, rid: str, step: str):
        timer = Timer()

        def on_result(info: dict) -> None:
            u = info.get("usage") or {}
            self.llm_calls += 1
            self.tokens_in += u.get("input") or 0
            self.tokens_out += u.get("output") or 0
            self.finish.append(f"{step}:{info.get('finish')}")
            log(logger, logging.INFO, "tldr_llm_call", podcastId=rid, step=step, finish=info.get("finish"),
                inputTokens=u.get("input"), outputTokens=u.get("output"), attempt=info.get("attempt"), durationMs=timer.ms())

        return on_result


@dataclass
class Part:
    ids: list[str]
    lines: list[ScriptLine] = field(default_factory=list)
    title: str | None = None
    takeaways: list[str] = field(default_factory=list)


class ScriptIncomplete(RuntimeError):
    """A single point could not be generated even alone (for example it keeps hitting the token limit)."""


# --- manifest ----------------------------------------------------------------------------------

def split_paragraphs(text: str, max_chars: int) -> list[str]:
    """Chunks at paragraph (then sentence) boundaries, never mid-sentence."""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if len(para) <= max_chars:
            if para:
                units.append(para)
        else:
            units.extend(s for s in re.split(r"(?<=[.!?])\s+", para) if s)
    chunks, cur = [], ""
    for u in units:
        if cur and len(cur) + len(u) + 2 > max_chars:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{u}" if cur else u
    if cur:
        chunks.append(cur)
    return chunks or [text]


def build_manifest(llm, title: str, text: str, stats: Stats, rid: str, *, offset: int = 0,
                   source: str | None = None) -> list[dict[str, Any]]:
    """Points of one article. For a section, call it once per article with the number of points so far as
    `offset` (ids stay unique) and the article's id as `source` (each point remembers where it came from)."""
    points: list[dict[str, Any]] = []
    for n, chunk in enumerate(split_paragraphs(text, MANIFEST_CHUNK_CHARS), start=1):
        m = llm.generate_json("pro", prompts.MANIFEST_SYSTEM, prompts.manifest_prompt(title, chunk), CoverageManifest,
                              temperature=0.1, max_output_tokens=6000,
                              on_result=stats.hook(rid, f"manifest{n}" if source is None else f"manifest-{source}-{n}"))
        points += [p.model_dump() for p in m.points]
    for i, p in enumerate(points, start=offset + 1):
        p["id"] = f"k{i}"
        if source is not None:
            p["source"] = source
    return points


def est_words(point: dict[str, Any]) -> int:
    """Rough spoken length of one point; used only to size output budgets and group parts."""
    return 70 + 35 * len(point.get("required_facts") or []) + (25 if point.get("caveat") else 0)


def plan_outline(points: list[dict[str, Any]]) -> list[list[str]]:
    """Groups consecutive points into parts. Small content yields a single part (a single LLM call)."""
    parts: list[list[str]] = []
    cur: list[str] = []
    words = 0
    cur_source: str | None = None
    for p in points:
        w = est_words(p)
        # A part never spans two articles (section podcasts): each part is written from its own article's text.
        if cur and (len(cur) >= MAX_PART_POINTS or words + w > MAX_PART_WORDS or p.get("source") != cur_source):
            parts.append(cur)
            cur, words = [], 0
        cur_source = p.get("source")
        cur.append(p["id"])
        words += w
    if cur:
        parts.append(cur)
    return parts


# --- generation --------------------------------------------------------------------------------

def _budget(points: list[dict[str, Any]]) -> int:
    return max(MIN_TOKENS, min(MAX_TOKENS, 3 * sum(est_words(p) for p in points) + 500))


def _tail(parts: list[Part], n: int = 3) -> list[str]:
    lines = [ln for p in parts for ln in p.lines][-n:]
    return [f"{ln.speaker}: {ln.text}" for ln in lines]


def write_part(ctx: "Ctx", ids: list[str], first: bool, last: bool, done: list[Part], fix: str | None = None) -> list[Part]:
    """Generates one part. If the model hits its output limit the ids are split in half (each half is
    still generated from the full source) and the halves are returned in order. Never splits a point."""
    pts = [ctx.by_id[i] for i in ids]
    prompt = prompts.segment_prompt(
        title=ctx.title, text=ctx.text_for(ids), hosts=ctx.hosts, outline=ctx.outline_titles, first=first, last=last,
        stories=ctx.source_titles,
        points=_manifest_view(pts), covered_topics=[ctx.by_id[i]["topic"] for p in done for i in p.ids],
        previous_lines=_tail(done), est_words=sum(est_words(p) for p in pts), fix=fix, preferences=ctx.preferences, learning=ctx.learning)
    system = (prompts.INTERVIEW_SYSTEM + (prompts.PERSONALIZED_SYSTEM if ctx.preferences else "")
              + (prompts.LEARNING_SYSTEM if ctx.learning else ""))
    try:
        out = ctx.llm.generate_json("pro", system, prompt, ScriptPart, temperature=0.5,
                                    max_output_tokens=_budget(pts), on_result=ctx.stats.hook(ctx.rid, f"part{len(done) + 1}"))
    except AiOutputTruncated:
        ctx.stats.truncations += 1
        if len(ids) == 1:
            raise ScriptIncomplete(f"Point {ids[0]} does not fit in one generation call") from None
        mid = len(ids) // 2
        log(logger, logging.WARNING, "tldr_part_split", podcastId=ctx.rid, points=len(ids))
        a = write_part(ctx, ids[:mid], first, False, done, fix)
        return a + write_part(ctx, ids[mid:], False, last, done + a, fix)
    return [Part(ids=list(ids), lines=out.lines, title=out.title, takeaways=out.takeaways)]


@dataclass
class Ctx:
    llm: Any
    title: str
    text: str
    hosts: dict[str, str]
    points: list[dict[str, Any]]
    stats: Stats
    rid: str
    by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    outline_titles: list[str] = field(default_factory=list)
    # Section podcasts: article id -> that article's text block, and article id -> title. Empty for one article.
    sources: dict[str, str] = field(default_factory=dict)
    source_titles: dict[str, str] = field(default_factory=dict)
    learning: dict | None = None  # verified, topic-relevant learning context of a signed-in listener (app/learning/profile.py)
    preferences: str | None = None  # the listener's focus for a personalized episode; None = the default episode

    def __post_init__(self) -> None:
        self.by_id = {p["id"]: p for p in self.points}

    def text_for(self, ids: list[str]) -> str:
        """The source text a part is written from: the whole text for one article, otherwise only the articles
        whose points the part covers (keeps prompts small and stops facts from mixing between stories)."""
        if not self.sources:
            return self.text
        wanted = list(dict.fromkeys(self.by_id[i].get("source") for i in ids))
        return "\n\n".join(self.sources[s] for s in wanted if s in self.sources) or self.text


def generate_parts(ctx: Ctx, outline: list[list[str]]) -> list[Part]:
    parts: list[Part] = []
    for n, ids in enumerate(outline):
        parts += write_part(ctx, ids, n == 0, n == len(outline) - 1, parts)
    return parts


# --- validation --------------------------------------------------------------------------------

def _numbers(text: str) -> set[str]:
    return {m.replace(",", "").rstrip(".") for m in NUMBER_RE.findall(text)}


def flat_lines(parts: list[Part]) -> list[ScriptLine]:
    return [ln for p in parts for ln in p.lines]


def script_text(parts: list[Part]) -> str:
    return "\n".join(f"{i}. {ln.speaker}: {ln.text}" for i, ln in enumerate(flat_lines(parts)))


def deterministic_check(parts: list[Part], points: list[dict[str, Any]]) -> dict[str, list[str]]:
    """id -> problems. Every point must be cited by a line, and its multi-digit figures must be spoken
    in the lines that cite it."""
    cited: dict[str, list[str]] = {p["id"]: [] for p in points}
    for ln in flat_lines(parts):
        for i in ln.item_ids:
            if i in cited:
                cited[i].append(ln.text)
    problems: dict[str, list[str]] = {}
    for p in points:
        texts = cited[p["id"]]
        if not texts:
            problems[p["id"]] = [p["key_points"], *p.get("required_facts", [])]
            continue
        spoken = _numbers(" ".join(texts))
        missing = [f for f in p.get("required_facts", [])
                   if (need := {n for n in _numbers(f) if len(n.replace(".", "")) >= 2}) and not need <= spoken]
        if missing:
            problems[p["id"]] = missing
    return problems


def structure_problems(parts: list[Part]) -> list[str]:
    out = []
    lines = flat_lines(parts)
    if not lines or lines[0].speaker != "A":
        out.append("episode does not open with the interviewer")
    if not any(p.takeaways for p in parts[-1:]):
        out.append("episode has no closing takeaways")
    return out


@dataclass
class Validation:
    missing: dict[str, list[str]]  # point id -> facts not stated
    unsupported: list[int]  # global line indexes
    structure: list[str]

    @property
    def complete(self) -> bool:
        return not (self.missing or self.unsupported or self.structure)


def validate(ctx: Ctx, parts: list[Part]) -> Validation:
    missing = {k: list(v) for k, v in deterministic_check(parts, ctx.points).items()}
    report = ctx.llm.generate_json("pro", prompts.COVERAGE_SYSTEM,
                                   prompts.coverage_prompt(_manifest_view(ctx.points), script_text(parts), ctx.text),
                                   CoverageReport, temperature=0.0, max_output_tokens=3000,
                                   on_result=ctx.stats.hook(ctx.rid, "coverage"))
    for m in report.missing:
        if m.id in ctx.by_id and m.facts:
            have = missing.setdefault(m.id, [])
            have += [f for f in m.facts if f not in have]
    n = len(flat_lines(parts))
    return Validation(missing, sorted({u.line for u in report.unsupported if 0 <= u.line < n}), structure_problems(parts))


def _manifest_view(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: p[k] for k in ("id", "source", "topic", "priority", "key_points", "required_facts", "caveat") if k in p} for p in points]


def _part_index_of_line(parts: list[Part], line_idx: int) -> int:
    seen = 0
    for n, p in enumerate(parts):
        seen += len(p.lines)
        if line_idx < seen:
            return n
    return len(parts) - 1


def repair(ctx: Ctx, parts: list[Part], v: Validation, max_repairs: int) -> tuple[list[Part], Validation]:
    """Regenerates only the parts with gaps, then validates again, up to max_repairs times."""
    attempts = 0
    while not v.complete and attempts < max_repairs:
        attempts += 1
        ctx.stats.repairs += 1
        bad: dict[int, list[str]] = {}
        for pid, facts in v.missing.items():
            n = next(i for i, p in enumerate(parts) if pid in p.ids)
            bad.setdefault(n, []).append(f"{pid} ({ctx.by_id[pid]['topic']}): say " + "; ".join(facts))
        for li in v.unsupported:
            bad.setdefault(_part_index_of_line(parts, li), []).append("remove claims the article does not support")
        if v.structure and not bad:  # e.g. missing closing: regenerate the last part
            bad[len(parts) - 1] = v.structure
        log(logger, logging.INFO, "tldr_repair", podcastId=ctx.rid, attempt=attempts, parts=sorted(bad),
            missingIds=sorted(v.missing), unsupportedLines=len(v.unsupported), structure=v.structure)
        new_parts: list[Part] = []
        for n, old in enumerate(parts):
            if n in bad:
                new_parts += write_part(ctx, old.ids, n == 0, n == len(parts) - 1, new_parts, fix="; ".join(bad[n]))
            else:
                new_parts.append(old)
        parts = new_parts
        v = validate(ctx, parts)
    return parts, v


def report_for(points: list[dict[str, Any]], parts: list[Part], v: Validation, stats: Stats, extra: dict | None = None) -> dict:
    lines = flat_lines(parts)
    words = sum(len(ln.text.split()) for ln in lines)
    status = "complete" if v.complete else "partial"
    return {
        "status": status,
        "expectedPoints": len(points),
        "coveredPoints": len(points) - sum(1 for pid in v.missing if not any(pid in ln.item_ids for ln in lines)),
        "missing": [{"id": k, "topic": next(p["topic"] for p in points if p["id"] == k), "facts": f}
                    for k, f in v.missing.items()],
        "unsupportedLines": v.unsupported,
        "structure": v.structure,
        "words": words,
        "chars": sum(len(ln.text) for ln in lines),
        "lines": len(lines),
        "parts": len(parts),
        "llmCalls": stats.llm_calls,
        "repairs": stats.repairs,
        "truncations": stats.truncations,
        "finishReasons": stats.finish,
        "tokens": {"input": stats.tokens_in, "output": stats.tokens_out},
        **(extra or {}),
    }
