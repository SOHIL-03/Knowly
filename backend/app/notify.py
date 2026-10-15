"""Briefing-ready notifications through Firebase Cloud Messaging."""

from __future__ import annotations

import logging

from app.logs import get_logger, log

logger = get_logger(__name__)


class FcmNotifier:
    def send(self, tokens: list[str], title: str, body: str, data: dict[str, str]) -> list[str]:
        """Sends to every token; returns tokens FCM reports as invalid so they can be removed."""
        if not tokens:
            return []
        from firebase_admin import messaging

        from app.infra.firebase import firebase_app

        msg = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(title=title, body=body),
            data=data,
        )
        try:
            res = messaging.send_each_for_multicast(msg, app=firebase_app())
        except Exception as exc:
            log(logger, logging.WARNING, "fcm_send_failed", error=type(exc).__name__)
            return []
        invalid = []
        for token, r in zip(tokens, res.responses, strict=True):
            if not r.success and r.exception and "registration-token-not-registered" in str(getattr(r.exception, "code", "")):
                invalid.append(token)
        return invalid
