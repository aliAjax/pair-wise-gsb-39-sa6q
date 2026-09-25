"""Shared domain error and clock helpers.

Kept in its own module so that the subscription, impact and notification
modules can raise domain errors without importing :mod:`app`.
"""
from __future__ import annotations

from datetime import datetime, timezone


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
