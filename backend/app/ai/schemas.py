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
    text: str = Field(min_length=1, max_length=700, description="Exactly what is said; no stage directions")
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
