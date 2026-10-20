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

    # DEBUG adds per-call detail; TRACE also lets HTTP client libraries log (very noisy).
    log_level: str = "INFO"

    gcp_project: str = Field(default="demo-knowly", description="Google Cloud / Firebase project id")
    gcp_region: str = "us-central1"

    # --- Text generation (app/ai/text_llm.py) ---------------------------------------
    # true: every text call (parse, plans, scripts, Ask, quiz, grading, faithfulness) streams from Gemini
    # (GEMINI_TEXT_MODEL), one attempt per Gemini key, each with a first-token deadline, then OpenAI as the
    # final fallback. false: OpenAI only, without streaming for structured output (the previous behaviour).
    llm_streaming_enabled: bool = True
    # Model id from the previous Gemini text setup (verified on ai.google.dev on 2026-10-15). Used for every tier.
    gemini_text_model: str = "gemini-3.8-flash"
    # Thinking delays the first text token, so it is kept minimal by default. Empty = the model's default.
    gemini_text_thinking_level: str = "minimal"
    # A Gemini attempt that has not produced its first text within this many seconds (connection included) is
    # cancelled and the next key is tried. The OpenAI fallback has no first-token deadline.
    gemini_first_token_timeout_seconds: float = 3.0
    # Whether the OpenAI fallback streams structured (JSON) output too. Free-text answers always stream.
    openai_streaming_enabled: bool = True

    # --- OpenAI (final text fallback) -----------------------------------------------
    # One backend-only key; used when every Gemini key failed, or for all text when LLM_STREAMING_ENABLED=false.
    openai_api_key: str = Field(default="", repr=False)
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 60.0
    # Retries after the first attempt for 429 / 5xx / network errors (bounded, with backoff).
    openai_max_retries: int = 2
    # Review workflow: capture each OpenAI response for the preview and hold audio until the user
    # clicks Generate Audio. A briefing can also opt in or out per request.
    review_before_audio: bool = False

    # --- Gemini (text generation, embeddings, text-to-speech, speech-to-text) --------
    # API keys for the Gemini Developer API, tried in order 1 -> 10 on every request; the next key
    # is used only when the previous one is out of quota (app/ai/gemini_keys.py). Text generation also
    # moves on when a key is slow to start answering (see above). Empty slots are
    # skipped. GEMINI_API_KEY is the single-key fallback used when no numbered key is set.
    # Alternatively Vertex AI with ADC for embeddings (GEMINI_USE_VERTEX=true); TTS always needs keys.
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
    # Model id verified on ai.google.dev/gemini-api/docs/models on 2026-10-15.
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
    # A Gemini request that has not answered after this long counts as failed, so a hung key moves on to the next key
    # instead of stalling forever. A 2,500-character speech chunk normally takes well under a minute.
    gemini_timeout_seconds: float = 120.0
    gemini_tts_model: str = "gemini-3.8-flash-tts"
    # Speech-to-text for the podcast page's voice questions (Gemini audio understanding). Answers stay on OpenAI.
    gemini_stt_model: str = "gemini-3.5-transcribe"  # dedicated transcription model; general models invent text from silence
    stt_max_bytes: int = 900_000  # a recording must fit the request-size limit (max_request_bytes)
    daily_limit_stt: int = 60
    tts_host_a_name: str = "Maya"
    tts_host_a_voice: str = "Kore"  # Firm
    tts_host_b_name: str = "Theo"
    tts_host_b_voice: str = "Puck"  # Upbeat
    tts_answer_voice: str = "Sulafat"  # Warm
    # Lines are sent in chunks of about this many characters and the WAV parts are joined.
    tts_chunk_chars: int = 2500
    # Podcast audio chunks synthesized at the same time (a long episode is many ~1 minute Gemini calls). 1 = one after another.
    tts_concurrency: int = 3
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
    # TLDR story podcast: how many times missing or unsupported script parts are regenerated.
    tldr_max_repairs: int = 2
    # TLDR section podcast: at most this many stories of one section are read and discussed.
    tldr_section_max_stories: int = 8

    # --- Semantic recommendations (app/recommend). Deterministic ranking from embeddings only: no LLM
    # call scores, classifies, reranks or explains a story. Off = the original B6 scorer.
    rec_enabled: bool = True
    rec_ranking_version: str = "semantic-v1"
    rec_final_count: int = 12  # stories recommended per briefing (a wildcard is added on top)
    # Initial weight of each declared interest facet type. Feedback adjusts these within the bounds below.
    rec_weight_project: float = 1.0
    rec_weight_goal: float = 0.85
    rec_weight_stack: float = 0.8
    rec_weight_interest: float = 0.7
    rec_weight_expertise: float = 0.5
    rec_weight_min: float = 0.1
    rec_weight_max: float = 1.5
    rec_secondary_min_ratio: float = 0.85  # other facets within this share of the best score are kept as matches
    rec_secondary_max: int = 2
    rec_novelty_weight: float = 0.15  # share of the final score; capped at 0.3 so novelty never outweighs relevance
    rec_heard_penalty: float = 0.25  # same story cluster in the last 7 briefings (normalized score units)
    rec_mmr_lambda: float = 0.7  # 1.0 = pure relevance, lower = more diversity
    rec_duplicate_threshold: float = 0.93  # story-to-story cosine at which two stories count as the same story
    # Mutes. A story's cosine to a muted topic is measured as a gap above the median cosine of today's candidates
    # to that topic, because absolute cosines differ by topic phrasing and by embedding model (for
    # gemini-embedding-001 at 768 dimensions, on-topic stories sat about +0.10 to +0.18 above the median and
    # unrelated ones at most +0.07 in recsys_eval; recalibrate when the model or EMBED_DIM changes, see README).
    rec_mute_exclude_gap: float = 0.11  # at or above: the story is mainly about the muted topic, so it is dropped
    rec_mute_penalty_gap: float = 0.06  # between this and the exclude gap: graded penalty
    rec_mute_baseline: float = 0.54  # stand-in for the median when there are fewer than rec_mute_min_candidates stories
    rec_mute_min_candidates: int = 8
    rec_mute_penalty: float = 0.6
    rec_mute_incidental_penalty: float = 0.1  # muted phrase appears only in the blurb (not headline, category or entities)
    rec_wildcard_enabled: bool = True
    rec_wildcard_min_relevance: float = 0.25  # normalized relevance a wildcard must still have
    # A story gets a "matches your ..." explanation only when its normalized relevance is at least this.
    rec_explain_min_relevance: float = 0.4
    # Story embeddings: document text = headline + blurb + article intro; embeddings are cached on the item.
    rec_excerpt_chars: int = 600
    rec_embed_batch_size: int = 50
    rec_embed_max_retries: int = 2  # extra attempts for transient (non-quota) errors
    rec_embed_backoff_seconds: float = 1.0  # doubles on every retry
    rec_embed_timeout_seconds: float = 30.0  # overall budget for the embedding step, retries included
    rec_embed_price_per_million_tokens: float | None = None  # only used to estimate cost in logs and the evaluation
    # Feedback: how much one action moves the matched facet's learned weight (declared weights never change).
    rec_feedback_save: float = 0.10
    rec_feedback_ask: float = 0.05
    rec_feedback_deeper: float = 0.08
    rec_feedback_less: float = -0.15
    rec_feedback_max_delta: float = 0.5  # bound on the learned adjustment, up or down
    rec_feedback_half_life_days: float = 30.0
    rec_knew_step: float = 0.25  # "Knew this already": familiarity added to the story's topics
    rec_familiarity_cap: float = 0.6

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
