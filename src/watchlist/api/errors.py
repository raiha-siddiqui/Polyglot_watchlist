"""Stable JSON error bodies and the review-error to HTTP status mapping.

Every error response has exactly ``{code, message, retryable, details}``.
``code`` is stable (the service's ``ReviewError.code`` or an API code below);
clients branch on it, never on ``message``. Server-side failures (5xx) get a
fixed message so exception text, file paths or retriever internals are not
returned, and ``details`` is filtered to an allow-list of safe keys. Errors are
never turned into review outcomes: a timeout or failure is not "no match".
"""

from __future__ import annotations

from typing import Any, Mapping

from watchlist.review.public_errors import (
    REVIEW_STATUS,
    SAFE_DETAIL_KEYS,
    error_body,
    review_error_body,
    review_error_status,
)

__all__ = [
    "ApiError",
    "REVIEW_STATUS",
    "SAFE_DETAIL_KEYS",
    "SECURITY_HEADERS",
    "error_body",
    "review_error_status",
    "review_error_body",
]

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("cache-control", "no-store"),
    ("content-security-policy", "default-src 'none'; frame-ancestors 'none'"),
    ("cross-origin-resource-policy", "same-origin"),
    (
        "x-research-notice",
        "research-only; possible record matches for human review; not identity, "
        "wrongdoing, sanctions status or clearance",
    ),
)


class ApiError(Exception):
    """A transport-level failure (auth, limits, routing) with a stable code."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})
        self.headers = dict(headers or {})

    def body(self) -> dict[str, Any]:
        return error_body(self.code, self.message, self.retryable, self.details)
