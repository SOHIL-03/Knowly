"""Public Gmail waitlist: an email and the newsletter senders the person wants as podcasts. Stored in Firestore
`waitlist/{sha256(email)}` (server only; the Firestore rules deny browser access), so signing up twice updates one document."""

from __future__ import annotations

import hashlib
import re
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.deps import services
from app.errors import ApiError
from app.services import Services
from app.store import now

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MAX_SENDERS = 10
LIMIT_PER_HOUR = 10  # per client address, in this process
_recent: dict[str, deque[float]] = defaultdict(deque)


class WaitlistIn(BaseModel):
    email: str = Field(max_length=254)
    senders: list[str] = Field(max_length=MAX_SENDERS)


def _limit(ip: str) -> None:
    hits, cutoff = _recent[ip], time.monotonic() - 3600
    while hits and hits[0] < cutoff:
        hits.popleft()
    if len(hits) >= LIMIT_PER_HOUR:
        raise ApiError(429, "rate_limited", "Too many sign-ups from this address. Try again later.")
    hits.append(time.monotonic())


@router.post("/waitlist")
def join_waitlist(body: WaitlistIn, request: Request, svc: Services = Depends(services)):
    email = body.email.strip().lower()
    senders = list(dict.fromkeys(s.strip().lower() for s in body.senders if s.strip()))
    if not EMAIL_RE.match(email):
        raise ApiError(422, "invalid", "Enter a valid email address")
    if not senders or len(senders) > MAX_SENDERS or not all(EMAIL_RE.match(s) for s in senders):
        raise ApiError(422, "invalid", f"Give 1 to {MAX_SENDERS} newsletter sender addresses, one per line")
    _limit(request.client.host if request.client else "unknown")
    ref = svc.db.collection("waitlist").document(hashlib.sha256(email.encode()).hexdigest())
    stamp = now()
    created = ref.get().to_dict() or {}
    ref.set({"email": email, "senders": senders, "createdAt": created.get("createdAt") or stamp, "updatedAt": stamp})
    return {"ok": True}
