"""Sending email. Backends: console (log only), smtp, memory (tests read `outbox`)."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

log = logging.getLogger(__name__)
outbox: list[EmailMessage] = []


def send(to: str, subject: str, body: str) -> None:
    """Best effort: a failure is logged, never raised, so a request doesn't fail because email is down."""
    settings = get_settings()
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = settings.email_from, to, subject
    message.set_content(body)
    try:
        if settings.email_backend == "smtp":
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls()
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password or "")
                smtp.send_message(message)
        elif settings.email_backend == "memory":
            outbox.append(message)
        else:
            log.info("EMAIL to=%s subject=%r\n%s", to, subject, body)
    except Exception:  # noqa: BLE001
        log.exception("could not send email to %s", to)
