"""B2 Fetch for Gmail mode, plus sender detection for onboarding.

Only messages from senders the user enabled are read in full, and only if they carry a
List-Unsubscribe or List-Id header. Personal mail from a followed sender is skipped.
"""

from __future__ import annotations

import base64
import email.utils
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from app.gmail.auth import GmailReauthRequired, gmail_service, mark_reauth
from app.logs import exc_fields, get_logger, log
from app.pipeline.ingest import RawIssue
from app.pipeline.text import is_newsletter
from app.services import Services
from app.store import now, slug

logger = get_logger(__name__)
KNOWN_TECH_DOMAINS = {"tldrnewsletter.com", "bytebytego.com", "deeplearning.ai", "pragmaticengineer.com",
                      "pythonweekly.com", "substack.com", "bensbites.co", "theneurondaily.com"}
MAX_ISSUES_PER_RUN = 20


def sender_id_for(address: str) -> str:
    return f"snd_{slug(address)}"


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _html_part(payload: dict[str, Any]) -> str | None:
    stack = [payload]
    plain = None
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/html":
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        if data and mime == "text/plain" and plain is None:
            text = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
            plain = "<pre>" + text.replace("<", "&lt;") + "</pre>"
        stack.extend(part.get("parts", []) or [])
    return plain


def detect_senders(svc: Services, uid: str) -> list[dict[str, Any]]:
    """Senders seen in the last 30 days with a newsletter header (decision A3)."""
    try:
        gmail = gmail_service(svc, uid)
    except GmailReauthRequired:
        log(logger, logging.WARNING, "gmail_reauth_required", uid=uid, during="detect_senders")
        return []
    ids: list[str] = []
    req = gmail.users().messages().list(userId="me", q="newer_than:30d", maxResults=100)
    while req is not None and len(ids) < 300:
        res = req.execute()
        ids += [m["id"] for m in res.get("messages", [])]
        req = gmail.users().messages().list_next(req, res)
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for mid in ids:
        msg = gmail.users().messages().get(userId="me", id=mid, format="metadata",
                                           metadataHeaders=["From", "List-Unsubscribe", "List-Id"]).execute()
        h = _headers(msg)
        if not is_newsletter(h):
            continue
        name, addr = email.utils.parseaddr(h.get("From", ""))
        if addr:
            counts[addr.lower()] += 1
            names[addr.lower()] = name or addr
    enabled = {s.id: (s.to_dict() or {}).get("enabled") for s in svc.paths.newsletters(uid).stream()}
    out = []
    for addr, n in counts.most_common(50):
        sid = sender_id_for(addr)
        domain = addr.split("@")[-1]
        known = any(domain == d or domain.endswith("." + d) for d in KNOWN_TECH_DOMAINS)
        svc.paths.sender(sid).set({"name": names[addr], "domain": domain, "address": addr, "isKnownTech": known}, merge=True)
        out.append({"senderId": sid, "name": names[addr], "address": addr, "domain": domain, "issueCount": n,
                    "isKnownTech": known, "enabled": enabled.get(sid, known)})
    log(logger, logging.INFO, "senders_detected", uid=uid, messagesScanned=len(ids), senders=len(out))
    return out


def fetch_new_issues(svc: Services, uid: str, user: dict[str, Any]) -> list[RawIssue]:
    gmail_state = user.get("gmail") or {}
    if gmail_state.get("status") != "ok":
        log(logger, logging.WARNING, "gmail_fetch_skipped", uid=uid, reason="gmail not connected", status=gmail_state.get("status"))
        return []
    subs = {s.id: s.to_dict() or {} for s in svc.paths.newsletters(uid).where(filter=FieldFilter("enabled", "==", True)).stream()}
    if not subs:
        log(logger, logging.WARNING, "gmail_fetch_skipped", uid=uid, reason="no enabled newsletters")
        return []
    addresses = {}
    for sid in subs:
        sender = svc.paths.sender(sid).get().to_dict() or {}
        if sender.get("address"):
            addresses[sender["address"].lower()] = (sid, sender)
    if not addresses:
        log(logger, logging.WARNING, "gmail_fetch_skipped", uid=uid, reason="enabled senders have no address", senders=len(subs))
        return []
    since = min((s.get("lastFetchedAt") for s in subs.values() if s.get("lastFetchedAt")), default=None)
    since = since or (now() - timedelta(days=2))
    query = "from:(" + " OR ".join(addresses) + f") after:{int(since.timestamp())}"
    log(logger, logging.INFO, "gmail_fetch_start", uid=uid, senders=len(addresses), since=since.isoformat())
    try:
        gmail = gmail_service(svc, uid)
        res = gmail.users().messages().list(userId="me", q=query, maxResults=MAX_ISSUES_PER_RUN).execute()
    except GmailReauthRequired:
        log(logger, logging.WARNING, "gmail_reauth_required", uid=uid)
        return []
    except Exception as exc:
        if "invalid_grant" in str(exc):
            log(logger, logging.WARNING, "gmail_invalid_grant", uid=uid)
            mark_reauth(svc, uid)
            return []
        log(logger, logging.ERROR, "gmail_list_failed", uid=uid, **exc_fields(exc))
        raise
    log(logger, logging.INFO, "gmail_listed", uid=uid, messages=len(res.get("messages", [])))
    out: list[RawIssue] = []
    for ref in res.get("messages", []):
        msg = gmail.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        h = _headers(msg)
        _, addr = email.utils.parseaddr(h.get("From", ""))
        match = addresses.get(addr.lower())
        if not match:
            log(logger, logging.INFO, "gmail_message_skipped", uid=uid, reason="sender not enabled")
            continue
        if not is_newsletter(h):
            log(logger, logging.INFO, "skipped_personal_mail", uid=uid)
            continue
        html = _html_part(msg.get("payload", {}))
        if not html:
            log(logger, logging.WARNING, "gmail_message_skipped", uid=uid, reason="no html or text part", messageId=msg["id"])
            continue
        sid, sender = match
        received = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=UTC)
        out.append(RawIssue(sender_id=sid, sender_name=sender.get("name", addr), sender_domain=sender.get("domain", ""),
                            subject=h.get("Subject", "(no subject)"), received_at=received, html=html,
                            headers={k: v for k, v in h.items() if k in ("List-Unsubscribe", "List-Id")},
                            message_id=msg["id"], source="gmail", is_known_tech=bool(sender.get("isKnownTech"))))
    at = now()
    for sid in subs:
        svc.paths.newsletters(uid).document(sid).set({"lastFetchedAt": at}, merge=True)
    log(logger, logging.INFO, "gmail_fetch_done", uid=uid, issues=len(out))
    return out
