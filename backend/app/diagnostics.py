"""Startup diagnostics: one log line describing the runtime, plus TCP probes of the local emulators.

A refused connection to the Storage or Firestore emulator otherwise only shows up as a 2-minute
client retry timeout deep inside a briefing, so we check the ports as soon as the API starts."""

from __future__ import annotations

import logging
import os
import platform
import socket
import sys
from typing import Any
from urllib.parse import urlparse

from app.config import Settings
from app.logs import get_logger, log

logger = get_logger(__name__)

EMULATOR_VARS = ("FIRESTORE_EMULATOR_HOST", "FIREBASE_AUTH_EMULATOR_HOST", "STORAGE_EMULATOR_HOST")
CA_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def _host_port(value: str) -> tuple[str, int] | None:
    parsed = urlparse(value if "://" in value else f"//{value}")
    return (parsed.hostname, parsed.port) if parsed.hostname and parsed.port else None


def probe(value: str, timeout: float = 1.0) -> str:
    """'ok' when something accepts TCP connections at host:port (or a URL), else the error name."""
    target = _host_port(value)
    if target is None:
        return "bad_address"
    try:
        with socket.create_connection(target, timeout=timeout):
            return "ok"
    except OSError as exc:
        return type(exc).__name__


def emulator_status() -> dict[str, str]:
    return {name: probe(os.environ[name]) for name in EMULATOR_VARS if os.environ.get(name)}


def ca_bundle_status() -> dict[str, Any]:
    """CA-bundle variables that point at a missing file break every provider call (FileNotFoundError)."""
    out: dict[str, Any] = {}
    for name in CA_VARS:
        value = os.environ.get(name)
        if value:
            out[name] = {"path": value, "exists": os.path.exists(value)}
    return out


def log_startup(settings: Settings) -> None:
    from app.ai.gemini_keys import load_keys

    key_names = [k.name for k in load_keys(settings)]  # names only, never values
    log(
        logger, logging.INFO, "app_startup",
        env=settings.env, logLevel=settings.log_level, dispatchMode=settings.dispatch_mode,
        python=sys.version.split()[0], platform=platform.platform(), cwd=os.getcwd(),
        project=settings.gcp_project, bucket=settings.storage_bucket,
        geminiVertex=settings.gemini_use_vertex, geminiKeys=key_names,
        openaiModel=settings.openai_model, openaiConfigured=bool(settings.openai_api_key.strip()),
        embedModel=settings.gemini_embed_model, ttsModel=settings.gemini_tts_model,
        emulators={n: os.environ[n] for n in EMULATOR_VARS if os.environ.get(n)},
        caBundles=ca_bundle_status(),
    )
    for name, status in emulator_status().items():
        if status != "ok":
            log(logger, logging.WARNING, "emulator_unreachable", variable=name, address=os.environ[name], error=status,
                hint="Start the Firebase emulators (frontend/run.sh); a stale Firestore emulator can make the script skip them")
        else:
            log(logger, logging.INFO, "emulator_reachable", variable=name, address=os.environ[name])
    for name, info in ca_bundle_status().items():
        if not info["exists"]:
            log(logger, logging.WARNING, "ca_bundle_missing", variable=name, path=info["path"],
                hint="API calls will fail with FileNotFoundError; unset it or point it at an existing file")
    if not settings.openai_api_key.strip():
        log(logger, logging.WARNING, "openai_not_configured", hint="Set OPENAI_API_KEY in backend/.env (all text generation)")
    if not key_names and not settings.gemini_use_vertex:
        log(logger, logging.WARNING, "gemini_not_configured", hint="Set GEMINI_API_KEY_1 in backend/.env (embeddings and audio)")
