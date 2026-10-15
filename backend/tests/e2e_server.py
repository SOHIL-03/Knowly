"""TEST-ONLY launcher for browser end-to-end tests.

Runs the real FastAPI app, the real inline dispatcher (background threads), the real Firestore and
Auth emulators and the real Cloud Storage client against the Storage emulator. Only Gemini text and
Gemini TTS are replaced by the deterministic fakes in tests/fakes.py, because no Gemini
credentials are available in CI. Never use this to serve real users.

    python -m tests.e2e_server   (from backend/, with the emulators running)
"""

from __future__ import annotations

import os

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
os.environ.setdefault("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
os.environ.setdefault("STORAGE_EMULATOR_HOST", "http://127.0.0.1:9199")
os.environ["ENV"] = "local"
os.environ["GCP_PROJECT"] = "demo-knowly"
os.environ["STORAGE_BUCKET"] = "demo-knowly.appspot.com"
os.environ.setdefault("LOCAL_MEDIA_SIGNING_KEY", "e2e-local-key")
os.environ["PUBLIC_API_BASE_URL"] = ""

import uvicorn  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.dispatch import InlineDispatcher  # noqa: E402
from app.infra.blobs import GcsBlobStore  # noqa: E402
from app.infra.firebase import firestore_client  # noqa: E402
from app.services import Services, set_services  # noqa: E402
from tests.fakes import FakeEmbedder, FakeLLM, FakeSecrets, FakeTTS, OfflineHttp  # noqa: E402


def main() -> None:
    settings = get_settings()
    set_services(
        Services(
            settings=settings,
            db=firestore_client(),
            blobs=GcsBlobStore(settings),
            llm=FakeLLM(),
            embedder=FakeEmbedder(),
            tts=FakeTTS(),
            dispatcher=InlineDispatcher(),
            secrets=FakeSecrets(),
            http=OfflineHttp(),
        )
    )
    from app.main import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
