"""Validated API settings, read from explicit arguments and environment variables.

Environment variables (all optional except one key source):

``WATCHLIST_API_KEYS``
    Comma-, semicolon- or newline-separated ``actor:role:sha256hex`` entries.
``WATCHLIST_API_KEYS_FILE``
    Path to a UTF-8 file with one entry per line (``#`` comments allowed).
    Exactly one of the two key sources may be set.
``WATCHLIST_API_WRITE_QUEUE_MAX``
    Write operations allowed to wait behind the running one (default 8).
``WATCHLIST_API_WRITE_WAIT_SECONDS``
    How long one HTTP request waits for its queued write (default 30). This is
    the HTTP wait, not an inference deadline: an operation that outlives it
    still finishes and is visible through ``GET /v2/cases/{id}``.
``WATCHLIST_API_MAX_IN_FLIGHT``
    HTTP requests handled at once (default 64); more get ``429
    too_many_requests``. ``/health`` is exempt.
``WATCHLIST_API_BODY_TIMEOUT_SECONDS``
    How long the server waits for a complete request body (default 10). A
    client that stalls longer gets ``408 request_timeout``; a request only
    counts against ``WATCHLIST_API_MAX_IN_FLIGHT`` once its body has arrived.
``WATCHLIST_TRACE_PATH``
    Optional JSON-lines file for redacted span records (Step 27). Unset means
    spans only feed in-process metrics. The directory must already exist.
``WATCHLIST_LOG_FORMAT``
    ``text`` (default, logging left as configured by the host) or ``json``
    (``create_app`` installs the redacting JSON handler on ``watchlist``).

Keys are never read from the request, the config file or the repository.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
from typing import Mapping

from watchlist.api.auth import ApiKey, AuthConfigError, parse_key_entries

__all__ = [
    "ApiSettings",
    "SettingsError",
    "DEFAULT_BODY_TIMEOUT_SECONDS",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_IN_FLIGHT",
    "DEFAULT_WRITE_QUEUE_MAX",
    "DEFAULT_WRITE_WAIT_SECONDS",
    "PAGE_MAX",
]

DEFAULT_MAX_BODY_BYTES = 64 * 1024
DEFAULT_WRITE_QUEUE_MAX = 8
DEFAULT_WRITE_WAIT_SECONDS = 30.0
DEFAULT_MAX_IN_FLIGHT = 64
DEFAULT_BODY_TIMEOUT_SECONDS = 10.0
DEFAULT_TRACE_MAX_BYTES = 50 * 1024 * 1024
LOG_FORMATS = ("text", "json")
# API page cap (Step 24 proposal: 50-item pages); the service may allow more.
PAGE_MAX = 50

ENV_KEYS = "WATCHLIST_API_KEYS"
ENV_KEYS_FILE = "WATCHLIST_API_KEYS_FILE"
ENV_QUEUE = "WATCHLIST_API_WRITE_QUEUE_MAX"
ENV_WAIT = "WATCHLIST_API_WRITE_WAIT_SECONDS"
ENV_IN_FLIGHT = "WATCHLIST_API_MAX_IN_FLIGHT"
ENV_BODY_TIMEOUT = "WATCHLIST_API_BODY_TIMEOUT_SECONDS"
ENV_TRACE_PATH = "WATCHLIST_TRACE_PATH"
ENV_LOG_FORMAT = "WATCHLIST_LOG_FORMAT"


class SettingsError(ValueError):
    """API settings are missing or invalid; messages never contain key material."""


@dataclass(frozen=True)
class ApiSettings:
    """Everything ``create_app`` needs besides an optional injected service.

    ``workspace`` and ``config_path`` are required only when ``create_app``
    opens the service itself. ``api_keys`` hold hashes only.
    """

    workspace: Path | None = None
    config_path: Path | None = None
    api_keys: tuple[ApiKey, ...] = ()
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    write_queue_max: int = DEFAULT_WRITE_QUEUE_MAX
    write_wait_seconds: float = DEFAULT_WRITE_WAIT_SECONDS
    # Step 27 observability and load-shedding settings.
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    trace_path: Path | None = None
    trace_max_bytes: int = DEFAULT_TRACE_MAX_BYTES
    log_format: str = "text"
    body_timeout_seconds: float = DEFAULT_BODY_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if (
            type(self.max_body_bytes) is not int
            or not 1024 <= self.max_body_bytes <= 1_048_576
        ):
            raise SettingsError(
                "max_body_bytes must be an integer from 1024 to 1048576"
            )
        if (
            type(self.write_queue_max) is not int
            or not 0 <= self.write_queue_max <= 1000
        ):
            raise SettingsError("write_queue_max must be an integer from 0 to 1000")
        wait = self.write_wait_seconds
        if (
            type(wait) not in (int, float)
            or not math.isfinite(wait)
            or not 0 < wait <= 3600
        ):
            raise SettingsError("write_wait_seconds must be a number in (0, 3600]")
        if type(self.max_in_flight) is not int or not 1 <= self.max_in_flight <= 10000:
            raise SettingsError("max_in_flight must be an integer from 1 to 10000")
        body_wait = self.body_timeout_seconds
        if (
            type(body_wait) not in (int, float)
            or not math.isfinite(body_wait)
            or not 0 < body_wait <= 300
        ):
            raise SettingsError("body_timeout_seconds must be a number in (0, 300]")
        if self.trace_path is not None and not isinstance(self.trace_path, Path):
            raise SettingsError("trace_path must be a pathlib.Path or None")
        if (
            type(self.trace_max_bytes) is not int
            or not 4096 <= self.trace_max_bytes <= 2**40
        ):
            raise SettingsError("trace_max_bytes must be an integer of at least 4096")
        if self.log_format not in LOG_FORMATS:
            raise SettingsError("log_format must be 'text' or 'json'")
        if not all(isinstance(key, ApiKey) for key in self.api_keys):
            raise SettingsError("api_keys must contain ApiKey entries")
        if len({key.sha256 for key in self.api_keys}) != len(self.api_keys):
            raise SettingsError("duplicate API key hash")

    @classmethod
    def from_env(
        cls,
        *,
        workspace: Path | str | None,
        config_path: Path | str | None,
        environ: Mapping[str, str] | None = None,
        require_keys: bool = True,
    ) -> ApiSettings:
        """Build settings from paths plus ``WATCHLIST_API_*`` environment variables."""
        env = os.environ if environ is None else environ
        keys = _load_keys(env)
        if require_keys and not keys:
            raise SettingsError(
                f"no API keys configured; set {ENV_KEYS} or {ENV_KEYS_FILE}"
            )
        return cls(
            workspace=None if workspace is None else Path(workspace),
            config_path=None if config_path is None else Path(config_path),
            api_keys=keys,
            write_queue_max=_int_env(env, ENV_QUEUE, DEFAULT_WRITE_QUEUE_MAX),
            write_wait_seconds=_float_env(env, ENV_WAIT, DEFAULT_WRITE_WAIT_SECONDS),
            max_in_flight=_int_env(env, ENV_IN_FLIGHT, DEFAULT_MAX_IN_FLIGHT),
            trace_path=_path_env(env, ENV_TRACE_PATH),
            body_timeout_seconds=_float_env(
                env, ENV_BODY_TIMEOUT, DEFAULT_BODY_TIMEOUT_SECONDS
            ),
            log_format=env.get(ENV_LOG_FORMAT, "").strip().lower() or "text",
        )


def _load_keys(env: Mapping[str, str]) -> tuple[ApiKey, ...]:
    inline = env.get(ENV_KEYS, "").strip()
    path = env.get(ENV_KEYS_FILE, "").strip()
    if inline and path:
        raise SettingsError(f"set only one of {ENV_KEYS} and {ENV_KEYS_FILE}")
    try:
        if inline:
            return parse_key_entries(re.split(r"[,;\r\n]+", inline))
        if path:
            try:
                # utf-8-sig: Windows PowerShell 5 writes a byte-order mark.
                text = Path(path).read_text(encoding="utf-8-sig")
            except OSError as exc:
                raise SettingsError(
                    f"cannot read {ENV_KEYS_FILE}: {exc.strerror or type(exc).__name__}"
                ) from None
            return parse_key_entries(text.splitlines())
    except AuthConfigError as exc:
        raise SettingsError(str(exc)) from None
    return ()


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not re.fullmatch(r"[0-9]{1,6}", raw):
        raise SettingsError(f"{name} must be a nonnegative integer")
    return int(raw)


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number") from None


def _path_env(env: Mapping[str, str], name: str) -> Path | None:
    raw = env.get(name, "").strip()
    return Path(raw) if raw else None
