"""HTTP flow for the TLDR section podcast: start, poll, and receive the whole assembled episode."""

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
        podcast.generate(self.svc, payload["category"], payload["date"], payload["sectionId"], payload.get("preferences"))


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
    r = c.post("/api/tldr/podcast", json={"category": "tech", "date": "2026-10-16", "section": "big-tech"})
    assert r.status_code == 202

    got = c.get("/api/tldr/podcast", params={"category": "tech", "date": "2026-10-16", "section": "big-tech"}).json()
    assert got["status"] == "ready" and got["report"]["status"] == "complete"
    assert got["audioUrl"].endswith("big-tech.wav") and len(got["lines"]) > 4

    stored = svc.blobs.data["podcasts/tldr/tech/2026-10-16/big-tech.wav"]  # what the player is served
    with wave.open(io.BytesIO(stored), "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    assert seconds == pytest.approx(got["report"]["audioSeconds"], abs=0.1) and got["durationSec"] == round(seconds)
    spoken_chars = sum(len(t.text) for call in svc.tts.calls for t in call)
    assert spoken_chars == sum(len(ln["text"]) for ln in got["lines"])  # the audio covers the entire script


@pytest.mark.parametrize("body", [
    {"category": "tech", "date": "2026-10-16", "section": "../x"},
    {"category": "tech", "date": "2026-10-16", "section": "Big Tech"},
    {"category": "tech", "date": "2026-10-16", "section": "x" * 51},
    {"category": "tech", "date": "not-a-date", "section": "big-tech"},
    {"category": "Tech!", "date": "2026-10-16", "section": "big-tech"},
])
def test_bad_input_is_rejected(client, body):
    c, _ = client
    assert c.post("/api/tldr/podcast", json=body).status_code == 422


def test_the_old_per_story_request_is_no_longer_accepted(client):
    c, _ = client
    assert c.post("/api/tldr/podcast", json={"category": "tech", "date": "2026-10-16", "story": "s1"}).status_code == 422
    assert c.get("/api/tldr/podcast", params={"category": "tech", "date": "2026-10-16", "story": "s1"}).status_code == 422


@pytest.mark.parametrize("section", ["sponsors-only", "no-such-section"])
def test_unknown_or_sponsor_only_section_is_rejected(client, section):
    c, _ = client
    r = c.post("/api/tldr/podcast", json={"category": "tech", "date": "2026-10-16", "section": section})
    assert r.status_code == 502 and "not in this issue" in r.json()["error"]["message"]


def test_the_status_of_a_section_that_was_never_requested_is_none(client):
    c, _ = client
    got = c.get("/api/tldr/podcast", params={"category": "tech", "date": "2026-10-16", "section": "quick"}).json()
    assert got["status"] == "none"


# --- personalized podcast (user_preferences) ---------------------------------------------------------------------------

PREFS = "Explain it from beginner to advanced and focus on real-world examples"
BODY = {"category": "tech", "date": "2026-10-16", "section": "big-tech"}


def test_preferences_reach_the_model_and_the_episode_is_stored_apart_from_the_default(client):
    c, svc = client
    r = c.post("/api/tldr/podcast", json=BODY | {"user_preferences": PREFS})
    assert r.status_code == 202
    variant = r.json()["variant"]
    assert variant.startswith("p")

    parts = [p for s, p in svc.llm.prompts if s == "ScriptPart"]
    assert parts and all(f"<user_preferences>\n{PREFS}\n</user_preferences>" in p for p in parts)

    got = c.get("/api/tldr/podcast", params={k: v for k, v in BODY.items()} | {"variant": variant}).json()
    assert got["status"] == "ready" and got["report"]["status"] == "complete"
    assert got["audioUrl"].endswith(f"big-tech.{variant}.wav")
    # the shared default podcast of the section is untouched
    default = c.get("/api/tldr/podcast", params=BODY).json()
    assert default["status"] == "none"


def test_no_preferences_keeps_the_default_flow_and_prompt(client):
    c, svc = client
    r = c.post("/api/tldr/podcast", json=BODY | {"user_preferences": None})
    assert r.status_code == 202 and "variant" not in r.json()
    assert not any("user_preferences" in p for _, p in svc.llm.prompts)
    assert c.get("/api/tldr/podcast", params=BODY).json()["status"] == "ready"


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_blank_preferences_are_rejected(client, text):
    c, svc = client
    assert c.post("/api/tldr/podcast", json=BODY | {"user_preferences": text}).status_code == 422
    assert svc.llm.prompts == []


def test_overlong_preferences_are_rejected(client):
    c, _ = client
    assert c.post("/api/tldr/podcast", json=BODY | {"user_preferences": "x" * 1001}).status_code == 422


def test_bad_variant_is_rejected(client):
    c, _ = client
    assert c.get("/api/tldr/podcast", params=BODY | {"variant": "../x"}).status_code == 422
