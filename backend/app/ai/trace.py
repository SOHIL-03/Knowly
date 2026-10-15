"""Capture of the exact OpenAI responses of one briefing run, for the review workflow.

`OpenAILLM` records every completed API attempt here (raw text, parse result, validation, token
usage). Nothing is captured unless a sink was started for the current run, which the pipeline does
only for briefings made with review turned on. Recording never makes an extra API call and never
alters the response; the sink just receives what the one existing call returned.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from typing import Any

from app.logs import get_logger, log
from app.store import now

logger = get_logger(__name__)

STEP_LABELS = {
    "ExtractionResult": "Content extraction",
    "WholeEmailSummary": "Newsletter summary (whole-email fallback)",
    "SegmentPlan": "Segment plan",
    "PodcastScript": "Podcast script",
    "FaithfulnessResult": "Faithfulness check",
}
PROMPT_PREVIEW_CHARS = 1500

_sink: contextvars.ContextVar[TraceSink | None] = contextvars.ContextVar("llm_trace_sink", default=None)


class TraceSink:
    def __init__(self, persist: Callable[[list[dict[str, Any]]], None] | None = None) -> None:
        self.entries: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}
        self._persist = persist
        self._lock = threading.Lock()

    def _save(self) -> None:
        if self._persist:
            try:
                self._persist(self.entries)
            except Exception as exc:  # the preview must never break generation
                log(logger, logging.WARNING, "llm_trace_persist_failed", errorType=type(exc).__name__)

    def add(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.entries.append({"seq": len(self.entries) + 1, "at": now().isoformat(), **self.context, **entry})
            self._save()

    def annotate(self, step: str, **fields: Any) -> None:
        """Adds fields to the latest entry of this step in the current context (same segment)."""
        with self._lock:
            for e in reversed(self.entries):
                if e["step"] == step and all(e.get(k) == v for k, v in self.context.items()):
                    e.update(fields)
                    self._save()
                    return


def start(sink: TraceSink) -> contextvars.Token:
    return _sink.set(sink)


def stop(token: contextvars.Token) -> None:
    _sink.reset(token)


def active() -> TraceSink | None:
    return _sink.get()


def set_context(**context: Any) -> None:
    sink = _sink.get()
    if sink:
        sink.context = context


def record(step: str, **fields: Any) -> None:
    sink = _sink.get()
    if sink:
        sink.add({"step": step, "label": STEP_LABELS.get(step, step), **fields})


def annotate(step: str, **fields: Any) -> None:
    sink = _sink.get()
    if sink:
        sink.annotate(step, **fields)
