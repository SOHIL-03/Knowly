"""The mastery policy: the ONLY place that decides what an answer shows and how a concept's status changes.

Everything here is a pure, deterministic function. The LLM never picks a status: it returns a score and a
confidence for a free-text answer, and `classify` turns those into an evidence status using the thresholds below.
Only assessment answers produce evidence. Playing a podcast, reading the transcript, asking a question and hearing an
AI answer never reach this module (see app/learning/chat.py, which does not import it).

Evidence statuses of one answer:  demonstrated | partial | incorrect | insufficient | not_assessed
Concept statuses:                 unknown | learning | understood | needs_review

Transitions (evidence that counts = demonstrated, partial, incorrect; insufficient and not_assessed never change a status):
  unknown      + counting evidence                       -> learning
  learning     + counting evidence                       -> learning, or understood when the two latest counting
                                                            answers are both demonstrated and came from at least
                                                            UNDERSTOOD_MIN_QUESTIONS different questions
  understood   + demonstrated / partial                  -> understood (one partial answer never invalidates it)
  understood   + incorrect                               -> needs_review (history and counts are kept)
  needs_review + incorrect / partial                     -> needs_review
  needs_review + demonstrated                            -> understood once the two latest counting answers are both demonstrated
No status ever moves back to unknown, and nothing changes with time alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EvidenceStatus = Literal["demonstrated", "partial", "incorrect", "insufficient", "not_assessed"]
MasteryStatus = Literal["unknown", "learning", "understood", "needs_review"]

DEMONSTRATED_MIN_SCORE = 0.8  # correctness_score at or above this is demonstrated understanding
PARTIAL_MIN_SCORE = 0.4  # at or above this (and below the line above) is partial; below is incorrect
MIN_CONFIDENCE = 0.6  # evaluator confidence below this is insufficient evidence whatever the score
MIN_ANSWER_WORDS = 3  # a free-text answer shorter than this is insufficient evidence
UNDERSTOOD_MIN_QUESTIONS = 2  # distinct demonstrated questions needed before a concept is understood
RECENT_WINDOW = 5  # counting statuses kept on the record
MAX_MISCONCEPTIONS = 3

QUESTIONS_MIN, QUESTIONS_MAX = 3, 8
WORDS_PER_QUESTION = 120  # multiple choice is quick: a podcast of N spoken words gets about N / this many questions


def questions_for(words: int, major_concepts: int) -> int:
    """Assessment length follows the content: short podcasts get short assessments."""
    by_length = round(words / WORDS_PER_QUESTION)
    by_concepts = 2 * major_concepts  # each major concept is tested by two different questions, which "understood" needs
    return max(QUESTIONS_MIN, min(QUESTIONS_MAX, min(by_length, by_concepts) if by_concepts else by_length))


def question_targets(concepts: list[dict], n: int) -> list[str]:
    """Which concept each of the n questions must test (names). Major concepts come first and get two questions each, which
    is what reaching "understood" needs; supporting concepts fill any remaining questions. Deterministic, not left to the model."""
    major = [c["name"] for c in concepts if c["importance"] == "major"]
    rest = [c["name"] for c in concepts if c["importance"] == "supporting"] + [c["name"] for c in concepts if c["importance"] == "mention"]
    order = [*major, *major, *rest] if major else rest
    return (order * (n // max(1, len(order)) + 1))[:n] if order else []


def classify(score: float, confidence: float, answer: str, evidence_quote: str = "", *, objective: bool = False) -> EvidenceStatus:
    """What a graded answer shows. Objective (multiple-choice) answers are exact; free text needs supporting evidence."""
    text = (answer or "").strip()
    if not text:
        return "not_assessed"
    if objective:
        return "demonstrated" if score >= 1.0 else "incorrect"
    if len(text.split()) < MIN_ANSWER_WORDS or confidence < MIN_CONFIDENCE:
        return "insufficient"
    if score >= DEMONSTRATED_MIN_SCORE:
        # Credit needs a quote that really is in the learner's answer; otherwise there is no evidence for it.
        if not evidence_quote or " ".join(evidence_quote.lower().split()) not in " ".join(text.lower().split()):
            return "insufficient"
        return "demonstrated"
    return "partial" if score >= PARTIAL_MIN_SCORE else "incorrect"


COUNTING = ("demonstrated", "partial", "incorrect")


@dataclass
class Record:
    """The parts of a mastery record the policy reads."""
    status: MasteryStatus = "unknown"
    recent: list[str] | None = None  # latest counting statuses, oldest first
    demonstrated_keys: list[str] | None = None  # distinct questions answered with demonstrated understanding


def next_status(rec: Record, evidence: EvidenceStatus, question_key: str) -> tuple[MasteryStatus, str]:
    """(new status, reason) after one more piece of evidence. `rec` is the state BEFORE this evidence."""
    if evidence not in COUNTING:
        return rec.status, f"{evidence} evidence does not change the status"
    recent = [*(rec.recent or []), evidence][-RECENT_WINDOW:]
    keys = set(rec.demonstrated_keys or [])
    if evidence == "demonstrated":
        keys.add(question_key)
    last_two_demonstrated = recent[-2:] == ["demonstrated", "demonstrated"]
    earned = last_two_demonstrated and len(keys) >= UNDERSTOOD_MIN_QUESTIONS
    if rec.status in ("unknown", "learning"):
        if earned:
            return "understood", "two consecutive demonstrated answers to different questions"
        return "learning", f"first evidence ({evidence})" if rec.status == "unknown" else f"{evidence} answer; criteria for understood not yet met"
    if rec.status == "understood":
        if evidence == "incorrect":
            return "needs_review", "clearly incorrect answer after demonstrated understanding"
        return "understood", f"{evidence} answer; earlier demonstrated understanding kept"
    # needs_review
    if earned:
        return "understood", "two consecutive demonstrated answers after review"
    return "needs_review", f"{evidence} answer; review not yet complete"


def mastery_score(demonstrated: int, partial: int, incorrect: int) -> float | None:
    """Informational share of counting evidence that was correct (partial counts half). Never decides a status."""
    total = demonstrated + partial + incorrect
    return round((demonstrated + 0.5 * partial) / total, 3) if total else None
