"""Local, standard-library-only observability for the research review interfaces.

What this module provides (no network, no collector, no third-party package):

* **Redaction policy** (``Redactor``), applied to every structured log field and
  span attribute:

  - *Secret fields* (``authorization``, ``cookie``, ``password``, ``secret``,
    ``token`` and any key ending in ``key``/``keys``/``token``/``secret``) are
    replaced by ``"[redacted]"``. Their values are never hashed, because a hash
    of a low-entropy secret can be guessed.
  - *Personal or free-text fields* (any key with a ``_``/``-``/``.`` part that
    contains ``name``, ``alias``, ``reason``, ``quer``, ``text``, ``note``,
    ``comment`` or ``body``: ``display_name``, ``matched_alias``,
    ``cancel_reason``, ``query_text`` ...) are replaced by
    ``"hmac-sha256:<16 hex>;chars=<n>"``:
    a keyed fingerprint that lets an operator see that two records carry the
    same text inside one process run without storing the text. The key is
    random per process unless one is injected (tests), so fingerprints cannot
    be correlated across restarts or reversed with a dictionary.
  - *Every other string* has ``Bearer <token>`` and ``api_key=``/``x-api-key:``/
    ``token=``/``secret=``/``password=`` values replaced, control,
    format, bidi and line-separator characters escaped as ``\\uXXXX`` (so a
    newline in a name cannot forge a second log line) and is truncated to
    ``MAX_TEXT_CHARS`` code points.

* **Request IDs**: an incoming ``X-Request-ID`` is accepted only when it matches
  ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``; otherwise a new random ID is generated.
  The ID is echoed in the response, attached to log records and used as the
  trace ID of every span of the request. It is a correlation label only and is
  unrelated to the review service's idempotency ``request_id`` body field.

* **Spans** (``Tracer``): one JSON line per finished operation with the
  operation name, duration, outcome code (``ok`` or a stable error code) and
  redacted attributes, appended to a configurable file. A failing sink (full
  disk, read-only directory, missing directory) never fails the operation: the
  record is dropped and counted in ``watchlist_trace_records_dropped_total``.
  Instrumentation never feeds back into review decisions.

* **Metrics** (``MetricsRegistry``): in-process counters, gauges and histograms
  rendered in the Prometheus text exposition format (version 0.0.4). Label
  cardinality is bounded per metric; overflowing label sets are folded into
  one ``_overflow`` series.

* **JSON logging** (``JsonFormatter``, ``LogContextFilter``,
  ``configure_json_logging``): one JSON object per line, ASCII-only, with the
  request ID and redacted ``fields``. Tracebacks are not written; only the
  exception type name.

* **Service instrumentation** (``InstrumentedService``): a transparent proxy
  around the shared v2 ``ReviewService`` that records one span per operation.
  It forwards every call unchanged and never alters arguments or results.

Everything here is research tooling for a prototype over fictional data. A
metric or span describes software behaviour, never identity, wrongdoing,
sanctions status or clearance.
"""

from __future__ import annotations

from bisect import bisect_left
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import datetime as _dt
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence, TextIO
import unicodedata
from uuid import uuid4

__all__ = [
    "DEFAULT_BUCKETS",
    "DEFAULT_TRACE_MAX_BYTES",
    "HASHED_FIELDS",
    "HASHED_KEY_STEMS",
    "MAX_TEXT_CHARS",
    "PROMETHEUS_CONTENT_TYPE",
    "REDACTED",
    "REQUEST_ID_HEADER",
    "SECRET_FIELDS",
    "Counter",
    "Gauge",
    "Histogram",
    "InstrumentedService",
    "JsonFormatter",
    "LogContextFilter",
    "MetricsRegistry",
    "Observability",
    "ObservabilityMiddleware",
    "Redactor",
    "SCOPE_REQUEST_ID",
    "Span",
    "Tracer",
    "accept_request_id",
    "configure_json_logging",
    "current_request_id",
    "install_log_filter",
    "new_request_id",
    "outcome_code",
    "read_trace",
    "request_id_scope",
]

REQUEST_ID_HEADER = "x-request-id"
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
REDACTED = "[redacted]"
MAX_TEXT_CHARS = 256
DEFAULT_TRACE_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_SERIES_LIMIT = 500
# Seconds; covers fast reads (ms) up to the default 30 s HTTP write wait.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

SECRET_FIELDS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "password",
        "passwd",
        "secret",
        "token",
        "bearer",
        "credentials",
        "api_key",
        "apikey",
        "x-api-key",
    }
)
_SECRET_SUFFIXES = ("key", "keys", "token", "tokens", "secret", "password")
HASHED_FIELDS = frozenset(
    {
        "name",
        "names",
        "query",
        "normalized_name",
        "original_text",
        "alias",
        "aliases",
        "reason",
        "notes",
        "note",
        "text",
        "body",
        "comment",
    }
)
# A field is also hashed when any ``_``/``-``/``.``-separated part of its
# lower-cased key *contains* one of these stems, so ``display_name``,
# ``matched_alias``, ``original_text``, ``cancel_reason``, ``query_text`` and
# ``reviewer_notes`` are covered as well as the exact names above. Matching is
# deliberately broad: hashing an operational string (``hostname``) costs a
# little readability, while missing a personal one would leak it.
HASHED_KEY_STEMS: tuple[str, ...] = (
    "name",
    "alias",
    "reason",
    "quer",
    "text",
    "note",
    "comment",
    "body",
)
_KEY_PARTS = re.compile(r"[^a-z0-9]+")

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[^\s,;\"']+")
# ``api_key=...``, ``x-api-key: ...``, ``token=...`` and similar inside free
# text (query strings, exception messages). A value starting with ``%`` or ``{``
# is a format placeholder and is left alone so log format strings stay valid.
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b((?:x[-_])?api[-_]?keys?|access[-_]?token|refresh[-_]?token|token"
    r"|secret|password|passwd)(\s*[=:]\s*)(?![%{])[^\s,;&\"']+"
)
# Explicit bidi controls (also category Cf, listed for clarity and for Python
# versions whose Unicode tables might classify them differently).
_BIDI = frozenset(
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
)
_METRIC_NAME = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

_request_id: ContextVar[str | None] = ContextVar("watchlist_request_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("watchlist_span_id", default=None)

_internal_log = logging.getLogger("watchlist.observability")


# ---------------------------------------------------------------------------
# Request IDs.
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    """A fresh random request ID (32 lowercase hex characters)."""
    return uuid4().hex


def accept_request_id(value: str | bytes | None) -> str | None:
    """Return ``value`` if it is a safe request ID, else ``None``.

    Only ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}`` is accepted, so a header value with
    CR/LF, spaces, quotes, non-ASCII or bidi characters is never echoed into a
    response header, a log line or a trace file.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    return value if _REQUEST_ID.fullmatch(value) else None


def current_request_id() -> str | None:
    """The request ID of the running request (context variable), if any."""
    return _request_id.get()


@contextmanager
def request_id_scope(request_id: str | None = None) -> Iterator[str]:
    """Bind a request ID for code running outside the HTTP middleware (scripts)."""
    value = accept_request_id(request_id) or new_request_id()
    token = _request_id.set(value)
    try:
        yield value
    finally:
        _request_id.reset(token)


def outcome_code(exc: BaseException | None) -> str:
    """Stable outcome code: ``ok``, an error's own ``code`` or ``unexpected_error``."""
    if exc is None:
        return "ok"
    code = getattr(exc, "code", None)
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code
    return "unexpected_error"


# ---------------------------------------------------------------------------
# Redaction.
# ---------------------------------------------------------------------------


def _escape_char(char: str) -> str:
    return f"\\u{ord(char):04x}" if ord(char) <= 0xFFFF else f"\\U{ord(char):08x}"


def _is_unsafe(char: str) -> bool:
    return char in _BIDI or unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp", "Cs")


class Redactor:
    """Apply the documented redaction policy to text, fields and attributes.

    ``key`` is the HMAC key for fingerprints of personal/free-text fields; by
    default a random 32-byte key generated once per ``Redactor``.
    """

    def __init__(self, key: bytes | None = None, *, max_chars: int = MAX_TEXT_CHARS):
        if key is not None and (not isinstance(key, bytes) or not key):
            raise ValueError("redaction key must be non-empty bytes")
        if type(max_chars) is not int or not 16 <= max_chars <= 65536:
            raise ValueError("max_chars must be an integer from 16 to 65536")
        self._key = key if key is not None else secrets.token_bytes(32)
        self.max_chars = max_chars

    def fingerprint(self, text: str) -> str:
        """Keyed, truncated fingerprint plus length; never the text itself."""
        mac = hmac.new(self._key, text.encode("utf-8", "surrogatepass"), hashlib.sha256)
        return f"hmac-sha256:{mac.hexdigest()[:16]};chars={len(text)}"

    def scrub(self, text: str, *, truncate: bool = True) -> str:
        """Remove bearer and ``key=value`` credentials, escape unsafe characters, then truncate."""
        text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
        text = _KEY_VALUE_SECRET.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text
        )
        if any(_is_unsafe(char) for char in text):
            text = "".join(_escape_char(c) if _is_unsafe(c) else c for c in text)
        if truncate and len(text) > self.max_chars:
            extra = len(text) - self.max_chars
            text = f"{text[: self.max_chars]}...[+{extra} chars]"
        return text

    @staticmethod
    def is_secret_field(name: str) -> bool:
        lowered = name.lower()
        return lowered in SECRET_FIELDS or lowered.endswith(_SECRET_SUFFIXES)

    @staticmethod
    def is_hashed_field(name: str) -> bool:
        """Personal/free-text field: exact name or a key part containing a stem."""
        lowered = name.lower()
        if lowered in HASHED_FIELDS:
            return True
        return any(
            stem in part
            for part in _KEY_PARTS.split(lowered)
            if part
            for stem in HASHED_KEY_STEMS
        )

    def field(self, name: str, value: Any, *, _depth: int = 0) -> Any:
        """Redact one named value according to the policy."""
        if self.is_secret_field(name):
            return REDACTED
        if self.is_hashed_field(name):
            if value is None:
                return None
            if isinstance(value, str):
                return self.fingerprint(value)
            if isinstance(value, (list, tuple)):
                return [self.fingerprint(str(item)) for item in list(value)[:20]]
            return self.fingerprint(str(value))
        return self.value(value, _depth=_depth)

    def value(self, value: Any, *, _depth: int = 0) -> Any:
        """Redact an unnamed value (strings scrubbed, containers recursed)."""
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, str):
            return self.scrub(value)
        if _depth >= 4:
            return "[truncated]"
        if isinstance(value, Mapping):
            return {
                self.scrub(str(key)): self.field(str(key), item, _depth=_depth + 1)
                for key, item in list(value.items())[:50]
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self.value(item, _depth=_depth + 1) for item in list(value)[:50]]
        if isinstance(value, Path):
            # Paths can reveal local user names; keep only the final component.
            return self.scrub(value.name)
        return self.scrub(type(value).__name__)

    def fields(self, mapping: Mapping[str, Any] | None) -> dict[str, Any]:
        """Redact a mapping of named fields."""
        if not mapping:
            return {}
        return {
            self.scrub(str(key)): self.field(str(key), value)
            for key, value in list(mapping.items())[:50]
        }


# ---------------------------------------------------------------------------
# Logging.
# ---------------------------------------------------------------------------


class LogContextFilter(logging.Filter):
    """Attach the request ID and neutralize interpolated arguments.

    It never drops a record. String arguments (and string values of a mapping
    argument) are scrubbed, so ``logger.info("x=%s", user_text)`` cannot write
    a raw newline, bidi control or bearer token, whatever formatter is used.
    """

    def __init__(self, redactor: Redactor | None = None):
        super().__init__()
        self.redactor = redactor or Redactor()

    def _arg(self, value: Any) -> Any:
        return self.redactor.scrub(value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", None):
            record.request_id = current_request_id() or "-"
        if isinstance(record.msg, str):
            # Escape only: truncating a format string could break its arguments.
            record.msg = self.redactor.scrub(record.msg, truncate=False)
        args = record.args
        if isinstance(args, tuple):
            record.args = tuple(self._arg(arg) for arg in args)
        elif isinstance(args, Mapping):
            record.args = {key: self._arg(value) for key, value in args.items()}
        return True


class JsonFormatter(logging.Formatter):
    """One ASCII-only JSON object per record; ``fields`` extra is redacted."""

    def __init__(self, redactor: Redactor | None = None):
        super().__init__()
        self.redactor = redactor or Redactor()

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must not break logging
            message = str(record.msg)
        created = _dt.datetime.fromtimestamp(record.created, _dt.UTC)
        payload: dict[str, Any] = {
            "ts": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": self.redactor.scrub(message),
            "request_id": getattr(record, "request_id", None)
            or current_request_id()
            or "-",
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, Mapping):
            payload["fields"] = self.redactor.fields(extra)
        if record.exc_info and record.exc_info[0] is not None:
            # Type only: traceback text can hold paths, names or secrets.
            payload["exc_type"] = record.exc_info[0].__name__
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda value: type(value).__name__,
        )


_JSON_HANDLER_MARK = "_watchlist_json_handler"


def install_log_filter(
    logger_name: str = "watchlist.api", redactor: Redactor | None = None
) -> LogContextFilter:
    """Add one ``LogContextFilter`` to a logger (idempotent); return it."""
    logger = logging.getLogger(logger_name)
    for existing in logger.filters:
        if isinstance(existing, LogContextFilter):
            return existing
    log_filter = LogContextFilter(redactor)
    logger.addFilter(log_filter)
    return log_filter


def configure_json_logging(
    stream: TextIO | None = None,
    *,
    level: int = logging.INFO,
    logger_name: str = "watchlist",
    redactor: Redactor | None = None,
) -> logging.Handler:
    """Send ``logger_name`` records as JSON lines to ``stream`` (default stderr).

    Idempotent: a handler installed by an earlier call is replaced, never
    duplicated. The filter sits on the handler, so records from every child
    logger are neutralized too.
    """
    import sys

    logger = logging.getLogger(logger_name)
    for existing in list(logger.handlers):
        if getattr(existing, _JSON_HANDLER_MARK, False):
            logger.removeHandler(existing)
            existing.close()
    redactor = redactor or Redactor()
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    setattr(handler, _JSON_HANDLER_MARK, True)
    handler.setFormatter(JsonFormatter(redactor))
    handler.addFilter(LogContextFilter(redactor))
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler


# ---------------------------------------------------------------------------
# Metrics.
# ---------------------------------------------------------------------------


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_number(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def _labels_text(names: Sequence[str], values: Sequence[str]) -> str:
    if not names:
        return ""
    pairs = ",".join(
        f'{name}="{_escape_label(value)}"' for name, value in zip(names, values)
    )
    return "{" + pairs + "}"


class _Metric:
    kind = "untyped"

    def __init__(
        self,
        name: str,
        help_text: str,
        labelnames: Sequence[str] = (),
        *,
        series_limit: int = DEFAULT_SERIES_LIMIT,
    ):
        if not _METRIC_NAME.fullmatch(name):
            raise ValueError(f"invalid metric name {name!r}")
        for label in labelnames:
            if not _LABEL_NAME.fullmatch(label) or label.startswith("__"):
                raise ValueError(f"invalid label name {label!r}")
        self.name = name
        self.help = help_text.replace("\\", "\\\\").replace("\n", " ")
        self.labelnames = tuple(labelnames)
        self._limit = series_limit
        self._lock = threading.Lock()

    def _key(self, labels: Mapping[str, Any], existing: Mapping) -> tuple[str, ...]:
        if set(labels) != set(self.labelnames):
            raise ValueError(f"{self.name} needs labels {self.labelnames}")
        key = tuple(str(labels[name])[:120] for name in self.labelnames)
        if key not in existing and len(existing) >= self._limit:
            return tuple("_overflow" for _ in self.labelnames)
        return key


class Counter(_Metric):
    """Monotonic counter with optional labels."""

    kind = "counter"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: Any) -> None:
        if amount < 0 or not math.isfinite(amount):
            raise ValueError("counter increments must be finite and nonnegative")
        with self._lock:
            key = self._key(labels, self._values)
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: Any) -> float:
        key = tuple(str(labels.get(name, ""))[:120] for name in self.labelnames)
        with self._lock:
            return self._values.get(key, 0.0)

    def total(self) -> float:
        with self._lock:
            return sum(self._values.values())

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [
            f"{self.name}{_labels_text(self.labelnames, key)} {_format_number(value)}"
            for key, value in items
        ]


class Gauge(_Metric):
    """Settable gauge, or one read from ``callback`` at render time (no labels)."""

    kind = "gauge"

    def __init__(
        self, *args: Any, callback: Callable[[], float] | None = None, **kwargs: Any
    ):
        super().__init__(*args, **kwargs)
        if callback is not None and self.labelnames:
            raise ValueError("callback gauges cannot have labels")
        self._callback = callback
        self._values: dict[tuple[str, ...], float] = {}

    def set(self, value: float, **labels: Any) -> None:
        with self._lock:
            self._values[self._key(labels, self._values)] = float(value)

    def add(self, amount: float, **labels: Any) -> None:
        with self._lock:
            key = self._key(labels, self._values)
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, **labels: Any) -> float:
        if self._callback is not None:
            return float(self._callback())
        key = tuple(str(labels.get(name, ""))[:120] for name in self.labelnames)
        with self._lock:
            return self._values.get(key, 0.0)

    def render(self) -> list[str]:
        if self._callback is not None:
            try:
                value = float(self._callback())
            except Exception:  # a broken probe must not break /metrics
                return []
            return [f"{self.name} {_format_number(value)}"]
        with self._lock:
            items = sorted(self._values.items())
        return [
            f"{self.name}{_labels_text(self.labelnames, key)} {_format_number(value)}"
            for key, value in items
        ]


@dataclass
class _HistogramSeries:
    counts: list[int]
    total: float = 0.0
    count: int = 0


class Histogram(_Metric):
    """Cumulative-bucket histogram (Prometheus semantics: ``le`` is inclusive)."""

    kind = "histogram"

    def __init__(
        self,
        *args: Any,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        if "le" in self.labelnames:
            raise ValueError("'le' is reserved for histogram buckets")
        bounds = tuple(float(b) for b in buckets)
        if not bounds or list(bounds) != sorted(set(bounds)):
            raise ValueError("buckets must be strictly increasing")
        if not all(math.isfinite(b) for b in bounds):
            raise ValueError("buckets must be finite; +Inf is implicit")
        self.buckets = bounds
        self._series: dict[tuple[str, ...], _HistogramSeries] = {}

    def observe(self, value: float, **labels: Any) -> None:
        if not math.isfinite(value):
            return
        index = bisect_left(self.buckets, value)
        with self._lock:
            key = self._key(labels, self._series)
            series = self._series.get(key)
            if series is None:
                series = self._series[key] = _HistogramSeries(
                    [0] * (len(self.buckets) + 1)
                )
            series.counts[index] += 1
            series.total += value
            series.count += 1

    def count(self, **labels: Any) -> int:
        key = tuple(str(labels.get(name, ""))[:120] for name in self.labelnames)
        with self._lock:
            series = self._series.get(key)
            return 0 if series is None else series.count

    def render(self) -> list[str]:
        with self._lock:
            items = sorted(
                (key, list(s.counts), s.total, s.count)
                for key, s in self._series.items()
            )
        lines = []
        names = (*self.labelnames, "le")
        for key, counts, total, count in items:
            running = 0
            for bound, bucket in zip((*self.buckets, math.inf), counts):
                running += bucket
                labels = _labels_text(names, (*key, _format_number(bound)))
                lines.append(f"{self.name}_bucket{labels} {running}")
            base = _labels_text(self.labelnames, key)
            lines.append(f"{self.name}_sum{base} {_format_number(total)}")
            lines.append(f"{self.name}_count{base} {count}")
        return lines


class MetricsRegistry:
    """A per-application set of metrics rendered in Prometheus text format."""

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _register(self, metric: _Metric) -> Any:
        with self._lock:
            if metric.name in self._metrics:
                raise ValueError(f"metric {metric.name} already registered")
            self._metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help_text: str, labels: Sequence[str] = ()) -> Counter:
        return self._register(Counter(name, help_text, labels))

    def gauge(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        *,
        callback: Callable[[], float] | None = None,
    ) -> Gauge:
        return self._register(Gauge(name, help_text, labels, callback=callback))

    def histogram(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        *,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        return self._register(Histogram(name, help_text, labels, buckets=buckets))

    def get(self, name: str) -> _Metric | None:
        with self._lock:
            return self._metrics.get(name)

    def render(self) -> str:
        """The exposition text: ``# HELP``/``# TYPE`` then samples, LF line ends."""
        with self._lock:
            metrics = sorted(self._metrics.values(), key=lambda m: m.name)
        lines: list[str] = []
        for metric in metrics:
            lines.append(f"# HELP {metric.name} {metric.help}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            lines.extend(metric.render())  # type: ignore[attr-defined]
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Spans.
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """One in-flight operation; ``outcome`` defaults to ``ok`` or the error code."""

    operation: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    attrs: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None

    def set(self, key: str, value: Any) -> None:
        self.attrs[key] = value

    def set_outcome(self, code: str) -> None:
        self.outcome = code if _CODE.fullmatch(code or "") else "unexpected_error"


class Tracer:
    """Write one JSON line per finished span to ``path`` (or nowhere if ``None``).

    Writes are serialized by a lock and each record is one ``write`` call of one
    complete line. When the file would exceed ``max_bytes`` it is renamed to
    ``<name>.1`` (replacing an older one) and a new file is started. Any
    ``OSError`` drops the record, increments the drop counter and logs one
    warning per tracer; the traced operation itself is unaffected.
    """

    def __init__(
        self,
        path: Path | str | None,
        *,
        redactor: Redactor,
        metrics: MetricsRegistry,
        max_bytes: int = DEFAULT_TRACE_MAX_BYTES,
        clock: Callable[[], float] = time.perf_counter,
    ):
        if type(max_bytes) is not int or max_bytes < 4096:
            raise ValueError("max_bytes must be an integer of at least 4096")
        self.path = None if path is None else Path(path)
        self.redactor = redactor
        self.max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._warned = False
        self.written = metrics.counter(
            "watchlist_trace_records_written_total",
            "Span records appended to the local trace file.",
        )
        self.dropped = metrics.counter(
            "watchlist_trace_records_dropped_total",
            "Span records not written because the trace sink failed.",
        )
        self.span_total = metrics.counter(
            "watchlist_spans_total",
            "Finished operations by name and outcome code.",
            ("operation", "outcome"),
        )
        self.span_seconds = metrics.histogram(
            "watchlist_span_duration_seconds",
            "Duration of finished operations.",
            ("operation",),
        )

    @contextmanager
    def span(self, operation: str, **attrs: Any) -> Iterator[Span]:
        """Time a block; the outcome is ``ok`` or the raised error's code."""
        trace_id = current_request_id() or new_request_id()
        span = Span(operation, trace_id, uuid4().hex[:16], _span_id.get(), dict(attrs))
        token = _span_id.set(span.span_id)
        start = self._clock()
        wall = time.time()
        error: BaseException | None = None
        try:
            yield span
        except BaseException as exc:
            error = exc
            raise
        finally:
            _span_id.reset(token)
            duration = max(0.0, self._clock() - start)
            if span.outcome is None:
                span.outcome = outcome_code(error)
            self._finish(span, wall, duration)

    def _finish(self, span: Span, wall: float, duration: float) -> None:
        try:
            self.span_total.inc(operation=span.operation, outcome=span.outcome)
            self.span_seconds.observe(duration, operation=span.operation)
        except Exception:  # metrics must never break an operation
            pass
        if self.path is None:
            return
        record = {
            "ts": _dt.datetime.fromtimestamp(wall, _dt.UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "trace_id": span.trace_id,
            "span_id": span.span_id,
            "parent_span_id": span.parent_span_id,
            "operation": self.redactor.scrub(span.operation),
            "duration_ms": round(duration * 1000.0, 3),
            "outcome": span.outcome,
            "attrs": self.redactor.fields(span.attrs),
            "pid": os.getpid(),
        }
        line = (
            json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        try:
            self._append(line)
        except OSError as exc:
            self.dropped.inc()
            if not self._warned:
                self._warned = True
                _internal_log.warning(
                    "trace sink unavailable; dropping span records (errno=%s)",
                    getattr(exc, "errno", None),
                )
            return
        self.written.inc()

    def _append(self, line: str) -> None:
        assert self.path is not None
        data = line.encode("utf-8")
        with self._lock:
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(data) > self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with open(self.path, "ab") as handle:
                handle.write(data)


def read_trace(path: Path | str) -> list[dict[str, Any]]:
    """Parse a trace file written by ``Tracer`` (test and drill helper)."""
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# The bundle used by interfaces.
# ---------------------------------------------------------------------------


class Observability:
    """Metrics registry, tracer and redactor for one application instance."""

    def __init__(
        self,
        *,
        trace_path: Path | str | None = None,
        trace_max_bytes: int = DEFAULT_TRACE_MAX_BYTES,
        redactor: Redactor | None = None,
    ):
        self.redactor = redactor or Redactor()
        self.metrics = MetricsRegistry()
        self.tracer = Tracer(
            trace_path,
            redactor=self.redactor,
            metrics=self.metrics,
            max_bytes=trace_max_bytes,
        )
        self.http_requests = self.metrics.counter(
            "watchlist_http_requests_total",
            "HTTP requests by method, route template, status and outcome code.",
            ("method", "route", "status", "code"),
        )
        self.http_seconds = self.metrics.histogram(
            "watchlist_http_request_duration_seconds",
            "HTTP request duration by method and route template.",
            ("method", "route"),
        )
        self.http_in_flight = self.metrics.gauge(
            "watchlist_http_requests_in_flight",
            "HTTP requests currently being handled.",
        )

    def span(self, operation: str, **attrs: Any):
        """Shortcut for ``self.tracer.span``."""
        return self.tracer.span(operation, **attrs)

    def instrument(self, service: Any) -> InstrumentedService:
        """Wrap a ``ReviewService`` so each operation records a span."""
        if isinstance(service, InstrumentedService):
            return service
        return InstrumentedService(service, self)


class InstrumentedService:
    """Transparent proxy over a ``ReviewService`` that records operation spans.

    Only names in ``OPERATIONS`` are wrapped; every other attribute is
    forwarded as is. Arguments and results are passed through untouched; span
    attributes hold case IDs, statuses and fingerprints, never names or reasons.
    """

    OPERATIONS = frozenset(
        {
            "start_case",
            "get_case",
            "list_cases",
            "submit_review",
            "recover",
            "cancel_case",
            "audit",
            "verify_audit",
        }
    )

    def __init__(self, service: Any, observability: Observability):
        object.__setattr__(self, "_wl_service", service)
        object.__setattr__(self, "_wl_obs", observability)

    @property
    def wrapped(self) -> Any:
        """The underlying service."""
        return self._wl_service

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._wl_service, name)
        if name in self.OPERATIONS and callable(value):
            return self._wrap(name, value)
        return value

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._wl_service, name, value)

    def _wrap(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        obs = self._wl_obs

        def call(*args: Any, **kwargs: Any) -> Any:
            attrs = _operation_attrs(name, args, kwargs, obs.redactor)
            with obs.span(f"review.{name}", **attrs) as span:
                result = method(*args, **kwargs)
                status = getattr(result, "status", None)
                if isinstance(status, str):
                    span.set("status", status)
                header = getattr(result, "header", None)
                case_id = getattr(header, "case_id", None)
                if isinstance(case_id, str) and "case_id" not in span.attrs:
                    span.set("case_id", _case_attr(case_id, obs.redactor))
                if isinstance(result, (list, tuple)):
                    span.set("items", len(result))
                return result

        call.__name__ = name
        call.__doc__ = getattr(method, "__doc__", None)
        return call


def _case_attr(value: Any, redactor: Redactor) -> str:
    """A UUID case ID is kept; anything else (e.g. an attack string) is fingerprinted."""
    if isinstance(value, str) and _UUID.fullmatch(value):
        return value.lower()
    return "invalid:" + redactor.fingerprint(str(value))


def _operation_attrs(
    name: str, args: tuple[Any, ...], kwargs: Mapping[str, Any], redactor: Redactor
) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    if name == "start_case":
        if args:
            attrs["name"] = args[0] if isinstance(args[0], str) else None
        for key in ("origin", "actor", "request_id"):
            if kwargs.get(key) is not None:
                attrs[key] = kwargs[key]
        return attrs
    if name == "list_cases":
        if "origin" in kwargs:
            attrs["origin"] = kwargs["origin"]
        return attrs
    case_id = args[0] if args else kwargs.get("case_id")
    if case_id is not None:
        attrs["case_id"] = _case_attr(case_id, redactor)
    if name == "submit_review":
        form = args[1] if len(args) > 1 else kwargs.get("submission")
        submission_id = (
            form.get("submission_id")
            if isinstance(form, Mapping)
            else getattr(form, "submission_id", None)
        )
        if submission_id is not None:
            attrs["submission_id"] = submission_id
    if name == "cancel_case" and "actor" in kwargs:
        attrs["actor"] = kwargs["actor"]
    return attrs


# ---------------------------------------------------------------------------
# ASGI middleware: request IDs, request metrics and the request span.
# ---------------------------------------------------------------------------

SCOPE_REQUEST_ID = "watchlist.request_id"
_ERROR_BODY_CAP = 16384
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _route_template(scope: Mapping[str, Any]) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else "unrouted"


def _code_from_body(body: bytes) -> str | None:
    try:
        code = json.loads(body.decode("utf-8")).get("code")
    except (ValueError, AttributeError, UnicodeDecodeError):
        return None
    return code if isinstance(code, str) and _CODE.fullmatch(code) else None


class ObservabilityMiddleware:
    """Pure ASGI middleware: request ID, one request span and HTTP metrics.

    Sets ``X-Request-ID`` on every response it sends (replacing any other
    value), stores the ID in ``scope["watchlist.request_id"]`` for handlers
    that answer outside this middleware, and labels metrics with the matched
    route *template* (``/v2/cases/{case_id}``), never the raw path, so attack
    strings in URLs cannot create unbounded metric series or reach logs.
    """

    def __init__(self, app: Any, observability: Observability):
        self.app = app
        self.obs = observability

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Never echo or authenticate a caller-supplied correlation value.
        # Otherwise a token in X-Request-ID leaks into traces, or the choice
        # to replace it reveals whether a guessed token authenticates.
        request_id = new_request_id()
        scope[SCOPE_REQUEST_ID] = request_id
        method = scope.get("method", "GET")
        method = method if method in _METHODS else "OTHER"
        state = {"status": 500, "body": bytearray(), "capture": False}
        header = (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("ascii"))

        async def wrapped(message: Any) -> None:
            if message["type"] == "http.response.start":
                state["status"] = int(message.get("status", 500))
                state["capture"] = state["status"] >= 400
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != header[0]
                ]
                headers.append(header)
                message = {**message, "headers": headers}
            elif message["type"] == "http.response.body" and state["capture"]:
                body = state["body"]
                if len(body) < _ERROR_BODY_CAP:
                    body.extend(message.get("body", b"")[: _ERROR_BODY_CAP - len(body)])
            await send(message)

        token = _request_id.set(request_id)
        self.obs.http_in_flight.add(1)
        start = time.perf_counter()
        try:
            with self.obs.span("http.request", method=method) as span:
                error: BaseException | None = None
                try:
                    await self.app(scope, receive, wrapped)
                except BaseException as exc:
                    error = exc
                    state["status"] = 500
                    raise
                finally:
                    route = _route_template(scope)
                    status = int(state["status"])
                    if error is not None:
                        code = "internal_error"
                    elif status < 400:
                        code = "ok"
                    else:
                        code = _code_from_body(bytes(state["body"])) or f"http_{status}"
                    span.operation = f"http {method} {route}"
                    span.set("route", route)
                    span.set("status", status)
                    span.set_outcome(code)
                    self.obs.http_requests.inc(
                        method=method, route=route, status=str(status), code=code
                    )
                    self.obs.http_seconds.observe(
                        time.perf_counter() - start, method=method, route=route
                    )
        finally:
            self.obs.http_in_flight.add(-1)
            _request_id.reset(token)
