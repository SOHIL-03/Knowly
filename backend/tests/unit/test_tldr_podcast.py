"""TLDR story podcast: coverage-based script generation, validation and repair, chunked TTS and
assembly. The LLM and TTS are scripted stubs: deterministic, no network, no paid calls."""

from __future__ import annotations

import io
import re
import wave
from types import SimpleNamespace

import pytest

from app.ai.client import AiOutputTruncated, AiUnavailable
from app.ai.schemas import CoverageManifest, CoverageReport, ScriptPart
from app.audio.tts import SAMPLE_RATE, Speech, Turn, wav_from_pcm
from app.pipeline.enrich import EnrichResult
from app.tldr import audio, coverage, podcast
from app.tldr.scraper import Issue, Section, Story, TldrError

# --- stubs -----------------------------------------------------------------------------------------

ARTICLE = "The Roadster launched itself up a ramp and leapt over five cars. " * 20


def make_points(n: int, facts: int = 2) -> list[dict]:
    return [{"topic": f"Topic {i}", "priority": "core", "key_points": f"Key point {i} happened",
             "required_facts": [f"Figure {i}: {100 + i * 7} units", f"Name {i} confirmed"][:facts], "caveat": None}
            for i in range(1, n + 1)]


class ScriptedLLM:
    """Plays the three roles (manifest, script part, coverage audit) from a prepared manifest.

    omit: ids left out of the first script only; omit_always: left out of every attempt;
    drop_figures: ids whose answer leaves out the numbers on the first attempt; max_points: parts with
    more points hit the output limit; unsupported_line: line the first audit calls unsupported."""

    def __init__(self, points, *, omit=(), omit_always=(), drop_figures=(), max_points=None, unsupported_line=None):
        self.points, self.omit, self.omit_always = points, set(omit), set(omit_always)
        self.drop_figures, self.max_points, self.unsupported_line = set(drop_figures), max_points, unsupported_line
        self.prompts: list[tuple[str, str]] = []  # (schema, prompt)
        self.regenerated: set[str] = set()
        self.caps: list[int | None] = []
        self.audits = 0

    def generate_json(self, tier, system, prompt, schema, *, temperature=0.2, max_output_tokens=None, on_result=None):
        self.prompts.append((schema.__name__, prompt))
        self.caps.append(max_output_tokens)
        result = self._respond(schema, prompt)
        if on_result:
            on_result({"finish": "stop", "usage": {"input": 100, "output": 50, "total": 150}, "attempt": 1})
        return result

    def _respond(self, schema, prompt):
        if schema is CoverageManifest:
            return CoverageManifest.model_validate({"points": self.points})
        if schema is ScriptPart:
            return self._part(prompt)
        assert schema is CoverageReport
        return self._audit(prompt)

    def _part(self, prompt):
        section = prompt.split("Points to cover in THIS part")[1].split("Already covered")[0].split("These points normally")[0]
        ids = re.findall(r'"id": "(k\d+)"', section)
        if self.max_points is not None and len(ids) > self.max_points:
            raise AiOutputTruncated("output limit")
        fix = "previous attempt" in prompt
        lines = []
        if "Begin with a one-line welcome" in prompt:
            lines.append({"speaker": "A", "text": "Welcome to the show."})
        for i in ids:
            p = self.points[int(i[1:]) - 1]
            if i in self.omit_always or (i in self.omit and not fix):
                continue
            if fix:
                self.regenerated.add(i)
            facts = "" if i in self.drop_figures and not fix else " " + " and ".join(p["required_facts"]) + "."
            lines += [{"speaker": "A", "text": f"What about {p['topic']}?"},
                      {"speaker": "B", "text": p["key_points"] + "." + facts, "item_ids": [i]}]
        if not lines:
            lines = [{"speaker": "A", "text": "Moving on."}, {"speaker": "B", "text": "Yes."}]
        last = "closing part" in prompt or "only part" in prompt
        return ScriptPart.model_validate({"title": "Episode" if last else None, "lines": lines,
                                          "takeaways": ["Main takeaway"] if last else []})

    def _audit(self, prompt):
        self.audits += 1
        script = prompt.split("SCRIPT (numbered lines):")[1].split("<article>")[0]
        missing = [{"id": f"k{n}", "facts": [p["key_points"]]} for n, p in enumerate(self.points, start=1)
                   if p["key_points"] not in script]
        unsupported = []
        if self.unsupported_line is not None and self.audits == 1:
            unsupported = [{"line": self.unsupported_line, "reason": "not in article"}]
        return CoverageReport.model_validate({"missing": missing, "unsupported": unsupported})


class Blobs:
    def __init__(self):
        self.data: dict[str, bytes] = {}

    def put_text(self, path, text, content_type=""):
        self.data[path] = text.encode()

    def put_bytes(self, path, data, content_type):
        self.data[path] = data

    def get_text(self, path):
        return self.data[path].decode() if path in self.data else None

    def read_url(self, path):
        return f"/media/{path}"


class TTS:
    """Speaks 14 characters per second. fail_first: raise a transient error for the first N calls;
    short_first: then return a cut-off clip for the next N calls; always_fail: never succeeds."""

    def __init__(self, fail_first=0, short_first=0, always_fail=False):
        self.calls: list[list[Turn]] = []
        self.fail_first, self.short_first, self.always_fail = fail_first, short_first, always_fail
        self.on_call = None

    def synthesize(self, turns, voices):
        self.calls.append(turns)
        if self.on_call:
            self.on_call()
        n = len(self.calls)
        if self.always_fail or n <= self.fail_first:
            raise AiUnavailable("transient", "server_unavailable")
        chars = sum(len(t.text) for t in turns)
        secs = 0.2 if n <= self.fail_first + self.short_first else chars / audio.CHARS_PER_SEC
        # every clip is one constant sample value equal to its call number, so assembly order is checkable
        pcm = n.to_bytes(2, "little", signed=True) * int(SAMPLE_RATE * secs)
        return Speech(wav_from_pcm(pcm), len(pcm) / 2 / SAMPLE_RATE, [(len(turns), secs)])


class Dispatcher:
    def __init__(self):
        self.tasks = []

    def task(self, name, payload, **kw):
        self.tasks.append((name, payload))


def ctx_for(llm):
    points = [dict(p, id=f"k{i}") for i, p in enumerate(llm.points, start=1)]
    return coverage.Ctx(llm, "Article title", ARTICLE, {"A": "Maya", "B": "Theo"}, points, coverage.Stats(), "rid")


def run_script(llm, max_repairs=2):
    ctx = ctx_for(llm)
    outline = coverage.plan_outline(ctx.points)
    ctx.outline_titles = [f"part {n}" for n in range(len(outline))]
    parts = coverage.generate_parts(ctx, outline)
    parts, v = coverage.repair(ctx, parts, coverage.validate(ctx, parts), max_repairs)
    return ctx, outline, parts, v, coverage.report_for(ctx.points, parts, v, ctx.stats)


def part_calls(llm):
    return [p for s, p in llm.prompts if s == "ScriptPart"]


# --- 1. short article: one generation call -------------------------------------------------------------------

def test_short_article_is_one_generation_call_with_welcome_and_closing():
    llm = ScriptedLLM(make_points(2))
    _, outline, parts, v, report = run_script(llm)
    assert len(outline) == 1 and len(part_calls(llm)) == 1
    assert report["status"] == "complete" and report["expectedPoints"] == report["coveredPoints"] == 2
    prompt = part_calls(llm)[0]
    assert "only part" in prompt and "Begin with a one-line welcome" in prompt and "End with a brief recap" in prompt
    assert parts[0].takeaways == ["Main takeaway"] and report["llmCalls"] == 2  # script part + coverage audit (the manifest call is made by generate())


# --- 2 + 3. long article with many points and figures: several calls, no repetition --------------------------------

def test_long_article_uses_several_calls_with_continuity_and_every_figure_spoken():
    llm = ScriptedLLM(make_points(12, facts=2))
    _, outline, parts, v, report = run_script(llm)
    calls = part_calls(llm)
    assert len(outline) == len(calls) >= 3 and sum(len(o) for o in outline) == 12
    assert "opening part" in calls[0] and "closing part" in calls[-1] and "a middle part" in calls[1]
    assert "Begin with a one-line welcome" not in calls[1] and "do not repeat" in calls[1] and "Topic 1" in calls[1]
    assert "Last lines of the previous part" in calls[1]
    spoken = " ".join(ln.text for ln in coverage.flat_lines(parts))
    for p in llm.points:
        assert all(f in spoken for f in p["required_facts"])  # figures are in the script, not summarised away
    assert report["status"] == "complete" and report["words"] > 100
    assert all(cap and cap >= coverage.MIN_TOKENS for cap in llm.caps)  # every call has an output budget


def test_parts_are_grouped_at_point_boundaries():
    pts = [dict(p, id=f"k{i}") for i, p in enumerate(make_points(11, facts=5), start=1)]
    outline = coverage.plan_outline(pts)
    assert [i for ids in outline for i in ids] == [p["id"] for p in pts]
    assert all(len(ids) <= coverage.MAX_PART_POINTS for ids in outline)


# --- 4 + 5. output limit: the part is split, never truncated silently ---------------------------------------------

def test_output_limit_splits_the_part_in_order_without_losing_points():
    llm = ScriptedLLM(make_points(4), max_points=2)
    ctx, outline, parts, v, report = run_script(llm)
    calls = part_calls(llm)
    assert len(outline) == 1 and ctx.stats.truncations == 1
    assert [i for p in parts for i in p.ids] == ["k1", "k2", "k3", "k4"]
    assert report["status"] == "complete"
    assert "only part" in calls[0] and "opening part" in calls[1] and "closing part" in calls[2]  # first call hit the limit


def test_a_single_point_that_cannot_fit_fails_explicitly():
    with pytest.raises(coverage.ScriptIncomplete):
        run_script(ScriptedLLM(make_points(1), max_points=0))


# --- 6 / 7 / 8. coverage validation and repair -----------------------------------------------------------------------

def test_missing_point_is_reported_when_repairs_are_off():
    _, _, _, v, report = run_script(ScriptedLLM(make_points(3), omit={"k2"}), max_repairs=0)
    assert report["status"] == "partial" and report["coveredPoints"] == 2 and report["repairs"] == 0
    assert [m["id"] for m in report["missing"]] == ["k2"]


def test_numeric_fact_missing_from_the_script_is_caught_deterministically():
    llm = ScriptedLLM(make_points(2), drop_figures={"k1"})
    ctx = ctx_for(llm)
    parts = coverage.generate_parts(ctx, coverage.plan_outline(ctx.points))
    problems = coverage.deterministic_check(parts, ctx.points)
    assert list(problems) == ["k1"] and "107 units" in problems["k1"][0]


def test_missing_section_is_repaired_and_the_result_is_complete():
    llm = ScriptedLLM(make_points(8), omit={"k7"}, drop_figures={"k2"})
    _, outline, parts, v, report = run_script(llm, max_repairs=2)
    assert len(outline) == 2 and report["status"] == "complete" and report["repairs"] == 1
    assert report["coveredPoints"] == 8 and len(part_calls(llm)) == 4  # both parts had gaps: 2 + 2, no further rounds


def test_repair_regenerates_only_the_part_with_the_gap():
    llm = ScriptedLLM(make_points(8), omit={"k7"})
    _, outline, parts, v, report = run_script(llm)
    assert report["status"] == "complete" and len(part_calls(llm)) == len(outline) + 1
    assert llm.regenerated <= set(outline[-1])


def test_retry_limit_ends_in_an_explicit_partial_result_never_a_false_complete():
    llm = ScriptedLLM(make_points(3), omit_always={"k3"})
    _, _, _, v, report = run_script(llm, max_repairs=2)
    assert report["status"] == "partial" and report["repairs"] == 2
    assert [m["id"] for m in report["missing"]] == ["k3"] and len(part_calls(llm)) == 1 + 2


def test_unsupported_line_triggers_regeneration_of_its_part():
    llm = ScriptedLLM(make_points(2), unsupported_line=1)
    _, _, _, v, report = run_script(llm, max_repairs=1)
    assert report["status"] == "complete" and report["repairs"] == 1
    assert "remove claims the article does not support" in part_calls(llm)[-1]


# --- 9 / 10 / 11. TTS chunking, retry, ordering ----------------------------------------------------------------------

def turns(n, chars=400):
    return [Turn("Maya" if i % 2 == 0 else "Theo", f"{i:03d} " + "x" * chars) for i in range(n)]


VOICES = {"Maya": "Kore", "Theo": "Puck"}


def test_script_larger_than_the_request_limit_is_split_and_every_turn_is_spoken_once():
    tts, t = TTS(), turns(20)  # about 8,000 characters against a 2,500 character limit
    out = audio.synthesize_all(tts, t, VOICES, chunk_chars=2500, rid="r", sleep=lambda s: None)
    assert len(tts.calls) == len(out.chunk_chars) >= 4
    assert all(sum(len(x.text) for x in c) <= 2500 for c in tts.calls)
    assert [x.text for c in tts.calls for x in c] == [x.text for x in t]  # nothing lost, duplicated or reordered
    assert sum(n for n, _ in out.parts) == 20


def test_failed_chunk_is_retried_and_the_episode_is_still_complete():
    sleeps, tts = [], TTS(fail_first=1)
    out = audio.synthesize_all(tts, turns(8), VOICES, chunk_chars=2500, rid="r", sleep=sleeps.append)
    assert out.tts_calls == len(tts.calls) == len(out.chunk_chars) + 1 and sleeps == [2.0]
    assert sum(n for n, _ in out.parts) == 8


def test_cut_off_audio_chunk_is_retried():
    tts = TTS(short_first=1)
    out = audio.synthesize_all(tts, turns(4), VOICES, chunk_chars=2500, rid="r", sleep=lambda s: None)
    assert out.tts_calls == len(out.chunk_chars) + 1
    assert out.duration_sec >= 0.9 * sum(out.chunk_chars) / audio.CHARS_PER_SEC


def test_chunk_that_never_succeeds_fails_the_audio_instead_of_dropping_it():
    with pytest.raises(AiUnavailable, match="part 1 of"):
        audio.synthesize_all(TTS(always_fail=True), turns(4), VOICES, chunk_chars=2500, rid="r", sleep=lambda s: None)


def test_chunks_are_concatenated_in_order_with_matching_duration():
    tts = TTS()
    out = audio.synthesize_all(tts, turns(14, chars=100), VOICES, chunk_chars=250, rid="r", sleep=lambda s: None)
    assert len(tts.calls) == 7
    with wave.open(io.BytesIO(out.wav), "rb") as w:
        samples = w.readframes(w.getnframes())
        assert w.getframerate() == SAMPLE_RATE and w.getnframes() / SAMPLE_RATE == pytest.approx(out.duration_sec, abs=0.01)
    values = [int.from_bytes(samples[i : i + 2], "little", signed=True) for i in range(0, len(samples), 2000)]
    runs = [v for k, v in enumerate(values) if k == 0 or v != values[k - 1]]
    assert runs == list(range(1, 8))  # chunk 1, then 2 ... then 7: never reordered, replaced or dropped
    assert out.duration_sec == pytest.approx(sum(s for _, s in out.parts), abs=0.01)


# --- end to end through the task handler -------------------------------------------------------------------------------

ISSUE = Issue("tech", "2026-10-16", "https://tldr.tech/tech/2026-10-16", "Roadster", [
    Section("Big Tech", "", [Story("s1", "Roadster flies", "https://a.example/roadster", "It flew.", 4),
                             Story("s2", "Ad", "https://ad.example", "Buy.", None, True)]),
    Section("Quick", "", [Story("s3", "Fast", "https://paywalled.example/x", "Short note about a fast thing.", 1)])])


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(podcast.scraper, "get_issue", lambda date, category="tech": ISSUE)
    monkeypatch.setattr(podcast, "fetch_article", lambda url, **kw: EnrichResult(None, True, "paywall", []) if "paywalled" in url
                        else EnrichResult(ARTICLE, False, "ok", []))
    monkeypatch.setattr(audio.time, "sleep", lambda s: None)
    s = SimpleNamespace(tts_host_a_name="Maya", tts_host_a_voice="Kore", tts_host_b_name="Theo", tts_host_b_voice="Puck",
                        enrich_timeout_seconds=5, enrich_max_chars=50000, tldr_max_repairs=2, tts_chunk_chars=2500,
                        openai_model="gpt-4o-mini")
    return SimpleNamespace(settings=s, blobs=Blobs(), llm=ScriptedLLM(make_points(6)), tts=TTS(), dispatcher=Dispatcher(), http=None)


def test_request_generate_and_serve_the_whole_episode(svc):
    assert podcast.request(svc, "tech", "2026-10-16", "s1") == {"status": "processing"}
    assert svc.dispatcher.tasks == [("tldr-podcast", {"category": "tech", "date": "2026-10-16", "storyId": "s1"})]
    assert podcast.request(svc, "tech", "2026-10-16", "s1")["status"] == "processing"
    assert len(svc.dispatcher.tasks) == 1

    seen = {}
    svc.tts.on_call = lambda: seen.update(podcast.view(svc, "tech", "2026-10-16", "s1"))
    assert podcast.generate(svc, "tech", "2026-10-16", "s1") == "ok"

    # the script and its completeness report are inspectable BEFORE any audio is made
    assert seen["status"] == "processing" and seen["phase"] == "audio" and seen["report"]["status"] == "complete"
    assert seen["lines"] and "audioUrl" not in seen

    v = podcast.view(svc, "tech", "2026-10-16", "s1")
    r = v["report"]
    assert v["status"] == "ready" and v["audioUrl"].endswith("s1.wav") and r["status"] == "complete"
    assert r["expectedPoints"] == r["coveredPoints"] == 6 and r["llmCalls"] >= 3 and r["ttsCalls"] >= 1
    # what was spoken is exactly the validated script, and the stored audio is the full assembled episode
    assert "".join(t.text for call in svc.tts.calls for t in call) == "".join(ln["text"] for ln in v["lines"])
    with wave.open(io.BytesIO(svc.blobs.data["podcasts/tldr/tech/2026-10-16/s1.wav"]), "rb") as w:
        stored = w.getnframes() / w.getframerate()
    assert stored == pytest.approx(r["audioSeconds"], abs=0.1) and v["durationSec"] == round(stored)
    assert v["lines"][0]["role"] == "question" and {ln["role"] for ln in v["lines"]} == {"question", "answer"}
    assert podcast.generate(svc, "tech", "2026-10-16", "s1") == "skipped"


def test_paywalled_article_uses_the_summary_and_says_so(svc):
    podcast.request(svc, "tech", "2026-10-16", "s3")
    podcast.generate(svc, "tech", "2026-10-16", "s3")
    assert podcast.view(svc, "tech", "2026-10-16", "s3")["basis"] == "summary"
    assert "Short note about a fast thing." in svc.llm.prompts[0][1]


def test_tts_failure_keeps_the_script_and_report_for_inspection(svc):
    svc.tts = TTS(always_fail=True)
    podcast.request(svc, "tech", "2026-10-16", "s1")
    assert podcast.generate(svc, "tech", "2026-10-16", "s1") == "failed"
    v = podcast.view(svc, "tech", "2026-10-16", "s1")
    assert v["status"] == "failed" and v["lines"] and v["report"]["status"] == "complete" and "audioUrl" not in v
    assert podcast.request(svc, "tech", "2026-10-16", "s1") == {"status": "processing"}  # can be retried


def test_incomplete_script_is_labelled_partial_but_still_playable(svc):
    svc.llm = ScriptedLLM(make_points(3), omit_always={"k2"})
    podcast.request(svc, "tech", "2026-10-16", "s1")
    podcast.generate(svc, "tech", "2026-10-16", "s1")
    v = podcast.view(svc, "tech", "2026-10-16", "s1")
    assert v["status"] == "ready" and v["report"]["status"] == "partial" and v["report"]["missing"][0]["id"] == "k2"


def test_llm_failure_is_recorded(svc):
    class Down:
        def generate_json(self, *a, **k):
            raise AiUnavailable("down")

    svc.llm = Down()
    podcast.request(svc, "tech", "2026-10-16", "s1")
    assert podcast.generate(svc, "tech", "2026-10-16", "s1") == "failed"
    assert podcast.view(svc, "tech", "2026-10-16", "s1")["status"] == "failed"


def test_sponsored_or_unknown_story_is_rejected(svc):
    for sid in ("s2", "s99"):
        with pytest.raises(TldrError):
            podcast.request(svc, "tech", "2026-10-16", sid)
