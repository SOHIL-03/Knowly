"""Output contracts for every text-generation call (Architecture v3, section 9).

These are sent to the model as JSON Schema and validated again before anything is
stored. Field names follow the document.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Category = Literal["research", "tooling", "product", "funding", "policy", "tutorial", "other"]
SegmentType = Literal["project", "learning", "quickhits", "review"]


# 9.1 Item extraction (B3)
class ExtractedItem(BaseModel):
    headline: str = Field(min_length=3, max_length=300)
    blurb: str = Field(max_length=2000, description="The newsletter's own words, condensed")
    url: str | None = Field(default=None, description="Exactly as it appears in the issue, or null")
    is_sponsor: bool = False
    category: Category = "other"
    entities: list[str] = Field(default_factory=list, max_length=12)


class ExtractionResult(BaseModel):
    items: list[ExtractedItem]


class WholeEmailSummary(BaseModel):
    headline: str
    blurb: str
    category: Category = "other"
    entities: list[str] = Field(default_factory=list)


# 9.2 Segment plan (B7)
class PlannedSegment(BaseModel):
    type: SegmentType
    title: str = Field(max_length=80)
    why_chosen: str = Field(max_length=160)
    item_ids: list[str] = Field(min_length=1)
    concept_names: list[str] = Field(default_factory=list, max_length=3)
    target_seconds: int = Field(ge=20, le=600)


class SegmentPlan(BaseModel):
    segments: list[PlannedSegment] = Field(min_length=1, max_length=5)
    wildcard_item_id: str | None = None
    also_in_inbox: list[str] = Field(default_factory=list)


# B8: grounded podcast script, spoken by Gemini multi-speaker TTS (replaces the v3 source brief)
class ScriptLine(BaseModel):
    speaker: Literal["A", "B"]
    text: str = Field(min_length=1, max_length=2000, description="Exactly what is said; no stage directions. Aim for under 600 characters")
    style: str | None = Field(default=None, max_length=60, description="Delivery note, e.g. 'curious', 'calm'")
    item_ids: list[str] = Field(default_factory=list, description="Items this line draws facts from; empty only for short transitions")
    defines_term: str | None = None


class PodcastScript(BaseModel):
    title: str
    opening_question: str
    lines: list[ScriptLine] = Field(min_length=2)
    takeaways: list[str] = Field(min_length=1, max_length=3)


# 4.3 Ask: classifier step
class AskClassification(BaseModel):
    scope: Literal["segment", "earlier", "outside"]
    earlier_segment_id: str | None = None
    unsafe: bool = False


# 9.5 Quiz
class QuizQuestion(BaseModel):
    question: str
    rubric: list[str] = Field(min_length=1, max_length=5)
    hint: str
    model_answer: str
    source_item_ids: list[str] = Field(min_length=1)


class QuizGrade(BaseModel):
    grade: Literal["correct", "partly", "not_yet"]
    feedback: str
    missing_points: list[str] = Field(default_factory=list)


# 9.6 Apply
class ApplySuggestion(BaseModel):
    idea: str
    why_it_might_help: str
    check_first: str
    item_ids: list[str] = Field(min_length=1)


class ApplyResult(BaseModel):
    suggestions: list[ApplySuggestion] = Field(min_length=1, max_length=3)


# 9.7 Faithfulness
class UnsupportedClaim(BaseModel):
    quote: str
    reason: str


class FaithfulnessResult(BaseModel):
    unsupported_claims: list[UnsupportedClaim] = Field(default_factory=list)
    verdict: Literal["pass", "review", "fail"]


# Rephrase
class Rephrased(BaseModel):
    text: str
    item_ids: list[str] = Field(default_factory=list)


# Concept extraction for the learner model
class ConceptCandidates(BaseModel):
    concepts: list[str] = Field(default_factory=list, max_length=8)


# --- TLDR story podcast: coverage manifest -> script parts -> coverage check ---------------------
class CoveragePoint(BaseModel):
    topic: str = Field(max_length=100)
    priority: Literal["core", "supporting"] = "core"
    key_points: str = Field(max_length=800, description="What must be said, faithful to the article, 1 to 3 sentences")
    required_facts: list[str] = Field(default_factory=list, max_length=10,
                                      description="Exact figures, dates, names, quantities, comparisons, each a short statement")
    caveat: str | None = Field(default=None, max_length=300)


class CoverageManifest(BaseModel):
    points: list[CoveragePoint] = Field(min_length=1, max_length=40)


class ScriptPart(BaseModel):
    """One generated part of the episode. Only the last part carries the title and takeaways."""
    title: str | None = Field(default=None, max_length=120)
    lines: list[ScriptLine] = Field(min_length=2)
    takeaways: list[str] = Field(default_factory=list, max_length=8)


class MissingCoverage(BaseModel):
    id: str
    facts: list[str] = Field(default_factory=list, description="Required facts or key points the script does not state")


class UnsupportedLine(BaseModel):
    line: int
    reason: str


class CoverageReport(BaseModel):
    missing: list[MissingCoverage] = Field(default_factory=list)
    unsupported: list[UnsupportedLine] = Field(default_factory=list)


# --- podcast learning: concepts, Q&A, assessments (app/learning) ---------------------------------------------------------

class ExtractedConcept(BaseModel):
    name: str = Field(min_length=2, max_length=80, description="Canonical, common name of the concept, singular")
    domain: str | None = Field(default=None, max_length=40, description="Field that disambiguates a name with several meanings, else null")
    description: str = Field(min_length=5, max_length=300)
    importance: Literal["major", "supporting", "mention"]
    prerequisites: list[str] = Field(default_factory=list, max_length=4, description="Names of other listed concepts needed first")
    lines: list[int] = Field(min_length=1, max_length=6, description="Numbers of the script lines that discuss the concept")


class PodcastConcepts(BaseModel):
    concepts: list[ExtractedConcept] = Field(max_length=16)


class PodcastAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=2500, description="Plain spoken-style prose, no markdown")
    grounded: bool = Field(description="True when the podcast or its source material supports the answer")
    clarifying_question: str | None = Field(default=None, max_length=300)


class AssessmentQuestion(BaseModel):
    type: Literal["mcq", "explain", "scenario", "compare"]
    prompt: str = Field(min_length=10, max_length=600)
    options: list[str] = Field(default_factory=list, max_length=5, description="Multiple choice only: 3 or 4 options")
    correct_index: int | None = Field(default=None, description="Multiple choice only: index of the single correct option")
    concept_names: list[str] = Field(min_length=1, max_length=2, description="Names exactly as listed in CONCEPTS")
    rubric: list[str] = Field(default_factory=list, max_length=6, description="Points a correct free-text answer must make")
    model_answer: str = Field(default="", max_length=800, description="Free-text questions: a short model answer; may be empty for multiple choice")


class AssessmentSet(BaseModel):
    questions: list[AssessmentQuestion] = Field(min_length=1, max_length=10)


class AnswerEvaluation(BaseModel):
    question_id: str
    correctness_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0, description="How sure the evaluator is; low when the answer is vague or ambiguous")
    feedback: str = Field(max_length=600)
    evidence: str = Field(default="", max_length=400, description="A short quote copied from the learner's answer that supports the score")
    misconception: str | None = Field(default=None, max_length=300)


class AnswerEvaluations(BaseModel):
    evaluations: list[AnswerEvaluation]
