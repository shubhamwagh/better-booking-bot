"""Notification helper — stdout only for now.

Replace the `send` function body when ready to add email/Telegram.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def send(subject: str, body: str) -> None:
    print(f"[NOTIFY] {subject}\n{body}\n")
    log.info("Notification: %s", subject)
