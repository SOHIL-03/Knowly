"""Runtime configuration.

Every value comes from the environment (or a local .env in development). Nothing
secret has a default. See backend/.env.example for the full list.
"""

from __future__ import annotations

import logging
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"  # backend/.env, whatever the working directory
# Tests set KNOWLY_DISABLE_DOTENV=1 so they never read real keys from backend/.env.
USE_DOTENV = os.environ.get("KNOWLY_DISABLE_DOTENV") != "1"

# Google client libraries read FIRESTORE_EMULATOR_HOST, FIREBASE_AUTH_EMULATOR_HOST and
# STORAGE_EMULATOR_HOST from the process environment, not from Settings, so put backend/.env there
# too. Variables already set in the real environment win.
if USE_DOTENV:
    load_dotenv(ENV_FILE, override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE if USE_DOTENV else None, env_file_encoding="utf-8", extra="ignore")

    # "local" runs against the Firebase emulators with the in-process dispatcher.
    # "cloud" requires real Google Cloud resources and refuses dev-only shortcuts.
    env: Literal["local", "cloud", "test"] = "local"

    gcp_project: str = Field(default="demo-knowly", description="Google Cloud / Firebase project id")
    gcp_region: str = "us-central1"

    # --- Gemini (text intelligence) -------------------------------------------------
    # API keys for the Gemini Developer API, tried in order 1 -> 10 on every request; the next key
    # is used only when the previous one is out of quota (app/ai/gemini_keys.py). Empty slots are
    # skipped. GEMINI_API_KEY is the single-key fallback used when no numbered key is set.
    # Alternatively Vertex AI with ADC (GEMINI_USE_VERTEX=true; no keys, no failover).
    gemini_api_key_1: str = Field(default="", repr=False)
    gemini_api_key_2: str = Field(default="", repr=False)
    gemini_api_key_3: str = Field(default="", repr=False)
    gemini_api_key_4: str = Field(default="", repr=False)
    gemini_api_key_5: str = Field(default="", repr=False)
    gemini_api_key_6: str = Field(default="", repr=False)
    gemini_api_key_7: str = Field(default="", repr=False)
    gemini_api_key_8: str = Field(default="", repr=False)
    gemini_api_key_9: str = Field(default="", repr=False)
    gemini_api_key_10: str = Field(default="", repr=False)
    gemini_api_key: str = Field(default="", repr=False)
    gemini_use_vertex: bool = False
    gemini_vertex_location: str = "global"
    # Model ids verified on ai.google.dev/gemini-api/docs/models on 2026-10-15.
    gemini_flash_model: str = "gemini-3.8-flash"
    # Pro-class work (plans, scripts, Apply) also uses Flash by product decision.
    gemini_pro_model: str = "gemini-3.8-flash"
    gemini_embed_model: str = "gemini-embedding-001"
    # Firestore vector fields support at most 2048 dimensions.
    embed_dim: int = 768

    # --- Storage --------------------------------------------------------------------
    storage_bucket: str = "demo-knowly.appspot.com"
    signed_url_ttl_seconds: int = 900
    # HMAC key for local media URLs (emulator cannot sign GCS URLs). Required outside "cloud".
    local_media_signing_key: str = ""
    public_api_base_url: str = ""  # empty = relative media links (works from any device via the dev proxy)

    # --- Async execution ------------------------------------------------------------
    # "inline": local thread pool (development only).
    # "cloud": Pub/Sub + Cloud Tasks + Cloud Run Jobs.
    dispatch_mode: Literal["inline", "cloud"] = "inline"
    pubsub_topic_run_briefing: str = "run-briefing"
    pubsub_topic_gmail_events: str = "gmail-events"
    tasks_queue: str = "segment-audio"
    tasks_location: str = "us-central1"
    # Service URL the Cloud Tasks / Pub/Sub push requests target, and the identity they sign as.
    internal_base_url: str = ""
    internal_invoker_sa: str = ""
    # When set, Pub/Sub run-briefing pushes start this Cloud Run Job instead of running inline.
    briefing_job_name: str = ""

    # --- Audio: Gemini text-to-speech (ai.google.dev/gemini-api/docs/speech-generation) ----
    # Podcast segments use multi-speaker TTS (at most 2 prebuilt voices); spoken answers to the
    # listener's questions use single-speaker TTS. TTS uses the Gemini API (GEMINI_API_KEY).
    gemini_tts_model: str = "gemini-3.8-flash-tts"
    tts_host_a_name: str = "Maya"
    tts_host_a_voice: str = "Kore"  # Firm
    tts_host_b_name: str = "Theo"
    tts_host_b_voice: str = "Puck"  # Upbeat
    tts_answer_voice: str = "Sulafat"  # Warm
    # Lines are sent in chunks of about this many characters and the WAV parts are joined.
    tts_chunk_chars: int = 2500
    daily_limit_tts: int = 120

    # --- Gmail --------------------------------------------------------------------------
    gmail_oauth_client_id: str = ""
    # Name of the Secret Manager secret holding the OAuth client secret (cloud), or the raw
    # secret for local development only.
    gmail_oauth_client_secret_name: str = ""
    gmail_oauth_client_secret: str = ""
    gmail_pubsub_topic_full: str = ""  # projects/<p>/topics/gmail-events

    # --- Product limits -------------------------------------------------------------------
    daily_limit_ask: int = 60
    daily_limit_deeper: int = 10
    daily_limit_generate: int = 6
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    max_request_bytes: int = 1_000_000

    # --- Pipeline tuning (starting values from Architecture v3 section 7) ---------------
    dedupe_cosine_threshold: float = 0.88
    concept_match_cosine_threshold: float = 0.72
    enrich_timeout_seconds: float = 8.0
    enrich_max_chars: int = 20_000

    @property
    def is_cloud(self) -> bool:
        return self.env == "cloud"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_cloud:
        missing = [
            name
            for name, value in {
                "INTERNAL_BASE_URL": s.internal_base_url,
                "INTERNAL_INVOKER_SA": s.internal_invoker_sa,
                "STORAGE_BUCKET": s.storage_bucket,
            }.items()
            if not value
        ]
        if s.dispatch_mode != "cloud":
            missing.append("DISPATCH_MODE=cloud")
        if missing:
            raise RuntimeError(f"Cloud configuration incomplete: {', '.join(missing)}")
    elif not s.local_media_signing_key:
        # Local only: sign media links with a per-process random key. Links stop working after a
        # restart, which is fine because they expire after SIGNED_URL_TTL_SECONDS anyway.
        s.local_media_signing_key = secrets.token_urlsafe(32)
        logging.getLogger(__name__).warning("LOCAL_MEDIA_SIGNING_KEY not set; using a random key for this process")
    return s
