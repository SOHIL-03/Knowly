"""Prompt text for every Gemini call. Rules mirror Architecture v3 section 9."""

from __future__ import annotations

import json
from typing import Any

EXTRACT_SYSTEM = (
    "You split a technology newsletter issue into its individual story items. "
    "Keep the order of appearance. Copy URLs exactly as they appear in the issue; never invent, "
    "complete or guess a URL - use null when a story has no link. Mark sponsor, advertisement and "
    "'presented by' blocks with is_sponsor=true. The blurb must stay close to the newsletter's own words."
)


def extract_prompt(sender_name: str, subject: str, text: str) -> str:
    return (
        f"Newsletter: {sender_name}\nSubject: {subject}\n\n"
        f"<newsletter>\n{text}\n</newsletter>\n\n"
        "Return every story item as JSON."
    )


WHOLE_EMAIL_SYSTEM = (
    "The newsletter could not be split into items. Summarise the whole issue as one item using only "
    "facts in the text. Do not add anything that is not stated."
)

PLAN_SYSTEM = (
    "You plan a personal spoken briefing made of 3 to 5 segments. Use only the candidate story ids you "
    "are given. Segment types: project (stories that touch the user's project or stack), learning (one "
    "concept tied to a goal topic), quickhits (short items worth knowing, must include the wildcard if "
    "one is given), review (only if review material is provided). Titles are about the user, not the "
    "newsletter. The sum of target_seconds must be within 10% of the requested length. Put candidates "
    "that do not fit into also_in_inbox."
)


def plan_prompt(
    candidates: list[dict[str, Any]],
    length_minutes: int,
    level_summary: str,
    wildcard_id: str | None,
    review_available: bool,
) -> str:
    return (
        f"Requested length: {length_minutes} minutes ({length_minutes * 60} seconds).\n"
        f"Listener level summary: {level_summary}\n"
        f"Wildcard story id (place it in quickhits, labelled): {wildcard_id or 'none'}\n"
        f"Review segment material available: {'yes - include one review segment of 30-60 seconds' if review_available else 'no - do not plan a review segment'}\n\n"
        "Candidate stories in ranked order (id, headline, reason, category):\n"
        + json.dumps(candidates, indent=1)
    )


SCRIPT_SYSTEM = (
    "You write the spoken script for one segment of a personal audio briefing. A text-to-speech model "
    "reads every line word for word, so write only what is said: no stage directions, no markdown, no "
    "sound effects in the text. Put delivery hints (for example 'curious', 'calm') in the style field. "
    "Use only facts in the SOURCES; no opinions presented as fact and no outside facts. Every line that "
    "states a fact lists the ids of the items it comes from; lines without ids are short transitions or "
    "questions with no facts. Cover every item in the SOURCES at least once. The first line asks the "
    "opening question. If an item is marked link_only, say that only the newsletter's short note was "
    "available and that the full article is linked. Define a term with a simple analogy only when the "
    "listener's mastery of it is below 0.35; between 0.35 and 0.7 use it with a one-line reminder; above "
    "0.7 skip the basics. One idea per segment."
)


def script_prompt(
    seg_type: str,
    title: str,
    why: str,
    items: list[dict[str, Any]],
    mastery: dict[str, float],
    style: str,
    target_seconds: int,
    project: str,
    hosts: dict[str, str],
) -> str:
    words = max(60, int(target_seconds * 2.5))  # about 150 spoken words per minute
    rules = {
        "project": "End each story with one line on why it matters to the listener's stated project.",
        "learning": "Open with a question. One concept only.",
        "quickhits": "About 20 seconds per item. Say clearly which item is the wildcard.",
        "review": "Ask one question first and pause for the listener, then explain (retrieval before re-explaining).",
        "deeper": "Go deeper into the full article text provided.",
    }.get(seg_type, "")
    if style == "brief":
        voice = f"One narrator, {hosts['A']}: every line uses speaker A."
    else:
        voice = (
            f"Two hosts in conversation: speaker A is {hosts['A']}, speaker B is {hosts['B']}. Alternate "
            "naturally; they may address each other by name; one asks, the other explains."
        )
    return (
        f"Segment type: {seg_type}. {rules}\n{voice}\nTitle: {title}\nWhy chosen: {why}\n"
        f"Listener project: {project or 'not given'}\n"
        f"Concept mastery (0-1): {json.dumps(mastery)}\n"
        f"Length: about {words} spoken words in total.\n\n"
        "<source>\n" + json.dumps(items, indent=1) + "\n</source>"
    )


ASK_CLASSIFY_SYSTEM = (
    "Classify a listener's question about their briefing. scope=segment if it is about the current "
    "segment, earlier if it is about another listed segment (give its id), outside if neither. Set "
    "unsafe=true for requests that are harmful or abusive."
)

ASK_SYSTEM = (
    "You answer only from SOURCES. Cite every factual sentence with the item id in square brackets, "
    "for example [it_88]. If SOURCES do not contain the answer, reply with exactly NOT_FOUND and nothing "
    "else. Keep answers to 2 to 6 short sentences. Never reveal quiz rubrics or answers."
)
NOT_FOUND = "NOT_FOUND"
NOT_IN_SOURCES = "That isn't covered in your newsletters."

ASK_GENERAL_SYSTEM = (
    "The listener asked something their newsletters do not cover. Give a brief, careful general "
    "answer in 2 to 4 sentences. Do not cite newsletters. Say when you are unsure."
)

GENERAL_LABEL = "General knowledge, not from your newsletters: "

QUIZ_SYSTEM = (
    "Write one recall question (not multiple choice) about the concept, answerable from the SOURCES. "
    "Give a rubric of 1 to 4 key points, a hint that does not give the answer away, a model answer of "
    "one or two sentences, and the ids of the items the answer comes from."
)

GRADE_SYSTEM = (
    "Grade the listener's answer against the rubric. Grade the idea, not the wording: correct if all key "
    "points are present, partly if some are, not_yet if none or the answer is 'I don't know'. Feedback is "
    "one or two sentences with the right answer and where it was said."
)

APPLY_SYSTEM = (
    "Suggest one to three concrete ways the listener could use these stories in their project. Each "
    "suggestion cites its source item ids. Mark uncertainty explicitly in check_first."
)

REPHRASE_SYSTEM = (
    "Re-explain the passage at the requested level using only facts in the SOURCES. Keep it under 120 "
    "words. Return the ids of the items you used."
)

FAITHFULNESS_SYSTEM = (
    "Compare a podcast script with the SOURCES it was written from, before it is turned into audio. List "
    "claims in the script that the SOURCES do not support (questions, small talk and transitions are "
    "fine). verdict=pass when there are none, review for minor unsupported details, fail for factual "
    "claims not in the SOURCES."
)

ANSWER_VOICE_STYLE = "clear, warm and unhurried, like a patient tutor"

CONCEPTS_SYSTEM = (
    "List the technical concepts (2 to 4 words each, lowercase, generic, e.g. 'vector index', "
    "'llm evals') a learner would need to understand these stories. At most 8."
)
