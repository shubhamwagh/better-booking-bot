"""Notification helper - logs to stdout."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def send(subject: str, body: str) -> None:
    print(f"[NOTIFY] {subject}\n{body}\n")
    log.info(f"Notification: {subject}")
