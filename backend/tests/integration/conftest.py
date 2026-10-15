"""Integration tests run against the Firebase Auth and Firestore emulators.

Start them first:  npx firebase-tools emulators:start --only auth,firestore,storage --project demo-knowly
Gemini text, Gemini TTS and Cloud Storage are replaced by deterministic fakes (tests/fakes.py).
"""

from __future__ import annotations

import os

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
os.environ["ENV"] = "test"
os.environ["GCP_PROJECT"] = "demo-knowly"
os.environ["LOCAL_MEDIA_SIGNING_KEY"] = "test-signing-key"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.dispatch import SyncDispatcher  # noqa: E402
from app.infra.firebase import firestore_client  # noqa: E402
from app.services import Services, set_services  # noqa: E402
from tests.fakes import FakeEmbedder, FakeLLM, FakeSecrets, FakeTTS, InMemoryBlobs, OfflineHttp  # noqa: E402

FS = f"http://{os.environ['FIRESTORE_EMULATOR_HOST']}"
AUTH = f"http://{os.environ['FIREBASE_AUTH_EMULATOR_HOST']}"
PROJECT = "demo-knowly"

ARTICLE_HTML = "<html><body><article><h1>Vector search</h1>" + "".join(
    f"<p>Paragraph {i}: Firestore stores embeddings in vector fields and answers nearest-neighbour queries; "
    "an index is required and dimensions are limited.</p>" for i in range(15)
) + "</article></body></html>"


def _emulators_up() -> bool:
    try:
        httpx.get(f"{FS}/", timeout=2)
        httpx.get(f"{AUTH}/", timeout=2)
        return True
    except httpx.HTTPError:
        return False


def pytest_collection_modifyitems(config, items):
    if not _emulators_up():
        skip = pytest.mark.skip(reason="Firebase emulators are not running")
        for item in items:
            if "integration" in str(item.fspath):
                item.add_marker(skip)


@pytest.fixture
def svc():
    httpx.delete(f"{FS}/emulator/v1/projects/{PROJECT}/databases/(default)/documents", timeout=10)
    httpx.delete(f"{AUTH}/emulator/v1/projects/{PROJECT}/accounts", timeout=10)
    get_settings.cache_clear()
    s = Services(
        settings=get_settings(),
        db=firestore_client(),
        blobs=InMemoryBlobs(),
        llm=FakeLLM(),
        embedder=FakeEmbedder(),
        tts=FakeTTS(),
        dispatcher=SyncDispatcher(),
        secrets=FakeSecrets(),
        http=OfflineHttp({"https://firebase.google.com/docs/firestore/vector-search": ARTICLE_HTML}),
        notifier=None,
    )
    set_services(s)
    yield s
    set_services(None)


@pytest.fixture
def client(svc):
    from app.main import create_app

    return TestClient(create_app())


def sign_up(anonymous: bool = False, email: str | None = None) -> tuple[str, str, str]:
    body: dict = {"returnSecureToken": True}
    if not anonymous:
        body.update({"email": email or "asha@example.com", "password": "password123"})
    r = httpx.post(f"{AUTH}/identitytoolkit.googleapis.com/v1/accounts:signUp?key=fake-key", json=body, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["localId"], data["idToken"], data["refreshToken"]


class User:
    def __init__(self, client: TestClient, anonymous: bool = True, email: str | None = None) -> None:
        self.uid, self.token, self.refresh_token = sign_up(anonymous, email)
        self.client = client

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **kw):
        return self.client.get(f"/api{path}", headers=self.h, **kw)

    def post(self, path: str, json=None, **kw):
        return self.client.post(f"/api{path}", headers=self.h, json=json, **kw)

    def put(self, path: str, json=None):
        return self.client.put(f"/api{path}", headers=self.h, json=json)

    def patch(self, path: str, json=None):
        return self.client.patch(f"/api{path}", headers=self.h, json=json)

    def delete(self, path: str):
        return self.client.delete(f"/api{path}", headers=self.h)

    def onboard(self, project: str, goals: list[str], mutes: list[str] | None = None, levels=None, length: int = 10):
        assert self.post("/me/bootstrap", {"timezone": "Asia/Kolkata", "mode": "sample"}).status_code == 200
        r = self.put("/profile", {"project": {"text": project}, "goals": goals, "muteTopics": mutes or []})
        assert r.status_code == 200, r.text
        if levels:
            assert self.post("/profile/levelcheck", {"ratings": [{"name": n, "level": lv} for n, lv in levels]}).status_code == 200
        prefs = {"lengthMin": length, "style": "twoHost", "speed": 1.0,
                 "schedule": {"mode": "daily", "time": "07:00", "days": [0, 1, 2, 3, 4, 5, 6]},
                 "notify": "none", "wildcardOn": True, "endQuizOn": True}
        assert self.put("/prefs", {"prefs": prefs, "onboarded": True}).status_code == 200


@pytest.fixture
def asha(client):
    u = User(client)
    u.onboard("RAG app on Firestore vector search and Cloud Run with FastAPI", ["vector search", "LLM evals"],
              levels=[("vector search", "heard"), ("cloud run", "use")])
    return u


ASHA_PROJECT = "RAG app on Firestore vector search and Cloud Run with FastAPI"
