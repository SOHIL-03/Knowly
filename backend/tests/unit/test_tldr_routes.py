"""HTTP flow for the TLDR story podcast: start, poll, and receive the whole assembled episode."""

from __future__ import annotations

import io
import wave

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import services
from app.errors import install_error_handlers
from app.routes import tldr as tldr_routes
from app.tldr import podcast
from tests.unit.test_tldr_podcast import svc as _svc  # noqa: F401  (fixture: stub LLM, TTS, blobs, article fetch)


class InlineDispatcher:
    """Runs the queued task immediately, like the local dispatcher does in a worker thread."""

    def __init__(self, svc):
        self.svc = svc

    def task(self, name, payload, **kw):
        assert name == "tldr-podcast"
        podcast.generate(self.svc, payload["category"], payload["date"], payload["storyId"])


@pytest.fixture
def client(_svc):  # noqa: F811
    _svc.dispatcher = InlineDispatcher(_svc)
    app = FastAPI()
    install_error_handlers(app)
    app.dependency_overrides[services] = lambda: _svc
    app.include_router(tldr_routes.router, prefix="/api")
    return TestClient(app), _svc


def test_post_then_get_returns_the_whole_episode(client):
    c, svc = client
    r = c.post("/api/tldr/podcast", json={"category": "tech", "date": "2026-10-16", "story": "s1"})
    assert r.status_code == 202

    got = c.get("/api/tldr/podcast", params={"category": "tech", "date": "2026-10-16", "story": "s1"}).json()
    assert got["status"] == "ready" and got["report"]["status"] == "complete"
    assert got["audioUrl"].endswith("s1.wav") and len(got["lines"]) > 4

    stored = svc.blobs.data["podcasts/tldr/tech/2026-10-16/s1.wav"]  # what the player is served
    with wave.open(io.BytesIO(stored), "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    assert seconds == pytest.approx(got["report"]["audioSeconds"], abs=0.1) and got["durationSec"] == round(seconds)
    spoken_chars = sum(len(t.text) for call in svc.tts.calls for t in call)
    assert spoken_chars == sum(len(ln["text"]) for ln in got["lines"])  # the audio covers the entire script


@pytest.mark.parametrize("body", [
    {"category": "tech", "date": "2026-10-16", "story": "../x"},
    {"category": "tech", "date": "not-a-date", "story": "s1"},
    {"category": "Tech!", "date": "2026-10-16", "story": "s1"},
])
def test_bad_input_is_rejected(client, body):
    c, _ = client
    assert c.post("/api/tldr/podcast", json=body).status_code == 422


def test_sponsored_story_is_not_found_or_rejected(client):
    c, _ = client
    r = c.post("/api/tldr/podcast", json={"category": "tech", "date": "2026-10-16", "story": "s2"})
    assert r.status_code == 502 and "not in this issue" in r.json()["error"]["message"]
