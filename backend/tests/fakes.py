"""Deterministic test doubles. They read the real prompt content, so the pipeline code that builds
prompts and validates outputs is exercised. Used only by tests; never wired into the app."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from app.ai import prompts
from app.ai.client import INJECTION_GUARD, AiOutputInvalid
from app.ai.schemas import (
    AnswerEvaluations,
    ApplyResult,
    AskClassification,
    AssessmentSet,
    ConceptCandidates,
    ExtractionResult,
    FaithfulnessResult,
    PodcastAnswer,
    PodcastConcepts,
    PodcastScript,
    QuizGrade,
    QuizQuestion,
    Rephrased,
    SegmentPlan,
    WholeEmailSummary,
)
from app.audio.tts import Speech, Turn, pcm_seconds, wav_from_pcm

STOP = set("the a an and or of to in for on with is are be by as at it this that from can your you we our".split())
DIM = 64


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2]


class FakeEmbedder:
    """Hashed bag of words: texts that share words get a high cosine similarity."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.requests: list[tuple[str, int]] = []  # (task, number of texts) per call that reached the provider
        self.corrupt: str | None = None  # "wrong_dim" | "zero" | "nan": return malformed vectors

    def embed(self, texts: list[str], *, task: str = "document") -> list[list[float]]:
        from app.ai.client import AiUnavailable

        if self.fail:
            raise AiUnavailable("fake embedder failure")
        self.calls += 1
        self.requests.append((task, len(texts)))
        if self.corrupt:
            bad = {"wrong_dim": [1.0] * (DIM + 1), "zero": [0.0] * DIM, "nan": [float("nan")] * DIM}[self.corrupt]
            return [list(bad) for _ in texts]
        out = []
        for t in texts:
            v = [0.0] * DIM
            for w in words(t):
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                v[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


def _between(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.S)
    return m.group(1) if m else ""


def _json_after(text: str, marker: str) -> Any:
    idx = text.index(marker) + len(marker)
    return json.loads(text[idx:].strip())


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.invalid: set[str] = set()  # schema names that should fail validation
        self.unavailable = False
        self.quota = False  # simulate a used-up provider quota
        self.script_unsupported = False  # scripts include a claim the faithfulness check rejects
        self.open_question_extra = False  # the assessment generator also returns a non-multiple-choice question
        self.concept_plan: list[dict] | None = None  # concepts the extractor returns (default: two on lines 0 and 1)
        self.evaluation_overrides: dict[str, dict] = {}  # question id -> fields of AnswerEvaluation to force
        self.prompts: list[tuple[str, str, str]] = []  # (schema name, system, prompt) of the podcast-learning calls

    # --- structured ---------------------------------------------------------------------------
    def generate_json(self, tier: str, system: str, prompt: str, schema: type[BaseModel], *, temperature: float = 0.2,
                      max_output_tokens: int | None = None, on_result=None):
        from app.ai.client import AiUnavailable

        if self.quota:
            from app.ai.client import QUOTA_MESSAGE, AiQuotaExceeded

            raise AiQuotaExceeded(QUOTA_MESSAGE)
        if self.unavailable:
            raise AiUnavailable("fake outage")
        self.calls.append((tier, schema.__name__))
        if schema.__name__ in self.invalid:
            raise AiOutputInvalid(f"{schema.__name__} failed validation twice (fake)")
        handler = getattr(self, f"_{schema.__name__}", None)
        if handler is None:
            if schema.__name__ == "ProjectSummary":
                return schema(summary="A test project summary.")
            raise NotImplementedError(schema.__name__)
        return handler(system, prompt)

    def _ExtractionResult(self, system: str, prompt: str) -> ExtractionResult:
        text = _between(prompt, "newsletter")
        blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
        items = []
        heading: str | None = None
        for b in blocks:
            url_m = re.search(r"\((https?://[^)]+)\)", b)
            if "SPONSORED" in b:
                items.append({"headline": "Sponsor", "blurb": b[:200], "url": url_m.group(1) if url_m else None,
                              "is_sponsor": True, "category": "other", "entities": []})
                continue
            if "\n" not in b and len(b) < 90 and not url_m:
                heading = b
                continue
            if heading:
                body = re.sub(r"\n?[^\n]*\(https?://[^)]+\)\s*$", "", b).strip()
                ents = [w for w in re.findall(r"\b[A-Z][A-Za-z]+(?:\s[A-Z][a-z]+)?\b", heading + " " + body)][:4]
                items.append({"headline": heading, "blurb": body[:400], "url": url_m.group(1) if url_m else None,
                              "is_sponsor": False, "category": "tooling", "entities": ents})
                heading = None
        return ExtractionResult(items=items)

    def _WholeEmailSummary(self, system: str, prompt: str) -> WholeEmailSummary:
        text = _between(prompt, "newsletter")
        return WholeEmailSummary(headline=text.split("\n")[0][:80] or "Issue", blurb=text[:300])

    def _SegmentPlan(self, system: str, prompt: str) -> SegmentPlan:
        cands = _json_after(prompt, "Candidate stories in ranked order (id, headline, reason, category):\n")
        minutes = int(re.search(r"Requested length: (\d+) minutes", prompt).group(1))
        wildcard = re.search(r"labelled\): (\S+)", prompt).group(1)
        news = [c for c in cands if c["category"] != "review" and not c.get("wildcard")]
        review = [c for c in cands if c["category"] == "review"]
        total = minutes * 60
        segs = []
        if news[:2]:
            segs.append({"type": "project", "title": "For your project", "why_chosen": news[0]["reason"],
                         "item_ids": [c["id"] for c in news[:2]], "concept_names": [words(news[0]["headline"])[0]],
                         "target_seconds": int(total * 0.4)})
        if news[2:3]:
            segs.append({"type": "learning", "title": "Concept to learn", "why_chosen": news[2]["reason"],
                         "item_ids": [news[2]["id"]], "concept_names": [" ".join(words(news[2]["headline"])[:2])],
                         "target_seconds": int(total * 0.25)})
        quick = [c["id"] for c in news[3:7]]
        if wildcard != "none":
            quick.append(wildcard)
        if quick:
            segs.append({"type": "quickhits", "title": "Quick hits", "why_chosen": "Short items",
                         "item_ids": quick, "concept_names": [], "target_seconds": int(total * 0.3)})
        if review:
            segs.append({"type": "review", "title": "Review", "why_chosen": "Due", "item_ids": [r["id"] for r in review],
                         "concept_names": [], "target_seconds": 45})
        used = {i for s in segs for i in s["item_ids"]}
        return SegmentPlan.model_validate({"segments": segs, "wildcard_item_id": None if wildcard == "none" else wildcard,
                                           "also_in_inbox": [c["id"] for c in cands if c["id"] not in used]})

    def _PodcastScript(self, system: str, prompt: str) -> PodcastScript:
        items = json.loads(_between(prompt, "source"))
        target = int(re.search(r"about (\d+) spoken words", prompt).group(1))
        single = "One narrator" in prompt
        lines = [{"speaker": "A", "text": "What changed this week?", "style": "curious", "item_ids": []}]
        for n, it in enumerate(items):
            text = it["newsletter_note"]
            if it["link_only"]:
                text += " Only the newsletter's short note was available; the full article is linked."
            if it.get("article_excerpt"):
                text += " " + it["article_excerpt"][:200]
            if self.script_unsupported:
                text += " UNSUPPORTED claim."
            lines.append({"speaker": "A" if single or n % 2 else "B", "text": text, "style": "informative",
                          "item_ids": [it["id"]]})
        # pad to a plausible size so the size validator passes
        while sum(len(line["text"].split()) for line in lines) < min(40, target * 0.35):
            lines[1]["text"] += " " + lines[1]["text"]
        return PodcastScript.model_validate({
            "title": re.search(r"Title: (.*)", prompt).group(1), "opening_question": "What changed this week?",
            "lines": lines, "takeaways": [it["headline"] for it in items[:3]],
        })

    def _AskClassification(self, system: str, prompt: str) -> AskClassification:
        q = json.loads(prompt)["question"].lower()
        if "weather" in q:
            return AskClassification(scope="outside")
        if "bomb" in q:
            return AskClassification(scope="outside", unsafe=True)
        return AskClassification(scope="segment")

    def _QuizQuestion(self, system: str, prompt: str) -> QuizQuestion:
        sources = json.loads(_between(prompt, "source"))
        first = next(s for s in sources if "id" in s)
        key = words(first["headline"])[:2]
        return QuizQuestion(question=f"In one sentence, what is new about {first['headline']}?", rubric=key, hint="Think about the headline",
                            model_answer=first["note"][:200], source_item_ids=[first["id"]])

    def _QuizGrade(self, system: str, prompt: str) -> QuizGrade:
        data = json.loads(prompt)
        answer = data["listener_answer"].lower()
        hits = [r for r in data["rubric"] if r.lower() in answer]
        grade = "correct" if len(hits) == len(data["rubric"]) else "partly" if hits else "not_yet"
        return QuizGrade(grade=grade, feedback=data["model_answer"], missing_points=[r for r in data["rubric"] if r not in hits])

    def _ApplyResult(self, system: str, prompt: str) -> ApplyResult:
        sources = json.loads(_between(prompt, "source"))
        ids = [s["id"] for s in sources if "id" in s]
        return ApplyResult.model_validate({"suggestions": [
            {"idea": "Try it in a branch", "why_it_might_help": "Matches your stack", "check_first": "Measure first",
             "item_ids": ids[:1]},
            {"idea": "Invented", "why_it_might_help": "x", "check_first": "y", "item_ids": ["it_doesnotexist"]},
        ]})

    def _FaithfulnessResult(self, system: str, prompt: str) -> FaithfulnessResult:
        script = _between(prompt, "article")
        if "UNSUPPORTED" in script:
            return FaithfulnessResult(unsupported_claims=[{"quote": "UNSUPPORTED claim", "reason": "not in the sources"}], verdict="fail")
        return FaithfulnessResult(verdict="pass")

    def _Rephrased(self, system: str, prompt: str) -> Rephrased:
        sources = json.loads(_between(prompt, "source"))
        return Rephrased(text="Simpler: " + sources[0]["note"][:80], item_ids=[sources[0]["id"], "it_fake"])

    def _ConceptCandidates(self, system: str, prompt: str) -> ConceptCandidates:
        return ConceptCandidates(concepts=["vector search", "cold starts", "dead letter topics"])

    # --- podcast learning --------------------------------------------------------------------------------
    def _PodcastConcepts(self, system: str, prompt: str) -> PodcastConcepts:
        self.prompts.append(("PodcastConcepts", system, prompt))
        plan = self.concept_plan or [
            {"name": "vector search", "description": "Finding nearest vectors", "importance": "major", "lines": [0]},
            {"name": "embeddings", "description": "Numeric text representations", "importance": "major", "lines": [0, 1]}]
        return PodcastConcepts.model_validate({"concepts": plan})

    def _PodcastAnswer(self, system: str, prompt: str) -> PodcastAnswer:
        self.prompts.append(("PodcastAnswer", system, prompt))
        q = _between(prompt, "question").strip()
        return PodcastAnswer(answer=f"Here is an answer about: {q}", grounded="unknowable" not in q.lower())

    def _AssessmentSet(self, system: str, prompt: str) -> AssessmentSet:
        self.prompts.append(("AssessmentSet", system, prompt))
        n = int(re.search(r"Write (\d+) questions", prompt).group(1))
        names = re.findall(r"^- (.+?) \((?:major|supporting|mention)\)", prompt, re.M)
        qs = []
        for i in range(n):
            name = names[i % len(names)]
            qs.append({"type": "mcq", "prompt": f"Which statement about {name} is correct?", "options": ["Wrong idea", f"Right idea about {name}", "Other", "None"],
                       "correct_index": 1, "concept_names": [name]})
            if self.open_question_extra and i == 0:  # a free-text question the server must drop
                qs.append({"type": "explain", "prompt": f"Explain {name} in your own words.", "concept_names": [name],
                           "rubric": [f"{name} nearest"], "model_answer": "x"})
        return AssessmentSet.model_validate({"questions": qs})

    def _AnswerEvaluations(self, system: str, prompt: str) -> AnswerEvaluations:
        self.prompts.append(("AnswerEvaluations", system, prompt))
        asked = json.loads(prompt.split("QUESTIONS:\n")[1].split("\n\nLEARNER ANSWERS:")[0])
        answers = dict(re.findall(r'<answer id="(q\d+)">\n(.*?)\n</answer>', prompt, re.S))
        out = []
        for q in asked:
            text = answers.get(q["question_id"], "")
            hits = [r for r in q["rubric"] if all(w in text.lower() for w in r.lower().split())]
            score = len(hits) / max(1, len(q["rubric"]))
            ev = {"question_id": q["question_id"], "correctness_score": score,
                  "confidence": 0.9 if len(text.split()) >= 6 and "not sure" not in text.lower() else 0.3,
                  "feedback": "Feedback", "evidence": text.split(".")[0] if hits else "",
                  "misconception": None if score >= 0.5 else "Mixes up the idea"}
            out.append({**ev, **self.evaluation_overrides.get(q["question_id"], {})})
        return AnswerEvaluations.model_validate({"evaluations": out})

    # --- streaming ------------------------------------------------------------------------------
    def stream_text(self, tier: str, system: str, prompt: str, *, temperature: float = 0.3, cancel=None) -> Iterator[str]:
        self.calls.append((tier, "stream"))
        if system == prompts.ASK_GENERAL_SYSTEM:
            yield "This is a general answer."
            return
        question = prompt.rsplit("QUESTION:", 1)[1].strip().lower()
        sources = json.loads(_between(prompt, "source"))
        items = [s for s in sources if "id" in s]
        if "unknowable" in question:
            yield "NOT_"
            yield "FOUND"
            return
        if "hallucinate" in question:
            yield "It is great [it_madeup]."
            return
        first = items[0]
        yield f"{first['note'][:60]} "
        yield f"[{first['id']}]."


class FakeOpenAIClient:
    """Stands in for openai.OpenAI: answers chat.completions.create() with FakeLLM's deterministic
    output, so the real OpenAILLM (request building, JSON parsing, validation) runs in the pipeline."""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.fake = FakeLLM()
        self.requests: list[dict] = []
        self.responses: list[str] = []  # the exact message.content returned for each non-stream request
        self.fail: BaseException | None = None  # raised by every request when set
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        from types import SimpleNamespace

        self.requests.append(kw)
        if self.fail is not None:
            raise self.fail
        system, user = kw["messages"][0]["content"], kw["messages"][1]["content"]
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        if kw.get("stream") and not kw.get("response_format"):
            plain = system.removesuffix("\n\n" + INJECTION_GUARD)

            def chunks():
                for text in self.fake.stream_text("flash", plain, user):
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=None)], usage=None)

            return chunks()
        content = json_reply(self.fake, system, user)
        self.responses.append(content)
        if kw.get("stream"):  # streamed JSON (the fallback of app/ai/text_llm.py)
            def json_chunks():
                for part in split3(content):
                    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part, refusal=None), finish_reason=None)], usage=None)
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None, refusal=None), finish_reason="stop")], usage=None)
                yield SimpleNamespace(choices=[], usage=usage)

            return json_chunks()
        message = SimpleNamespace(content=content, refusal=None)
        return SimpleNamespace(model=kw["model"], usage=usage, choices=[SimpleNamespace(message=message, finish_reason="stop")])


def json_reply(fake: FakeLLM, system: str, user: str) -> str:
    """FakeLLM's answer for a structured request. The schema the request asked for is embedded in the system prompt."""
    from app.ai import schemas

    candidates = {
        cls: json.dumps(cls.model_json_schema(), separators=(",", ":"))
        for cls in vars(schemas).values()
        if isinstance(cls, type) and issubclass(cls, BaseModel) and cls is not BaseModel
    }
    schema = max((cls for cls, text in candidates.items() if text in system), key=lambda cls: len(candidates[cls]))
    return fake.generate_json("flash", system, user, schema).model_dump_json()


def split3(text: str) -> list[str]:
    n = max(1, len(text) // 3)
    return [text[:n], text[n:2 * n], text[2 * n:]]


class FakeGeminiTextClient:
    """Stands in for genai.Client on the streaming text path (client.aio.models.generate_content_stream), answering with
    FakeLLM's deterministic output split over several chunks, so the real TextLLM runs in the pipeline. With
    `hang=True` it never answers (every first-token deadline expires)."""

    def __init__(self, hang: bool = False) -> None:
        from types import SimpleNamespace

        self.fake = FakeLLM()
        self.hang = hang
        self.requests: list[dict] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content_stream=self._start))

    async def _start(self, *, model, contents, config):
        import asyncio
        from types import SimpleNamespace

        self.requests.append({"model": model, "contents": contents, "config": config})
        system = config.system_instruction
        if self.hang:
            pieces: list[str] = []
        elif config.response_mime_type == "application/json":
            pieces = split3(json_reply(self.fake, system, contents))
        else:
            pieces = list(self.fake.stream_text("flash", system.removesuffix("\n\n" + INJECTION_GUARD), contents))

        def res(text=None, finish=None):
            parts = [SimpleNamespace(text=text, thought=None)] if text else []
            return SimpleNamespace(prompt_feedback=None, usage_metadata=None if finish is None else SimpleNamespace(
                prompt_token_count=80, candidates_token_count=40, total_token_count=120),
                candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts),
                                            finish_reason=SimpleNamespace(name=finish) if finish else None)])

        async def stream():
            if self.hang:
                await asyncio.sleep(3600)
            for p in pieces:
                yield res(p)
            yield res(finish="STOP")

        return stream()


class InMemoryBlobs:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    def put_text(self, path: str, text: str, content_type: str = "text/plain") -> None:
        self.data[path] = text.encode()

    def put_bytes(self, path: str, data: bytes, content_type: str) -> None:
        self.data[path] = data

    def get_bytes(self, path: str) -> bytes | None:
        return self.data.get(path)

    def get_text(self, path: str) -> str | None:
        b = self.data.get(path)
        return b.decode() if b is not None else None

    def exists(self, path: str) -> bool:
        return path in self.data

    def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self.data if k.startswith(prefix)]
        for k in keys:
            del self.data[k]
        return len(keys)

    def gs_uri(self, path: str) -> str:
        return f"gs://test/{path}"

    def read_url(self, path: str) -> str:
        return f"http://media.test/{path}"


class FakeTTS:
    """Returns real WAV audio (silence) whose length follows the text, and records each request."""

    SECONDS_PER_CHAR = 0.004

    def __init__(self) -> None:
        self.calls: list[tuple[list[Turn], dict[str, str]]] = []
        self.fail_times = 0

    def synthesize(self, turns: list[Turn], voices: dict[str, str]) -> Speech:
        from app.ai.client import AiUnavailable

        self.calls.append((turns, dict(voices)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise AiUnavailable("fake TTS outage")
        if not 1 <= len(voices) <= 2:
            raise ValueError("bad voices")
        if len(voices) == 2:
            assert {t.speaker for t in turns} <= set(voices), "turn speaker not configured"
        frames = int(24_000 * self.SECONDS_PER_CHAR * sum(len(t.text) for t in turns))
        pcm = bytes(2 * max(frames, 2400))
        return Speech(wav_from_pcm(pcm), pcm_seconds(pcm), [(len(turns), pcm_seconds(pcm))])


class FakeSTT:
    """Gemini STT stand-in: returns the prepared text, or raises when told to. Records every call."""

    def __init__(self, text: str = "What is a vector database") -> None:
        self.text = text
        self.calls: list[tuple[int, str]] = []
        self.fail = False

    def transcribe(self, audio: bytes, mime_type: str) -> str:
        from app.ai.client import AiUnavailable

        self.calls.append((len(audio), mime_type))
        if self.fail:
            raise AiUnavailable("fake STT outage")
        return self.text


class FakeSecrets:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def put(self, name: str, value: str) -> str:
        self.data[name] = value
        return name

    def get(self, name: str) -> str | None:
        return self.data.get(name)

    def delete(self, name: str) -> None:
        self.data.pop(name, None)


class OfflineHttp:
    """httpx.Client stand-in for article fetching: every page is 'blocked' unless registered."""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = pages or {}

    def get(self, url: str):
        import httpx

        if url in self.pages:
            return httpx.Response(200, text=self.pages[url], headers={"content-type": "text/html"},
                                  request=httpx.Request("GET", url))
        return httpx.Response(403, text="blocked", request=httpx.Request("GET", url))
