"""Small typed HTTP client for the v2 review API.

The client is a transport adapter only: it sends the API's own request bodies,
returns the API's JSON objects unchanged and turns every failure into an
``ApiClientError`` with a stable ``code``. It never retries a write on its own,
never turns an error into a review outcome and never prints, logs or returns
the API key.

Configuration comes from the environment:

``WATCHLIST_API_URL``
    Base URL of the API (default ``http://127.0.0.1:8000``). Plain ``http`` is
    accepted only for loopback hosts, so the key is never sent unencrypted
    over a network.
``WATCHLIST_API_KEY``
    Bearer key issued with ``python -m watchlist.api new-key``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
import re
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit
import uuid

import httpx

__all__ = [
    "DEFAULT_API_URL",
    "ENV_API_KEY",
    "ENV_API_URL",
    "OUTCOMES",
    "ORIGINS",
    "ApiClientError",
    "ClientConfig",
    "ReviewForm",
    "WatchlistClient",
    "new_uuid",
]

ENV_API_URL = "WATCHLIST_API_URL"
ENV_API_KEY = "WATCHLIST_API_KEY"
DEFAULT_API_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 40.0  # longer than the API's 30 s write wait

Outcome = Literal["candidate_for_follow_up", "none_selected", "unresolved"]
Origin = Literal["human", "simulated_demo", "test", "unknown"]
OUTCOMES: tuple[str, ...] = ("candidate_for_follow_up", "none_selected", "unresolved")
ORIGINS: tuple[str, ...] = ("human", "simulated_demo", "test", "unknown")

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ENTITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_LOOPBACK_NAMES = frozenset({"localhost"})


class ApiClientError(Exception):
    """A failed API call with a stable ``code``.

    ``status`` is the HTTP status, or ``None`` when no response arrived.
    API errors keep the server's ``code``, ``message``, ``retryable`` and
    ``details``; client-side failures use ``client_config_invalid``,
    ``invalid_input``, ``api_unreachable``, ``api_timeout`` or ``bad_response``.
    Messages never contain the API key.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details: dict[str, Any] = dict(details or {})


def new_uuid() -> str:
    """A fresh lowercase hyphenated UUID4, the form the API requires."""
    return str(uuid.uuid4())


def _is_loopback(host: str) -> bool:
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ClientConfig:
    """API location and key. ``repr`` never shows the key."""

    base_url: str = DEFAULT_API_URL
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ApiClientError(
                "client_config_invalid",
                f"{ENV_API_URL} must be an http(s) URL with a host.",
            )
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ApiClientError(
                "client_config_invalid",
                f"{ENV_API_URL} must not contain credentials, a query or a fragment.",
            )
        if parts.scheme == "http" and not _is_loopback(parts.hostname):
            raise ApiClientError(
                "client_config_invalid",
                "Plain http is allowed only for loopback hosts; use https elsewhere.",
            )
        key = self.api_key
        if key is not None and (
            not 16 <= len(key) <= 512 or not key.isascii() or not key.isprintable()
        ):
            # Never echo the key or its length.
            raise ApiClientError(
                "client_config_invalid",
                f"{ENV_API_KEY} is not a valid API key.",
            )

    @property
    def has_key(self) -> bool:
        """Whether a key is configured (the key itself is never displayed)."""
        return self.api_key is not None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ClientConfig:
        """Read ``WATCHLIST_API_URL`` and ``WATCHLIST_API_KEY``."""
        env = os.environ if environ is None else environ
        url = env.get(ENV_API_URL, "").strip() or DEFAULT_API_URL
        key = env.get(ENV_API_KEY, "").strip() or None
        return cls(base_url=url.rstrip("/"), api_key=key)


@dataclass(frozen=True)
class ReviewForm:
    """One review form bound to the case revision and packet the reviewer saw."""

    submission_id: str
    revision: str
    packet_sha256: str
    outcome: str
    candidate_entity_id: str | None
    reason: str

    def to_json(self) -> dict[str, Any]:
        """The exact ``POST /v2/cases/{id}/review`` body."""
        return {
            "submission_id": self.submission_id,
            "revision": self.revision,
            "packet_sha256": self.packet_sha256,
            "outcome": self.outcome,
            "candidate_entity_id": self.candidate_entity_id,
            "reason": self.reason,
        }


def _check(pattern: re.Pattern[str], value: object, name: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ApiClientError("invalid_input", f"{name} has an invalid format.")
    return value


class WatchlistClient:
    """Typed calls to the v2 review API.

    ``http`` injects a ready ``httpx.Client`` (for example FastAPI's
    ``TestClient``); its own base URL is then used. Otherwise a client is
    created for ``config.base_url`` that ignores proxy environment variables
    and does not follow redirects, so the key goes only to the configured API.
    """

    def __init__(
        self,
        config: ClientConfig,
        *,
        http: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.config = config
        self._http = http or httpx.Client(
            base_url=config.base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"WatchlistClient(base_url={self.config.base_url!r}, key_configured={self.config.has_key})"

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    # -- transport ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if auth:
            if self.config.api_key is None:
                raise ApiClientError(
                    "client_config_invalid",
                    f"No API key configured; set {ENV_API_KEY}.",
                )
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        try:
            response = self._http.request(
                method,
                path,
                json=None if body is None else dict(body),
                params=None
                if params is None
                else {k: v for k, v in params.items() if v is not None},
                headers=headers,
            )
        except httpx.TimeoutException:
            raise ApiClientError(
                "api_timeout",
                "The API did not answer in time. The operation may still finish: "
                "reload the case before doing anything else.",
                retryable=True,
            ) from None
        except httpx.HTTPError as exc:
            raise ApiClientError(
                "api_unreachable",
                f"The API could not be reached ({type(exc).__name__}).",
                retryable=True,
            ) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise ApiClientError(
                "bad_response",
                f"The API returned a non-JSON response (HTTP {response.status_code}).",
                status=response.status_code,
            )
        if response.status_code >= 400:
            code = payload.get("code")
            message = payload.get("message")
            details = payload.get("details")
            raise ApiClientError(
                code if isinstance(code, str) and code else "http_error",
                message if isinstance(message, str) else f"HTTP {response.status_code}",
                status=response.status_code,
                retryable=payload.get("retryable") is True,
                details=details if isinstance(details, dict) else None,
            )
        if not isinstance(payload.get("limitation"), str):
            raise ApiClientError(
                "bad_response",
                "The API response has no research limitation notice.",
                status=response.status_code,
            )
        return payload

    # -- endpoints ----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /health`` (no key needed)."""
        return self._request("GET", "/health", auth=False)

    def start_case(
        self, name: str, *, origin: str, request_id: str | None
    ) -> dict[str, Any]:
        """``POST /v2/cases``. Always pass a fresh ``request_id`` per screening form."""
        if request_id is not None:
            _check(_UUID, request_id, "request_id")
        return self._request(
            "POST",
            "/v2/cases",
            body={"name": name, "origin": origin, "request_id": request_id},
        )

    def list_cases(
        self, *, limit: int = 50, offset: int = 0, origin: str | None = None
    ) -> dict[str, Any]:
        """``GET /v2/cases`` (summaries only: no names, evidence or notes)."""
        return self._request(
            "GET",
            "/v2/cases",
            params={"limit": limit, "offset": offset, "origin": origin},
        )

    def get_case(self, case_id: str) -> dict[str, Any]:
        """``GET /v2/cases/{id}``: ``{case, revision, limitation}``."""
        return self._request("GET", f"/v2/cases/{_check(_UUID, case_id, 'case_id')}")

    def submit_review(self, case_id: str, form: ReviewForm) -> dict[str, Any]:
        """``POST /v2/cases/{id}/review``. Resending the same form is idempotent."""
        _check(_UUID, form.submission_id, "submission_id")
        _check(_SHA256, form.revision, "revision")
        _check(_SHA256, form.packet_sha256, "packet_sha256")
        return self._request(
            "POST",
            f"/v2/cases/{_check(_UUID, case_id, 'case_id')}/review",
            body=form.to_json(),
        )

    def recover(self, case_id: str) -> dict[str, Any]:
        """``POST /v2/cases/{id}/recover``; never invents a review."""
        return self._request(
            "POST", f"/v2/cases/{_check(_UUID, case_id, 'case_id')}/recover"
        )

    def cancel(self, case_id: str, reason: str) -> dict[str, Any]:
        """``POST /v2/cases/{id}/cancel`` (cancellation is not a review outcome)."""
        return self._request(
            "POST",
            f"/v2/cases/{_check(_UUID, case_id, 'case_id')}/cancel",
            body={"reason": reason},
        )

    def audit(self, case_id: str) -> dict[str, Any]:
        """``GET /v2/cases/{id}/audit``."""
        return self._request(
            "GET", f"/v2/cases/{_check(_UUID, case_id, 'case_id')}/audit"
        )

    def verify_audit(self, case_id: str) -> dict[str, Any]:
        """``GET /v2/cases/{id}/audit/verify`` (local tamper-evidence check)."""
        return self._request(
            "GET", f"/v2/cases/{_check(_UUID, case_id, 'case_id')}/audit/verify"
        )

    def get_entity(self, entity_id: str) -> dict[str, Any]:
        """``GET /v2/entities/{id}``: a fictional reference record."""
        return self._request(
            "GET", f"/v2/entities/{_check(_ENTITY_ID, entity_id, 'entity_id')}"
        )
