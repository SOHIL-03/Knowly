"""Dependency container. Production wiring lives here; tests replace pieces with fakes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.config import Settings, get_settings

if TYPE_CHECKING:
    from google.cloud import firestore

    from app.ai.client import LLM, Embedder
    from app.audio.tts import Speaker
    from app.dispatch import Dispatcher
    from app.infra.blobs import BlobStore
    from app.infra.secrets import SecretStore


@dataclass
class Services:
    settings: Settings
    db: "firestore.Client"
    blobs: "BlobStore"
    llm: "LLM"
    embedder: "Embedder"
    tts: "Speaker"
    dispatcher: "Dispatcher"
    secrets: "SecretStore"
    http: Any = None  # optional httpx.Client override for article fetching
    notifier: Any = None

    @property
    def paths(self):
        from app.store import Paths

        return Paths(self.db)


_services: Services | None = None


def build_services(settings: Settings | None = None) -> Services:
    from app.ai.client import GeminiEmbedder, GeminiLLM
    from app.ai.gemini_keys import GeminiKeyPool
    from app.audio.tts import build_speaker
    from app.dispatch import build_dispatcher
    from app.infra.blobs import GcsBlobStore
    from app.infra.firebase import firestore_client
    from app.infra.secrets import default_secret_store
    from app.notify import FcmNotifier

    s = settings or get_settings()
    pool = GeminiKeyPool(s)  # one shared pool: every Gemini request starts at GEMINI_API_KEY_1
    llm = GeminiLLM(s, pool)
    return Services(
        settings=s,
        db=firestore_client(),
        blobs=GcsBlobStore(s),
        llm=llm,
        embedder=GeminiEmbedder(s, llm),
        tts=build_speaker(s, pool),
        dispatcher=build_dispatcher(s),
        secrets=default_secret_store(s),
        notifier=FcmNotifier(),
    )


def get_services() -> Services:
    global _services
    if _services is None:
        _services = build_services()
    return _services


def set_services(services: Services | None) -> None:
    """Test hook."""
    global _services
    _services = services
