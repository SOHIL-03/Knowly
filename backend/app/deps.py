"""Request dependencies: Firebase ID token verification and internal-caller verification.

The uid always comes from the verified token. Request bodies never carry a uid.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, Request

from app.errors import ApiError
from app.logs import Timer, correlation_id, exc_fields, get_logger, log
from app.services import Services, get_services


logger = get_logger(__name__)
QUIET_PATHS = {"/api/health", "/api/ready"}  # polled constantly; logged at DEBUG only


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
        log(logger, logging.WARNING, "auth_token_rejected", code=code, **exc_fields(exc, frames=2))
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
        except Exception as exc:
            log(logger, logging.WARNING, "internal_token_rejected", path=request.url.path, **exc_fields(exc, frames=2))
            raise ApiError(403, "forbidden", "Invalid internal caller")
        if claims.get("email") != s.internal_invoker_sa or not claims.get("email_verified"):
            log(logger, logging.WARNING, "internal_caller_mismatch", path=request.url.path)
            raise ApiError(403, "forbidden", "Invalid internal caller")
        return
    if not s.local_media_signing_key or not hmac.compare_digest(token, s.local_media_signing_key):
        log(logger, logging.WARNING, "internal_token_rejected", path=request.url.path, reason="local key mismatch")
        raise ApiError(403, "forbidden", "Invalid internal caller")


async def correlation_middleware(request: Request, call_next):
    cid = request.headers.get("x-cloud-trace-context", "").split("/")[0] or request.headers.get("x-request-id") or ""
    token = correlation_id.set(cid or uuid.uuid4().hex[:16])
    quiet = request.url.path in QUIET_PATHS
    level = logging.DEBUG if quiet else logging.INFO
    timer = Timer()
    log(logger, level, "request_start", method=request.method, path=request.url.path,
        query=request.url.query[:200] or None, client=request.client.host if request.client else None)
    try:
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id.get()
        # 5xx are errors, 4xx are warnings (the handler also logs the code), the rest is routine.
        status = response.status_code
        log(logger, logging.ERROR if status >= 500 else logging.WARNING if status >= 400 and not quiet else level,
            "request_done", method=request.method, path=request.url.path, status=status, durationMs=timer.ms())
        return response
    except Exception as exc:
        log(logger, logging.ERROR, "request_crashed", method=request.method, path=request.url.path,
            durationMs=timer.ms(), **exc_fields(exc))
        raise
    finally:
        correlation_id.reset(token)
