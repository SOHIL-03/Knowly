"""Request dependencies: Firebase ID token verification and internal-caller verification.

The uid always comes from the verified token. Request bodies never carry a uid.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, Request

from app.errors import ApiError
from app.logs import correlation_id
from app.services import Services, get_services


@dataclass
class CurrentUser:
    uid: str
    email: str | None
    anonymous: bool


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "unauthenticated", "Sign in to continue")
    return authorization.split(" ", 1)[1].strip()


def current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    from app.infra.firebase import verify_id_token

    token = _bearer(authorization)
    try:
        claims = verify_id_token(token)
    except Exception as exc:
        name = type(exc).__name__
        code = "session_expired" if "Expired" in name or "Revoked" in name else "unauthenticated"
        raise ApiError(401, code, "Your session has expired. Sign in again." if code == "session_expired" else "Sign in to continue")
    provider = (claims.get("firebase") or {}).get("sign_in_provider")
    return CurrentUser(uid=claims["uid"], email=claims.get("email"), anonymous=provider == "anonymous")


def services() -> Services:
    return get_services()


def internal_caller(request: Request, authorization: str | None = Header(default=None), svc: Services = Depends(services)) -> None:
    """Pub/Sub push, Cloud Tasks and Cloud Scheduler calls carry a Google OIDC token signed for our
    service account. In local mode the LOCAL_MEDIA_SIGNING_KEY acts as a shared secret instead."""
    token = _bearer(authorization)
    s = svc.settings
    if s.is_cloud:
        from google.auth.transport.requests import Request as GRequest
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_oauth2_token(token, GRequest(), audience=s.internal_base_url)
        except Exception:
            raise ApiError(403, "forbidden", "Invalid internal caller")
        if claims.get("email") != s.internal_invoker_sa or not claims.get("email_verified"):
            raise ApiError(403, "forbidden", "Invalid internal caller")
        return
    if not s.local_media_signing_key or not hmac.compare_digest(token, s.local_media_signing_key):
        raise ApiError(403, "forbidden", "Invalid internal caller")


async def correlation_middleware(request: Request, call_next):
    cid = request.headers.get("x-cloud-trace-context", "").split("/")[0] or request.headers.get("x-request-id") or ""
    import uuid

    token = correlation_id.set(cid or uuid.uuid4().hex[:16])
    try:
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id.get()
        return response
    finally:
        correlation_id.reset(token)
