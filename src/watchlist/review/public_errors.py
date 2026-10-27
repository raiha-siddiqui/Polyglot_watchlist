"""Public error policy shared by HTTP and MCP; no transport dependencies.

Local paths and exception details must not cross the interface boundary.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from watchlist.review.contracts import ReviewError, canonical

# ReviewError.code -> HTTP status (Decision 0007's proposal, adapted: an
# unusable workspace is 503 "unavailable", not 500).
REVIEW_STATUS: dict[str, int] = {
    "invalid_input": 422,
    "entity_not_found": 404,
    "reference_mismatch": 409,
    "case_not_found": 404,
    "stale_submission": 409,
    "submission_conflict": 409,
    "request_conflict": 409,
    "invalid_state": 409,
    "recovery_required": 409,
    "incompatible_version": 409,
    "retriever_mismatch": 409,
    "read_only": 403,
    "workspace_busy": 503,
    "workspace_invalid": 503,
    "retrieval_failed": 503,
    "context_provider_failed": 503,
    "storage_inconsistent": 500,
    "reentrant_call": 500,
}

# Fixed public messages for server-side failures; the original text may hold
# exception strings or local paths and is only summarized in server logs.
_SERVER_MESSAGES: dict[str, str] = {
    "workspace_busy": "The review workspace is busy; retry shortly.",
    "workspace_invalid": "The review workspace is unavailable.",
    "retrieval_failed": "Candidate retrieval failed; no result was inferred.",
    "context_provider_failed": "A review context provider failed; no result was inferred.",
    "storage_inconsistent": "Saved review records are inconsistent; nothing was repaired.",
    "reentrant_call": "Internal review-service call error.",
}

# Only these detail keys leave the server. Paths (e.g. journal ``files``),
# retriever descriptors and provider internals are dropped.
SAFE_DETAIL_KEYS = frozenset(
    {
        "case_id",
        "status",
        "next_action",
        "expected_packet_sha256",
        "allowed",
        "missing",
        "unexpected",
        "request_id",
        "provider",
        "cause",
        "case_origin",
        "submission_origin",
        "errors",
        "limit",
        "retry_after_seconds",
    }
)


def _safe_details(details: Mapping[str, Any]) -> dict[str, Any]:
    kept = {key: value for key, value in details.items() if key in SAFE_DETAIL_KEYS}
    try:
        return json.loads(canonical(kept))
    except (TypeError, ValueError):
        return {}


def error_body(
    code: str, message: str, retryable: bool, details: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The one error shape: ``{code, message, retryable, details}``."""
    return {
        "code": code,
        "message": message,
        "retryable": bool(retryable),
        "details": _safe_details(details or {}),
    }


def review_error_status(error: ReviewError) -> int:
    """HTTP status for a service error; unknown codes are treated as 500."""
    return REVIEW_STATUS.get(error.code, 500)


def review_error_body(error: ReviewError) -> dict[str, Any]:
    """Public body for a service error: fixed text for 5xx, filtered details."""
    status = review_error_status(error)
    message = (
        _SERVER_MESSAGES.get(error.code, "Internal review-service error.")
        if status >= 500
        else error.message
    )
    return error_body(error.code, message, error.retryable, error.details)
