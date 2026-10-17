"""POST /api/waitlist: validation, one document per email, upsert, and the per-address limit."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import services
from app.errors import install_error_handlers
from app.routes import waitlist


class Ref:
    def __init__(self, store, key):
        self.store, self.key = store, key

    def get(self):
        return SimpleNamespace(to_dict=lambda: self.store.get(self.key))

    def set(self, data):
        self.store[self.key] = data


class Db:
    def __init__(self):
        self.docs = {}

    def collection(self, name):
        assert name == "waitlist"
        return SimpleNamespace(document=lambda key: Ref(self.docs, key))


@pytest.fixture
def client():
    waitlist._recent.clear()
    db = Db()
    app = FastAPI()
    install_error_handlers(app)
    app.dependency_overrides[services] = lambda: SimpleNamespace(db=db)
    app.include_router(waitlist.router, prefix="/api")
    return TestClient(app), db


def test_saves_one_document_per_email_and_updates_it(client):
    c, db = client
    assert c.post("/api/waitlist", json={"email": "Me@Gmail.com", "senders": ["dan@tldrnewsletter.com", "DAN@tldrnewsletter.com"]}).json() == {"ok": True}
    key = hashlib.sha256(b"me@gmail.com").hexdigest()
    first = dict(db.docs[key])
    assert first["senders"] == ["dan@tldrnewsletter.com"]
    c.post("/api/waitlist", json={"email": "me@gmail.com", "senders": ["a@b.co"]})
    assert len(db.docs) == 1 and db.docs[key]["senders"] == ["a@b.co"] and db.docs[key]["createdAt"] == first["createdAt"]


@pytest.mark.parametrize("body", [
    {"email": "nope", "senders": ["a@b.co"]},
    {"email": "me@gmail.com", "senders": []},
    {"email": "me@gmail.com", "senders": ["not-an-address"]},
    {"email": "me@gmail.com", "senders": [f"s{i}@b.co" for i in range(11)]},
])
def test_rejects_bad_input(client, body):
    c, db = client
    assert c.post("/api/waitlist", json=body).status_code == 422 and not db.docs


def test_limits_sign_ups_per_address(client):
    c, _ = client
    codes = [c.post("/api/waitlist", json={"email": f"u{i}@gmail.com", "senders": ["a@b.co"]}).status_code for i in range(12)]
    assert codes[:10] == [200] * 10 and codes[10:] == [429, 429]
