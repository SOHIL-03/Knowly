"""Gmail refresh tokens live in Secret Manager, never in Firestore or the browser.

Secret Manager has no local emulator, so local development uses a file store under
backend/.local-secrets (git-ignored). It refuses to run when ENV=cloud.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.logs import uid_hash


class SecretStore(Protocol):
    def put(self, name: str, value: str) -> str: ...
    def get(self, name: str) -> str | None: ...
    def delete(self, name: str) -> None: ...


def gmail_secret_name(uid: str) -> str:
    return f"gmail-token-{uid_hash(uid)}"


class SecretManagerStore:
    def __init__(self, settings: Settings) -> None:
        from google.cloud import secretmanager

        self.client = secretmanager.SecretManagerServiceClient()
        self.project = settings.gcp_project

    def _secret_path(self, name: str) -> str:
        return f"projects/{self.project}/secrets/{name}"

    def put(self, name: str, value: str) -> str:
        from google.api_core.exceptions import AlreadyExists

        try:
            self.client.create_secret(
                parent=f"projects/{self.project}",
                secret_id=name,
                secret={"replication": {"automatic": {}}},
            )
        except AlreadyExists:
            pass
        version = self.client.add_secret_version(parent=self._secret_path(name), payload={"data": value.encode()})
        return version.name

    def get(self, name: str) -> str | None:
        from google.api_core.exceptions import NotFound

        try:
            res = self.client.access_secret_version(name=f"{self._secret_path(name)}/versions/latest")
        except NotFound:
            return None
        return res.payload.data.decode()

    def delete(self, name: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self.client.delete_secret(name=self._secret_path(name))
        except NotFound:
            pass


class LocalDevSecretStore:
    def __init__(self, settings: Settings, root: Path | None = None) -> None:
        if settings.is_cloud:
            raise RuntimeError("LocalDevSecretStore must not be used in cloud mode")
        self.path = (root or Path(__file__).resolve().parents[2] / ".local-secrets") / "secrets.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def put(self, name: str, value: str) -> str:
        data = self._load()
        data[name] = value
        self.path.write_text(json.dumps(data))
        return f"local/{name}"

    def get(self, name: str) -> str | None:
        return self._load().get(name)

    def delete(self, name: str) -> None:
        data = self._load()
        data.pop(name, None)
        self.path.write_text(json.dumps(data))


def default_secret_store(settings: Settings) -> SecretStore:
    return SecretManagerStore(settings) if settings.is_cloud else LocalDevSecretStore(settings)
