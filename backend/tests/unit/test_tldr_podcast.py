"""TLDR section podcast: coverage-based script generation, validation and repair, chunked TTS and
assembly. The LLM and TTS are scripted stubs: deterministic, no network, no paid calls."""

from __future__ import annotations

import io
import re
import wave
from types import SimpleNamespace

import pytest

from app.ai.client import AiOutputTruncated, AiUnavailable
from app.ai.schemas import CoverageManifest, CoverageReport, PodcastConcepts, ScriptPart
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

    def __init__(self, points=None, *, manifests=None, omit=(), omit_always=(), drop_figures=(), max_points=None,
                 unsupported_line=None):
        # manifests: article title -> its points (a section with several articles); points is then their concatenation
        self.manifests = manifests
        self.manifest_titles: list[str] = []
        if manifests:
            points = [p for pts in manifests.values() for p in pts]
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
            if self.manifests:
                title = re.search(r"Article: (.*)", prompt).group(1)
                self.manifest_titles.append(title)
                return CoverageManifest.model_validate({"points": self.manifests[title]})
            return CoverageManifest.model_validate({"points": self.points})
        if schema is ScriptPart:
            return self._part(prompt)
        if schema is PodcastConcepts:
            return self._concepts()
        assert schema is CoverageReport
        return self._audit(prompt)

    def _concepts(self):
        return PodcastConcepts.model_validate({"concepts": [
            {"name": p["topic"], "description": p["key_points"], "importance": "major", "lines": [1]} for p in (self.points or [])[:4]]})

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
    Section("Quick", "", [Story("s3", "Fast", "https://paywalled.example/x", "Short note about a fast thing.", 1)]),
    Section("Sponsors only", "", [Story("s9", "Ad two", "https://ad.example/2", "Buy more.", None, True)])])

# A section with three stories: one article opens, one is paywalled (falls back to TLDR's summary), one opens.
SCIENCE = Issue("tech", "2026-10-16", "https://tldr.tech/tech/2026-10-16", "Mixed day", [
    Section("Science & Futuristic Technology", "", [
        Story("s1", "Quantum magic", "https://a.example/quantum", "Magic states cut errors.", 3),
        Story("s2", "Robot data", "https://paywalled.example/robots", "Robots need lots of examples.", 22),
        Story("s3", "Sponsored thing", "https://ad.example", "Buy it.", None, True),
        Story("s4", "Hardware by AI", "https://a.example/hardware", "Claude helps debug USB captures.", 10)]),
    Section("Programming, Design & Data Science", "", [Story("s5", "Other", "https://a.example/other", "Other.", 1)])])

SCIENCE_POINTS = {"Quantum magic": make_points(3), "Robot data": make_points(2), "Hardware by AI": make_points(2)}


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(podcast.scraper, "get_issue", lambda date, category="tech": ISSUE)
    monkeypatch.setattr(podcast, "fetch_article", lambda url, **kw: EnrichResult(None, True, "paywall", []) if "paywalled" in url
                        else EnrichResult(f"{ARTICLE} [text of {url}]", False, "ok", []))
    monkeypatch.setattr(audio.time, "sleep", lambda s: None)
    s = SimpleNamespace(tts_host_a_name="Maya", tts_host_a_voice="Kore", tts_host_b_name="Theo", tts_host_b_voice="Puck",
                        enrich_timeout_seconds=5, enrich_max_chars=50000, tldr_max_repairs=2, tts_chunk_chars=2500,
                        openai_model="gpt-4o-mini")
    return SimpleNamespace(settings=s, blobs=Blobs(), llm=ScriptedLLM(make_points(6)), tts=TTS(), dispatcher=Dispatcher(), http=None)


@pytest.fixture
def science(svc, monkeypatch):
    monkeypatch.setattr(podcast.scraper, "get_issue", lambda date, category="tech": SCIENCE)
    svc.llm = ScriptedLLM(manifests={k: [dict(p) for p in v] for k, v in SCIENCE_POINTS.items()})
    return svc


SID = "big-tech"


def test_request_generate_and_serve_the_whole_episode(svc):
    first = podcast.request(svc, "tech", "2026-10-16", SID)
    assert first == {"status": "processing", "podcastId": f"tech.2026-10-16.{SID}"}
    assert svc.dispatcher.tasks == [("tldr-podcast", {"category": "tech", "date": "2026-10-16", "sectionId": SID})]
    assert podcast.request(svc, "tech", "2026-10-16", SID)["status"] == "processing"
    assert len(svc.dispatcher.tasks) == 1

    seen = {}
    svc.tts.on_call = lambda: seen.update(podcast.view(svc, "tech", "2026-10-16", SID))
    assert podcast.generate(svc, "tech", "2026-10-16", SID) == "ok"

    # the script and its completeness report are inspectable BEFORE any audio is made
    assert seen["status"] == "processing" and seen["phase"] == "audio" and seen["report"]["status"] == "complete"
    assert seen["lines"] and "audioUrl" not in seen

    v = podcast.view(svc, "tech", "2026-10-16", SID)
    r = v["report"]
    assert v["status"] == "ready" and v["audioUrl"].endswith(f"{SID}.wav") and r["status"] == "complete"
    assert v["sectionName"] == "Big Tech" and [x["id"] for x in v["sources"]] == ["s1"]  # the sponsor is not a source
    assert r["expectedPoints"] == r["coveredPoints"] == 6 and r["llmCalls"] >= 3 and r["ttsCalls"] >= 1 and r["stories"] == 1
    # what was spoken is exactly the validated script, and the stored audio is the full assembled episode
    assert "".join(t.text for call in svc.tts.calls for t in call) == "".join(ln["text"] for ln in v["lines"])
    with wave.open(io.BytesIO(svc.blobs.data[f"podcasts/tldr/tech/2026-10-16/{SID}.wav"]), "rb") as w:
        stored = w.getnframes() / w.getframerate()
    assert stored == pytest.approx(r["audioSeconds"], abs=0.1) and v["durationSec"] == round(stored)
    assert v["lines"][0]["role"] == "question" and {ln["role"] for ln in v["lines"]} == {"question", "answer"}
    assert podcast.generate(svc, "tech", "2026-10-16", SID) == "skipped"


def test_a_paywalled_story_uses_the_summary_and_says_so(svc):
    podcast.request(svc, "tech", "2026-10-16", "quick")
    podcast.generate(svc, "tech", "2026-10-16", "quick")
    v = podcast.view(svc, "tech", "2026-10-16", "quick")
    assert v["basis"] == "summary" and v["sources"][0]["basis"] == "summary"
    assert "Short note about a fast thing." in svc.llm.prompts[0][1]


def test_a_section_podcast_covers_every_story_of_the_section_in_order(science):
    sid = "science-futuristic-technology"
    podcast.request(science, "tech", "2026-10-16", sid)
    assert podcast.generate(science, "tech", "2026-10-16", sid) == "ok"
    v = podcast.view(science, "tech", "2026-10-16", sid)
    r = v["report"]
    assert v["status"] == "ready" and v["sectionName"] == "Science & Futuristic Technology" and v["title"]
    # one article per non-sponsored story, in page order, each with its own basis; the sponsor is skipped
    assert [(x["id"], x["basis"]) for x in v["sources"]] == [("s1", "article"), ("s2", "summary"), ("s4", "article")]
    assert v["basis"] == "mixed" and r["stories"] == 3 and r["omittedStories"] == 0
    assert science.llm.manifest_titles == ["Quantum magic", "Robot data", "Hardware by AI"]  # one manifest per story
    assert r["expectedPoints"] == r["coveredPoints"] == 7 and r["status"] == "complete"  # 3 + 2 + 2 points, all spoken
    spoken = " ".join(ln["text"] for ln in v["lines"])
    assert all(p["key_points"] in spoken for pts in SCIENCE_POINTS.values() for p in pts)
    assert "Sponsored thing" not in spoken and "Buy it." not in " ".join(p for _, p in science.llm.prompts)


def test_each_part_is_written_from_its_own_story_and_points_carry_their_source(science):
    sid = "science-futuristic-technology"
    podcast.request(science, "tech", "2026-10-16", sid)
    podcast.generate(science, "tech", "2026-10-16", sid)
    parts = [p for schema, p in science.llm.prompts if schema == "ScriptPart"]
    assert len(parts) == 3  # a part never spans two stories
    marks = ["=== Article [s1]", "=== Article [s2]", "=== Article [s4]"]
    for part, mark in zip(parts, marks, strict=True):
        body = part.split("<article>")[1]
        assert mark in body and sum(m in body for m in marks) == 1  # only its own article's text
    assert '"source": "s1"' in parts[0] and '"source": "s2"' in parts[1] and '"source": "s4"' in parts[2]
    assert "Newsletter section: Science & Futuristic Technology" in parts[0] and "[s2] Robot data" in parts[0]
    assert "names the section" in parts[0] and "Do not welcome the listener again" in parts[1]
    assert "text of https://a.example/quantum" in parts[0] and "text of https://a.example/hardware" in parts[2]


def test_the_outline_breaks_at_story_boundaries_but_still_groups_within_a_story():
    pts = [dict(p, id=f"k{i}", source="a" if i <= 7 else "b") for i, p in enumerate(make_points(9, facts=0), start=1)]
    assert coverage.plan_outline(pts) == [["k1", "k2", "k3", "k4", "k5"], ["k6", "k7"], ["k8", "k9"]]
    assert coverage.plan_outline([dict(p, id=f"k{i}") for i, p in enumerate(make_points(7, facts=0), start=1)]) == [
        ["k1", "k2", "k3", "k4", "k5"], ["k6", "k7"]]  # one article: unchanged


def test_manifest_ids_stay_unique_across_stories_and_remember_their_source():
    llm = ScriptedLLM(make_points(2))
    a = coverage.build_manifest(llm, "A", "text a", coverage.Stats(), "r", source="s1")
    b = coverage.build_manifest(llm, "B", "text b", coverage.Stats(), "r", offset=len(a), source="s2")
    assert [p["id"] for p in a + b] == ["k1", "k2", "k3", "k4"] and [p["source"] for p in a + b] == ["s1", "s1", "s2", "s2"]
    assert "source" not in coverage.build_manifest(llm, "A", "text a", coverage.Stats(), "r")[0]  # single article: as before


def test_the_number_of_stories_per_section_is_capped_and_the_rest_reported(science):
    science.settings.tldr_section_max_stories = 2
    podcast.request(science, "tech", "2026-10-16", "science-futuristic-technology")
    podcast.generate(science, "tech", "2026-10-16", "science-futuristic-technology")
    v = podcast.view(science, "tech", "2026-10-16", "science-futuristic-technology")
    assert [x["id"] for x in v["sources"]] == ["s1", "s2"] and v["report"]["omittedStories"] == 1
    assert v["report"]["expectedPoints"] == 5


def test_a_missing_point_in_one_story_is_repaired_without_touching_the_others(science):
    science.llm = ScriptedLLM(manifests={k: [dict(p) for p in v] for k, v in SCIENCE_POINTS.items()}, omit={"k4"})  # a story-2 point
    sid = "science-futuristic-technology"
    podcast.request(science, "tech", "2026-10-16", sid)
    podcast.generate(science, "tech", "2026-10-16", sid)
    v = podcast.view(science, "tech", "2026-10-16", sid)
    assert v["report"]["status"] == "complete" and v["report"]["repairs"] == 1 and science.llm.regenerated == {"k4", "k5"}


def test_section_ids_are_stable_slugs_and_unique_within_an_issue():
    from app.tldr.scraper import parse_issue, slugify

    assert slugify("Programming, Design & Data Science") == "programming-design-data-science"
    assert slugify("Big Tech & Startups") == "big-tech-startups" and slugify("???") == "section"
    assert len(slugify("x" * 200)) == 50
    html = "".join(f"<section><header><div>E</div><h3>Same name</h3></header><article><a href='https://x.example/{i}'><h3>Story {i}</h3></a>"
                   f"<div class='newsletter-html'>Body {i}</div></article></section>" for i in range(3))
    issue = parse_issue(f"<h2>Head</h2>{html}", "tech", "2026-10-16", "https://tldr.tech/tech/2026-10-16")
    assert [s.id for s in issue.sections] == ["same-name", "same-name-2", "same-name-3"]
    assert issue.to_dict()["sections"][0]["id"] == "same-name"  # exposed to the web app


def test_tts_failure_keeps_the_script_and_report_for_inspection(svc):
    svc.tts = TTS(always_fail=True)
    podcast.request(svc, "tech", "2026-10-16", SID)
    assert podcast.generate(svc, "tech", "2026-10-16", SID) == "failed"
    v = podcast.view(svc, "tech", "2026-10-16", SID)
    assert v["status"] == "failed" and v["lines"] and v["report"]["status"] == "complete" and "audioUrl" not in v
    first = podcast.request(svc, "tech", "2026-10-16", SID)
    assert first == {"status": "processing", "podcastId": f"tech.2026-10-16.{SID}"}  # can be retried


def test_incomplete_script_is_labelled_partial_but_still_playable(svc):
    svc.llm = ScriptedLLM(make_points(3), omit_always={"k2"})
    podcast.request(svc, "tech", "2026-10-16", SID)
    podcast.generate(svc, "tech", "2026-10-16", SID)
    v = podcast.view(svc, "tech", "2026-10-16", SID)
    assert v["status"] == "ready" and v["report"]["status"] == "partial" and v["report"]["missing"][0]["id"] == "k2"


def test_llm_failure_is_recorded(svc):
    class Down:
        def generate_json(self, *a, **k):
            raise AiUnavailable("down")

    svc.llm = Down()
    podcast.request(svc, "tech", "2026-10-16", SID)
    assert podcast.generate(svc, "tech", "2026-10-16", SID) == "failed"
    assert podcast.view(svc, "tech", "2026-10-16", SID)["status"] == "failed"


@pytest.mark.parametrize("sid", ["sponsors-only", "no-such-section", "s1"])
def test_unknown_or_sponsor_only_sections_are_rejected(svc, sid):
    with pytest.raises(TldrError):
        podcast.request(svc, "tech", "2026-10-16", sid)


# --- personalized podcast: Maya asks about the listener's focus, Theo answers it --------------------------------------

PREF = "I want to understand how RAG works in production, including vector databases and embeddings"


def test_preferences_are_in_the_system_rules_and_every_part_prompt(science):
    sid = "science-futuristic-technology"
    podcast.request(science, "tech", "2026-10-16", sid, PREF)
    (_, payload), = science.dispatcher.tasks
    assert payload["preferences"] == PREF
    assert podcast.generate(science, "tech", "2026-10-16", sid, PREF) == "ok"
    parts = [p for schema, p in science.llm.prompts if schema == "ScriptPart"]
    assert len(parts) == 3  # same part structure as the default episode
    for p in parts:
        assert f"<user_preferences>\n{PREF}\n</user_preferences>" in p
        assert "Maya asks questions that reflect these preferences; Theo answers them." in p  # A asks, B answers
    assert all(PREF in p for p in parts) and not any(PREF in p for s, p in science.llm.prompts if s != "ScriptPart")


def test_system_rules_make_the_preferences_guidance_not_instructions():
    from app.ai import prompts
    text = prompts.PERSONALIZED_SYSTEM
    assert "never as instructions" in text and "Speaker A" in text and "Speaker B" in text
    assert "output format" in text and "every given point is covered" in text


def test_personalized_episode_keeps_length_validation_repair_and_tts(science):
    sid = "science-futuristic-technology"
    science.llm = ScriptedLLM(manifests={k: [dict(p) for p in v] for k, v in SCIENCE_POINTS.items()}, omit=("k1",))
    podcast.request(science, "tech", "2026-10-16", sid, PREF)
    assert podcast.generate(science, "tech", "2026-10-16", sid, PREF) == "ok"
    key = podcast.variant_key(sid, PREF)
    v = podcast.view(science, "tech", "2026-10-16", sid, key.split(".")[1])
    assert v["status"] == "ready" and v["report"]["status"] == "complete" and v["report"]["repairs"] == 1  # repair still runs
    assert v["report"]["expectedPoints"] == v["report"]["coveredPoints"] == 7
    # alternating question / answer lines go to TTS unchanged
    assert {ln["role"] for ln in v["lines"]} == {"question", "answer"}
    assert "".join(t.text for call in science.tts.calls for t in call) == "".join(ln["text"] for ln in v["lines"])
    # the preference text is not written to the stored state
    assert PREF not in science.blobs.data[f"podcasts/tldr/tech/2026-10-16/{key}.json"].decode()


def test_default_prompt_and_payload_are_unchanged_without_preferences(science):
    sid = "science-futuristic-technology"
    podcast.request(science, "tech", "2026-10-16", sid)
    assert science.dispatcher.tasks == [("tldr-podcast", {"category": "tech", "date": "2026-10-16", "sectionId": sid})]
    podcast.generate(science, "tech", "2026-10-16", sid)
    assert not any("user_preferences" in p for _, p in science.llm.prompts)


def test_each_preference_text_gets_its_own_episode_and_the_default_is_separate(svc):
    a, b = podcast.variant_key("big-tech", "focus on cost"), podcast.variant_key("big-tech", "focus on risk")
    assert len({a, b, "big-tech"}) == 3 and a == podcast.variant_key("big-tech", "Focus on  cost".replace("  ", " "))
    podcast.request(svc, "tech", "2026-10-16", "big-tech", "focus on cost")
    assert podcast.request(svc, "tech", "2026-10-16", "big-tech")["status"] == "processing"  # default still startable
    assert len(svc.dispatcher.tasks) == 2


def test_blank_preferences_raise_and_start_nothing(svc):
    with pytest.raises(TldrError):
        podcast.request(svc, "tech", "2026-10-16", "big-tech", "   ")
    assert svc.dispatcher.tasks == [] and podcast.clean_preferences(None) is None
    assert podcast.clean_preferences("a\x00b\n\n c") == "a b c"


def test_chunks_synthesized_concurrently_are_still_joined_in_script_order():
    import threading
    import time as _t

    class Slow(TTS):
        """Earlier chunks take longer, so they finish last: order must come from the script, not from completion."""
        def __init__(self):
            super().__init__()
            self.lock, self.active, self.peak = threading.Lock(), 0, 0

        def synthesize(self, turns, voices):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            _t.sleep(0.15 if turns[0].text.startswith("T0") else 0.01)
            with self.lock:
                self.active -= 1
            chars = sum(len(t.text) for t in turns)
            n = int(turns[0].text[1:3])  # clip value identifies its chunk
            pcm = n.to_bytes(2, "little", signed=True) * int(SAMPLE_RATE * chars / audio.CHARS_PER_SEC)
            return Speech(wav_from_pcm(pcm), len(pcm) / 2 / SAMPLE_RATE, [(len(turns), chars / audio.CHARS_PER_SEC)])

    turns_ = [Turn(speaker="Maya" if i % 2 == 0 else "Theo", text=f"T{i:02d}" + "x" * 397) for i in range(8)]  # 400 chars each
    tts = Slow()
    out = audio.synthesize_all(tts, turns_, {"Maya": "Kore", "Theo": "Puck"}, chunk_chars=800, rid="r", concurrency=4)
    assert tts.peak > 1  # really ran in parallel
    assert len(out.parts) == 4 and out.tts_calls == 4 and out.chunk_chars == [800] * 4
    pcm = audio.pcm_from_audio(out.wav)
    first_samples = [int.from_bytes(pcm[k:k + 2], "little", signed=True) for k in range(0, len(pcm), 2)]
    firsts = [v for i, v in enumerate(first_samples) if i == 0 or v != first_samples[i - 1]]
    assert firsts == [0, 2, 4, 6]  # chunks start with turns 0, 2, 4, 6, in script order


class _LockDb:
    """Minimal stand-in for a Firestore collection holding one lock doc (create is atomic, update is version-checked)."""

    def __init__(self, at=None):
        self.at, self.version = at, 1

    def collection(self, _):
        return SimpleNamespace(document=lambda _id: self)

    def write_option(self, last_update_time):
        return last_update_time

    def create(self, data):
        from google.api_core.exceptions import AlreadyExists

        if self.at is not None:
            raise AlreadyExists("exists")
        self.at = data["at"]

    def get(self):
        return SimpleNamespace(to_dict=lambda: {"at": self.at}, update_time=self.version)

    def update(self, data, option):
        if option != self.version:
            raise ValueError("precondition failed")
        self.at, self.version = data["at"], self.version + 1


def test_claim_is_won_by_exactly_one_caller_and_never_blocks():
    svc = SimpleNamespace(db=_LockDb())
    assert podcast._claim(svc, "tech", "2026-10-16", "s") is True
    assert podcast._claim(svc, "tech", "2026-10-16", "s") is False


def test_claim_takes_over_a_stale_lock_once():
    svc = SimpleNamespace(db=_LockDb(at=podcast.now() - podcast.STALE * 2))
    assert podcast._claim(svc, "tech", "2026-10-16", "s") is True
    assert podcast._claim(svc, "tech", "2026-10-16", "s") is False
