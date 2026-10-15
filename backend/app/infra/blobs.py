"""Cloud Storage access for audio, fetched articles and raw issue HTML.

Path conventions (audio is private per user; raw HTML and articles are the shared cache):
  raw/{issueId}.html                      raw newsletter HTML, deleted by a 7-day lifecycle rule
  articles/{itemId}.txt                   fetched article main text (shared cache)
  audio/{uid}/{briefingId}/{segId}.wav    segment podcast audio (Gemini multi-speaker TTS)
  audio/{uid}/qa/{qaId}.wav               spoken answer (Gemini single-speaker TTS)
  audio/{uid}/quiz/{quizId}-{part}.wav    spoken quiz question or feedback
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import timedelta
from typing import Protocol

from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from app.config import Settings, get_settings


class BlobStore(Protocol):
    def put_text(self, path: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None: ...
    def put_bytes(self, path: str, data: bytes, content_type: str) -> None: ...
    def get_text(self, path: str) -> str | None: ...
    def get_bytes(self, path: str) -> bytes | None: ...
    def exists(self, path: str) -> bool: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def read_url(self, path: str) -> str: ...
    def gs_uri(self, path: str) -> str: ...


class GcsBlobStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if os.environ.get("STORAGE_EMULATOR_HOST"):
            self.client = storage.Client(project=settings.gcp_project, credentials=AnonymousCredentials())
        else:
            self.client = storage.Client(project=settings.gcp_project)
        self.bucket = self.client.bucket(settings.storage_bucket)

    def put_text(self, path: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        self.bucket.blob(path).upload_from_string(text.encode("utf-8"), content_type=content_type)

    def put_bytes(self, path: str, data: bytes, content_type: str) -> None:
        self.bucket.blob(path).upload_from_string(data, content_type=content_type)

    def get_bytes(self, path: str) -> bytes | None:
        blob = self.bucket.blob(path)
        try:
            return blob.download_as_bytes()
        except Exception as exc:  # google.api_core NotFound and emulator 404s
            if "404" in str(exc) or exc.__class__.__name__ == "NotFound":
                return None
            raise

    def get_text(self, path: str) -> str | None:
        data = self.get_bytes(path)
        return data.decode("utf-8") if data is not None else None

    def exists(self, path: str) -> bool:
        return self.bucket.blob(path).exists()

    def delete_prefix(self, prefix: str) -> int:
        count = 0
        for blob in self.client.list_blobs(self.bucket, prefix=prefix):
            blob.delete()
            count += 1
        return count

    def gs_uri(self, path: str) -> str:
        return f"gs://{self.settings.storage_bucket}/{path}"

    def read_url(self, path: str) -> str:
        if self.settings.is_cloud:
            # Cloud Run has no private key; sign through the IAM signBlob API.
            import google.auth
            from google.auth.transport.requests import Request

            creds, _ = google.auth.default()
            creds.refresh(Request())
            return self.bucket.blob(path).generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=self.settings.signed_url_ttl_seconds),
                method="GET",
                service_account_email=getattr(creds, "service_account_email", None),
                access_token=creds.token,
            )
        return local_media_url(self.settings, path)


def _media_signature(key: str, path: str, expires: int) -> str:
    mac = hmac.new(key.encode(), f"{path}|{expires}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def local_media_url(settings: Settings, path: str) -> str:
    """Short-lived URL served by /media in local mode, where the emulator cannot sign URLs."""
    if not settings.local_media_signing_key:
        raise RuntimeError("LOCAL_MEDIA_SIGNING_KEY must be set for local media URLs")
    expires = int(time.time()) + settings.signed_url_ttl_seconds
    token = base64.urlsafe_b64encode(json.dumps({"p": path, "e": expires}).encode()).decode().rstrip("=")
    sig = _media_signature(settings.local_media_signing_key, path, expires)
    # Relative by default: the browser resolves it against the page's own host (localhost, a LAN IP
    # or a tunnel), and the dev server proxies /api to the backend.
    base = settings.public_api_base_url.rstrip("/")
    return f"{base}/api/media?t={token}&s={sig}"


def verify_local_media(settings: Settings, token: str, sig: str) -> str | None:
    try:
        padded = token + "=" * (-len(token) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        path, expires = data["p"], int(data["e"])
    except Exception:
        return None
    if expires < time.time() or not settings.local_media_signing_key:
        return None
    if not hmac.compare_digest(_media_signature(settings.local_media_signing_key, path, expires), sig):
        return None
    return path


def default_blob_store() -> BlobStore:
    return GcsBlobStore(get_settings())
