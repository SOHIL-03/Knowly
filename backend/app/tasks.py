"""Background task handlers shared by every dispatcher (inline, Cloud Tasks, Pub/Sub, Cloud Run Job)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services import get_services


def handle_run_briefing(payload: dict[str, Any]) -> None:
    from app.pipeline.runner import run_briefing

    run_briefing(get_services(), payload["uid"], payload["briefingId"])


def handle_segment_audio(payload: dict[str, Any]) -> None:
    from app.pipeline.audio_stage import synthesize_segment

    synthesize_segment(get_services(), payload["uid"], payload["briefingId"], payload["segId"])


def handle_gmail_debounced(payload: dict[str, Any]) -> None:
    from app.gmail.watch import run_debounced_mini_briefing

    run_debounced_mini_briefing(get_services(), payload["uid"])


def handle_deeper_segment(payload: dict[str, Any]) -> None:
    from app.checkpoint import build_deeper_segment

    build_deeper_segment(get_services(), payload["uid"], payload["briefingId"], payload["segId"], payload["itemId"],
                         payload["newSegId"])


def handle_tldr_podcast(payload: dict[str, Any]) -> None:
    from app.tldr.podcast import generate

    generate(get_services(), payload["category"], payload["date"], payload["sectionId"], payload.get("preferences"),
             payload.get("uid"), payload.get("variant"))


TASK_HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "segment-audio": handle_segment_audio,
    "gmail-debounced": handle_gmail_debounced,
    "deeper-segment": handle_deeper_segment,
    "tldr-podcast": handle_tldr_podcast,
}
