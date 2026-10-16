"""How background work is started.

cloud:  run_briefing -> Pub/Sub topic `run-briefing` (push to /internal/pubsub/run-briefing,
        which starts the Cloud Run Job or runs the stages);
        per-segment work -> Cloud Tasks HTTP tasks to /internal/tasks/<name> with OIDC.
inline: a local thread pool that calls the same handlers. Development and tests only.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from app.config import Settings
from app.logs import Timer, correlation_id, exc_fields, get_logger, log, uid_hash

logger = get_logger(__name__)

TASK_NAMES = ("segment-audio", "gmail-debounced", "deeper-segment", "tldr-podcast")


class Dispatcher(Protocol):
    def run_briefing(self, uid: str, briefing_id: str) -> None: ...
    def task(self, name: str, payload: dict[str, Any], *, delay_seconds: int = 0, dedupe_key: str | None = None) -> None: ...


class InlineDispatcher:
    def __init__(self, max_workers: int = 4) -> None:
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="knowly-inline")
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    def run_briefing(self, uid: str, briefing_id: str) -> None:
        from app.tasks import handle_run_briefing

        payload = {"uid": uid, "briefingId": briefing_id}
        log(logger, logging.INFO, "task_submitted", task="run-briefing", mode="inline", **payload)
        self.pool.submit(_safe, handle_run_briefing, payload, "run-briefing", correlation_id.get())

    def task(self, name: str, payload: dict[str, Any], *, delay_seconds: int = 0, dedupe_key: str | None = None) -> None:
        from app.tasks import TASK_HANDLERS

        handler = TASK_HANDLERS[name]
        if dedupe_key:
            with self._lock:
                if dedupe_key in self._pending:
                    log(logger, logging.INFO, "task_deduplicated", task=name, dedupeKey=dedupe_key, mode="inline")
                    return
                self._pending.add(dedupe_key)
        cid = correlation_id.get()
        log(logger, logging.INFO, "task_submitted", task=name, mode="inline", delaySeconds=delay_seconds, **_ids(payload))

        def run() -> None:
            try:
                _safe(handler, payload, name, cid)
            finally:
                if dedupe_key:
                    with self._lock:
                        self._pending.discard(dedupe_key)

        if delay_seconds > 0:
            timer = threading.Timer(delay_seconds, lambda: self.pool.submit(run))
            timer.daemon = True
            timer.start()
        else:
            self.pool.submit(run)


class SyncDispatcher:
    """Runs work immediately in the caller's thread. Used by tests for determinism."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.paused: set[str] = set()

    def run_briefing(self, uid: str, briefing_id: str) -> None:
        from app.tasks import handle_run_briefing

        self.calls.append(("run-briefing", {"uid": uid, "briefingId": briefing_id}))
        log(logger, logging.DEBUG, "task_submitted", task="run-briefing", mode="sync", paused="run-briefing" in self.paused)
        if "run-briefing" not in self.paused:
            handle_run_briefing({"uid": uid, "briefingId": briefing_id})

    def task(self, name: str, payload: dict[str, Any], *, delay_seconds: int = 0, dedupe_key: str | None = None) -> None:
        from app.tasks import TASK_HANDLERS

        self.calls.append((name, payload))
        if name not in self.paused:
            TASK_HANDLERS[name](payload)


class CloudDispatcher:
    def __init__(self, settings: Settings) -> None:
        from google.cloud import pubsub_v1, tasks_v2

        self.settings = settings
        self.publisher = pubsub_v1.PublisherClient()
        self.tasks = tasks_v2.CloudTasksClient()
        self.queue = self.tasks.queue_path(settings.gcp_project, settings.tasks_location, settings.tasks_queue)

    def run_briefing(self, uid: str, briefing_id: str) -> None:
        topic = self.publisher.topic_path(self.settings.gcp_project, self.settings.pubsub_topic_run_briefing)
        data = json.dumps({"uid": uid, "briefingId": briefing_id}).encode()
        message_id = self.publisher.publish(topic, data, jobKey=f"{uid}:{briefing_id}").result(timeout=30)
        log(logger, logging.INFO, "task_submitted", task="run-briefing", mode="cloud", topic=topic, messageId=message_id,
            uid=uid, briefingId=briefing_id)

    def task(self, name: str, payload: dict[str, Any], *, delay_seconds: int = 0, dedupe_key: str | None = None) -> None:
        import datetime as dt

        from google.api_core.exceptions import AlreadyExists
        from google.cloud import tasks_v2
        from google.protobuf import timestamp_pb2

        task: dict[str, Any] = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{self.settings.internal_base_url}/internal/tasks/{name}",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
                "oidc_token": {
                    "service_account_email": self.settings.internal_invoker_sa,
                    "audience": self.settings.internal_base_url,
                },
            }
        }
        if dedupe_key:
            safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in dedupe_key)[:400]
            task["name"] = f"{self.queue}/tasks/{safe}"
        if delay_seconds > 0:
            ts = timestamp_pb2.Timestamp()
            ts.FromDatetime(dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay_seconds))
            task["schedule_time"] = ts
        try:
            self.tasks.create_task(parent=self.queue, task=task)
        except AlreadyExists:
            log(logger, logging.INFO, "task_deduplicated", task=name, dedupeKey=dedupe_key, mode="cloud")
            return
        log(logger, logging.INFO, "task_submitted", task=name, mode="cloud", delaySeconds=delay_seconds, **_ids(payload))


def _ids(payload: dict[str, Any]) -> dict[str, Any]:
    """The identifying fields of a task payload, for log lines (uid is hashed by log())."""
    out = {k: payload[k] for k in ("briefingId", "segId", "itemId") if k in payload}
    if "uid" in payload:
        out["uidHash"] = uid_hash(payload["uid"])
    return out


def _safe(fn, payload: dict[str, Any], name: str | None = None, cid: str = "-") -> None:
    # Worker threads do not inherit the request's contextvars, so carry the correlation id across.
    task = name or getattr(fn, "__name__", "task")
    token = correlation_id.set(cid if cid != "-" else f"{task}-{payload.get('briefingId', '')}".strip("-"))
    timer = Timer()
    log(logger, logging.INFO, "task_started", task=task, **_ids(payload))
    try:
        fn(payload)
        log(logger, logging.INFO, "task_finished", task=task, durationMs=timer.ms(), **_ids(payload))
    except Exception as exc:
        logger.exception("background task failed", extra={"fields": {
            "task": task, "durationMs": timer.ms(), **_ids(payload), **exc_fields(exc)}})
    finally:
        correlation_id.reset(token)


def build_dispatcher(settings: Settings) -> Dispatcher:
    if settings.dispatch_mode == "cloud":
        return CloudDispatcher(settings)
    if settings.is_cloud:
        raise RuntimeError("Inline dispatch is not allowed in cloud mode")
    return InlineDispatcher()
