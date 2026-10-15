"""Firebase Admin and Firestore clients.

With FIRESTORE_EMULATOR_HOST / FIREBASE_AUTH_EMULATOR_HOST set, both clients talk to the
local emulators and need no credentials.
"""

from __future__ import annotations

import os
from functools import lru_cache

import firebase_admin
from firebase_admin import auth as fb_auth
from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore

from app.config import get_settings


def using_emulators() -> bool:
    return bool(os.environ.get("FIRESTORE_EMULATOR_HOST"))


@lru_cache
def firebase_app() -> firebase_admin.App:
    s = get_settings()
    opts = {"projectId": s.gcp_project, "storageBucket": s.storage_bucket}
    try:
        return firebase_admin.get_app()
    except ValueError:
        pass
    if os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"):
        # The Auth emulator accepts unsigned tokens; Admin needs no real credential.
        return firebase_admin.initialize_app(credential=_EmulatorCredential(), options=opts)
    return firebase_admin.initialize_app(options=opts)


class _EmulatorCredential(firebase_admin.credentials.Base):
    def get_credential(self):  # type: ignore[override]
        return AnonymousCredentials()


@lru_cache
def firestore_client() -> firestore.Client:
    s = get_settings()
    if using_emulators():
        return firestore.Client(project=s.gcp_project, credentials=AnonymousCredentials())
    return firestore.Client(project=s.gcp_project)


def verify_id_token(token: str) -> dict:
    return fb_auth.verify_id_token(token, app=firebase_app(), check_revoked=not os.environ.get("FIREBASE_AUTH_EMULATOR_HOST"))


def delete_auth_user(uid: str) -> None:
    fb_auth.delete_user(uid, app=firebase_app())
