"""Gmail read-only OAuth. The refresh token goes to Secret Manager and never reaches the browser."""

from __future__ import annotations

import httpx

from app.errors import ApiError
from app.infra.secrets import gmail_secret_name
from app.services import Services
from app.store import now

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"
ALLOWED_SCOPES = {GMAIL_SCOPE, "openid", "email", "profile",
                  "https://www.googleapis.com/auth/userinfo.email", "https://www.googleapis.com/auth/userinfo.profile"}


class GmailReauthRequired(RuntimeError):
    pass


def client_secret(svc: Services) -> str:
    s = svc.settings
    if s.gmail_oauth_client_secret_name:
        value = svc.secrets.get(s.gmail_oauth_client_secret_name)
        if value:
            return value
    if s.gmail_oauth_client_secret and not s.is_cloud:
        return s.gmail_oauth_client_secret
    raise ApiError(503, "gmail_not_configured", "Gmail connection is not configured on this server.")


def exchange_code(svc: Services, uid: str, code: str) -> dict:
    """Exchanges a GIS popup auth code (redirect_uri=postmessage) and stores the refresh token."""
    s = svc.settings
    if not s.gmail_oauth_client_id:
        raise ApiError(503, "gmail_not_configured", "Gmail connection is not configured on this server.")
    res = httpx.post(TOKEN_URI, data={
        "code": code, "client_id": s.gmail_oauth_client_id, "client_secret": client_secret(svc),
        "redirect_uri": "postmessage", "grant_type": "authorization_code",
    }, timeout=20)
    if res.status_code != 200:
        raise ApiError(400, "gmail_code_invalid", "Google did not accept the authorisation code.")
    data = res.json()
    scopes = set((data.get("scope") or "").split())
    if GMAIL_SCOPE not in scopes:
        raise ApiError(400, "gmail_scope_missing", "Read-only Gmail access was not granted.")
    if scopes - ALLOWED_SCOPES:
        raise ApiError(400, "gmail_scope_too_broad", "Knowly only accepts read-only Gmail access.")
    refresh = data.get("refresh_token")
    if not refresh:
        raise ApiError(400, "gmail_no_refresh_token", "Google did not return offline access. Remove Knowly from your Google account permissions and connect again.")
    name = gmail_secret_name(uid)
    svc.secrets.put(name, refresh)
    email = _email_for(data.get("access_token"))
    svc.paths.user(uid).set({"gmail": {"tokenSecretRef": name, "status": "ok", "email": email, "connectedAt": now()}}, merge=True)
    return {"email": email}


def _email_for(access_token: str | None) -> str | None:
    if not access_token:
        return None
    r = httpx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                  headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    return r.json().get("emailAddress") if r.status_code == 200 else None


def gmail_service(svc: Services, uid: str):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    user = svc.paths.user(uid).get().to_dict() or {}
    ref = (user.get("gmail") or {}).get("tokenSecretRef")
    token = svc.secrets.get(ref) if ref else None
    if not token:
        raise GmailReauthRequired("no stored Gmail token")
    creds = Credentials(None, refresh_token=token, token_uri=TOKEN_URI, client_id=svc.settings.gmail_oauth_client_id,
                        client_secret=client_secret(svc), scopes=[GMAIL_SCOPE])
    try:
        creds.refresh(Request())
    except RefreshError as exc:
        mark_reauth(svc, uid)
        raise GmailReauthRequired("refresh failed") from exc
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def mark_reauth(svc: Services, uid: str) -> None:
    # Pause triggers and keep the library (section 12).
    svc.paths.user(uid).set({"gmail": {"status": "reauth"}}, merge=True)
