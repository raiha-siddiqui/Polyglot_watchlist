"""FastAPI application over the shared v2 ``ReviewService``.

The API adds transport concerns only: authentication, request bounds, a
bounded write queue, error mapping and security headers. Every review
operation is a direct ``ReviewService`` call; nothing here selects, infers or
changes a review outcome. Outputs are possible record matches for research and
human review, never identity, wrongdoing, sanctions status or clearance.

Write model: one worker thread runs all mutations of this process in order
(retrieval included); at most ``write_queue_max`` more wait behind it and the
next one gets ``429 queue_full``. The workspace lock still serializes writers
across processes, and the service's ``UNIQUE`` constraints guarantee one review
per case. Reads run in the server thread pool and take the workspace lock only
briefly. No lock is held between requests.

Step 27 hooks (``watchlist.observability``): every response carries an
``X-Request-ID``; each request and each service operation records a redacted
span and in-process metrics; ``GET /metrics`` (viewer) exposes them in the
Prometheus text format; ``GET /ready`` checks that the workspace opens
read-only (one lock attempt, coalesced and cached for ``READY_CACHE_SECONDS``;
a lock held by a running operation reports ready with check ``busy``); more
than ``max_in_flight`` concurrent requests whose bodies have fully arrived get
``429 too_many_requests`` instead of piling up, and a body that does not arrive
within ``body_timeout_seconds`` gets ``408 request_timeout``. Unexpected
exceptions are answered with the generic 500 and stopped inside the app, so the
ASGI server never logs their message or traceback. None of this affects review
logic.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
import logging
import re
import threading
import time
from typing import Annotated, Any, Callable, Sequence, TypeVar

from fastapi import Depends, FastAPI, Path as PathParam, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from watchlist.api.auth import KeyStore, Principal, Role
from watchlist.api.errors import (
    SECURITY_HEADERS,
    ApiError,
    error_body,
    review_error_body,
    review_error_status,
)
from watchlist.api.schemas import (
    AuditResponse,
    CancelRequest,
    CaseListResponse,
    CaseResponse,
    EntityResponse,
    ErrorResponse,
    HealthResponse,
    Origin,
    ReviewRequest,
    StartCaseRequest,
    VerificationResponse,
)
from watchlist.api.settings import PAGE_MAX, ApiSettings
from watchlist.entities import Entity
from watchlist.observability import (
    PROMETHEUS_CONTENT_TYPE,
    REQUEST_ID_HEADER,
    SCOPE_REQUEST_ID,
    Observability,
    ObservabilityMiddleware,
    configure_json_logging,
    install_log_filter,
)
from watchlist.review.contracts import (
    LIMITATION,
    AuditAnchor,
    CaseView,
    ContextProvider,
    ReviewError,
    ReviewSubmission,
    StaleSubmissionError,
    digest,
    loads_strict,
    uuid_text,
)

__all__ = [
    "API_VERSION",
    "ReadinessProbe",
    "WriteQueue",
    "case_revision",
    "create_app",
    "openapi_document",
]

API_VERSION = "review-api-v2.0"
logger = logging.getLogger("watchlist.api")
T = TypeVar("T")
# Readiness tries the workspace lock exactly once (no polling): a busy lock
# means another operation is running, which is not a readiness failure.
READY_LOCK_TIMEOUT_SECONDS = 0.0
# A healthy readiness result ("ok"/"busy") is reused for this long, so probe
# floods cannot contend for the workspace lock. Failures are never cached.
READY_CACHE_SECONDS = 1.0
# Probes that must answer even when the server is saturated.
_LIMIT_EXEMPT_PATHS = frozenset({"/health"})

_ENTITY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = r"^[0-9a-f]{64}$"
_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse, "description": text}
    for status, text in (
        (401, "Missing or invalid API key"),
        (403, "Role does not permit this operation"),
        (404, "Unknown case, record or route"),
        (409, "Stale, duplicate, conflicting or not-allowed-now request"),
        (413, "Request body larger than the limit"),
        (422, "Invalid input"),
        (429, "Write queue is full; retry later"),
        (500, "Internal error (no details are returned)"),
        (503, "Workspace busy or a component unavailable; retry later"),
    )
}


def case_revision(view: CaseView) -> str:
    """Opaque revision token: SHA-256 of the canonical case view.

    Any durable change to the case (packet saved, review accepted, final
    audit, cancellation) changes it, so a form built from an older response is
    refused as stale.
    """
    return digest(view.to_dict())


# ---------------------------------------------------------------------------
# Bounded single-worker write queue.
# ---------------------------------------------------------------------------


class WriteQueue:
    """Run mutations one at a time; refuse when too many are already waiting.

    ``max_waiting`` counts operations queued behind the running one. Admission
    is decided synchronously, so a refused request has no effect at all.
    """

    def __init__(self, max_waiting: int):
        self._max = max_waiting
        self._lock = threading.Lock()
        self._outstanding = 0
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="watchlist-api-write"
        )

    @property
    def outstanding(self) -> int:
        """Running plus waiting operations."""
        with self._lock:
            return self._outstanding

    def submit(self, fn: Callable[[], T]) -> Future[T]:
        with self._lock:
            if self._outstanding >= self._max + 1:
                raise ApiError(
                    429,
                    "queue_full",
                    "Too many write operations are waiting; retry later.",
                    retryable=True,
                    details={"limit": self._max, "retry_after_seconds": 1},
                    headers={"retry-after": "1"},
                )
            self._outstanding += 1

        def run() -> T:
            try:
                return fn()
            finally:
                # Release before Future publishes the result to its waiters.
                self._release(None)

        try:
            future = self._executor.submit(run)
        except RuntimeError:
            self._release(None)
            raise ApiError(
                503, "service_unavailable", "The API is shutting down.", retryable=True
            ) from None
        future.add_done_callback(self._release_cancelled)
        return future

    def _release_cancelled(self, future: Future) -> None:
        # A queued callable cancelled before starting never runs its finally.
        if future.cancelled():
            self._release(None)

    def _release(self, _future: object) -> None:
        with self._lock:
            self._outstanding -= 1

    def shutdown(self) -> None:
        """Finish running and queued work, then stop the worker."""
        self._executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# Runtime state attached to the app.
# ---------------------------------------------------------------------------


@dataclass
class _Runtime:
    settings: ApiSettings
    service: Any  # watchlist.review.service.ReviewService
    keys: KeyStore
    writes: WriteQueue
    entities: dict[str, Entity]
    dataset: str
    reference_sha256: str
    observability: Observability | None = None

    async def write(self, fn: Callable[[], T], *, retry_safe: bool = True) -> T:
        """Queue one mutation and wait at most ``write_wait_seconds`` for it.

        The wait is the HTTP timeout only. An operation that outlives it keeps
        running to completion (never cancelled half-way) and the client gets
        ``503 operation_pending``. When ``retry_safe`` (the operation carries a
        ``request_id``/``submission_id`` or is naturally idempotent) that error
        is retryable. Otherwise (a case start without ``request_id``) a blind
        retry could create a second case, so the error is not retryable and
        names ``list_cases`` as the next action, matching the service's own
        contract for that path.
        """
        # Run in a copy of the caller's context so the request ID and parent
        # span reach the worker thread (observability only).
        future = self.writes.submit(partial(contextvars.copy_context().run, fn))
        waiting = asyncio.wrap_future(future)
        waiting.add_done_callback(_consume)
        try:
            return await asyncio.wait_for(
                asyncio.shield(waiting), self.settings.write_wait_seconds
            )
        except TimeoutError:
            if waiting.done():
                raise
            if not retry_safe:
                raise ApiError(
                    503,
                    "operation_pending",
                    "The case start is still running and may create a case. Do not "
                    "retry it blindly: list cases to find it (recover it if it "
                    "stays incomplete), and send a request_id to make retries safe.",
                    retryable=False,
                    details={"next_action": "list_cases"},
                ) from None
            raise ApiError(
                503,
                "operation_pending",
                "The operation is still running; read the case status or retry "
                "with the same request_id/submission_id.",
                retryable=True,
                details={"retry_after_seconds": 1},
                headers={"retry-after": "1"},
            ) from None


def _consume(future: asyncio.Future) -> None:
    """Mark a background result as retrieved (avoids 'never retrieved' noise)."""
    if not future.cancelled():
        future.exception()


def _runtime(request: Request) -> _Runtime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise ApiError(
            503, "service_unavailable", "The review service is not configured."
        )
    return runtime


# ---------------------------------------------------------------------------
# Authentication dependencies.
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="ApiKey",
    description="Bearer API key issued by the operator; its actor label is the "
    "attributed identity for every write.",
)


def _require(role: Role) -> Callable[..., Principal]:
    def dependency(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Principal:
        runtime = _runtime(request)
        principal = runtime.keys.authenticate(
            None if credentials is None else credentials.credentials
        )
        if principal is None:
            raise ApiError(
                401,
                "unauthenticated",
                "A valid bearer API key is required.",
                headers={"www-authenticate": "Bearer"},
            )
        if not principal.can(role):
            raise ApiError(
                403,
                "forbidden",
                f"The {principal.role} role cannot perform this operation.",
            )
        return principal

    return dependency


Viewer = Annotated[Principal, Depends(_require("viewer"))]
Reviewer = Annotated[Principal, Depends(_require("reviewer"))]


async def _strict_json(request: Request) -> None:
    """Re-parse the raw body to reject duplicate keys and NaN/Infinity."""
    body = await request.body()
    if body:
        try:
            loads_strict(body.decode("utf-8"))
        except UnicodeDecodeError:
            raise ApiError(
                422, "invalid_input", "Request body must be UTF-8 JSON."
            ) from None


StrictJson = Annotated[None, Depends(_strict_json)]
CaseId = Annotated[str, PathParam(max_length=64, description="Case UUID")]


# ---------------------------------------------------------------------------
# Pure ASGI middleware: body limit and security headers.
# ---------------------------------------------------------------------------


async def _send_error(send: Send, status: int, body: dict[str, Any]) -> None:
    response = JSONResponse(body, status_code=status)
    await response({"type": "http"}, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.disconnect"}


class BodyLimitMiddleware:
    """Refuse bodies over ``limit`` bytes with 413, by header and by counting.

    The whole body must arrive within ``timeout`` seconds (one deadline for all
    chunks, so a slow drip cannot extend it); otherwise ``408 request_timeout``.
    The in-flight limit sits inside this middleware, so a client that never
    finishes its body never occupies a request slot.
    """

    def __init__(self, app: ASGIApp, limit: int, timeout: float = 10.0):
        self.app = app
        self.limit = limit
        self.timeout = timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_large = error_body(
            "body_too_large",
            f"Request body exceeds {self.limit} bytes.",
            False,
            {"limit": self.limit},
        )
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                if not value.isdigit():
                    await _send_error(
                        send,
                        400,
                        error_body("bad_request", "Invalid Content-Length.", False),
                    )
                    return
                digits = value.lstrip(b"0") or b"0"
                if len(digits) > len(str(self.limit)) or int(digits) > self.limit:
                    await _send_error(send, 413, too_large)
                    return
        chunks: list[bytes] = []
        size = 0
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = await asyncio.wait_for(
                    receive(), max(deadline - time.monotonic(), 0.0)
                )
            except TimeoutError:
                await _send_error(
                    send,
                    408,
                    error_body(
                        "request_timeout",
                        "The request body was not received in time.",
                        True,
                    ),
                )
                return
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.limit:
                await _send_error(send, 413, too_large)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class InFlightLimitMiddleware:
    """Refuse requests beyond ``limit`` concurrently in flight with ``429``.

    Load shedding without queueing: a refused request does no work at all and
    gets a structured, retryable ``too_many_requests`` error with
    ``Retry-After``. Writes are additionally bounded by the write queue.
    ``/health`` is exempt so liveness stays observable under saturation.
    """

    def __init__(self, app: ASGIApp, limit: int):
        self.app = app
        self.limit = limit
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _LIMIT_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if self.in_flight >= self.limit:
            response = JSONResponse(
                error_body(
                    "too_many_requests",
                    "Too many requests are being handled; retry later.",
                    True,
                    {"limit": self.limit, "retry_after_seconds": 1},
                ),
                status_code=429,
                headers={"retry-after": "1"},
            )
            await response({"type": "http"}, _empty_receive, send)
            return
        self.in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1


class UnexpectedErrorMiddleware:
    """Answer an unexpected exception with the generic 500 and stop it here.

    Starlette's outermost error middleware re-raises after sending its 500 so
    the server can log it, and uvicorn then prints the exception message and
    traceback as plain text (possibly a name, reason or local path). Catching
    it inside the app keeps the only record the redacted ``watchlist.api``
    line (exception type only). If the response had already started, nothing
    more can be sent; the type is logged and the exception is dropped.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking)
        except Exception as exc:
            if started:
                logger.error(
                    "unexpected error after response start type=%s",
                    type(exc).__name__,
                )
                return
            response = await _on_unexpected(Request(scope), exc)
            await response(scope, _empty_receive, send)


class SecurityHeadersMiddleware:
    """Add fixed security and research-notice headers to every HTTP response."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name.encode("latin-1"), value.encode("latin-1"))
                    for name, value in SECURITY_HEADERS
                    if name.encode("latin-1") not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


# ---------------------------------------------------------------------------
# Exception handlers.
# ---------------------------------------------------------------------------


def _json(status: int, body: dict[str, Any], headers: dict[str, str] | None = None):
    return JSONResponse(body, status_code=status, headers=headers)


async def _on_review_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ReviewError)
    status = review_error_status(exc)
    case_id = exc.details.get("case_id")
    log = logger.warning if status >= 500 else logger.info
    log("review error code=%s status=%s case=%s", exc.code, status, case_id)
    headers = {"retry-after": "1"} if exc.retryable and status in (429, 503) else None
    return _json(status, review_error_body(exc), headers)


async def _on_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    logger.info("api error code=%s status=%s", exc.code, exc.status)
    return _json(exc.status, exc.body(), exc.headers or None)


async def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Never echo the submitted input back (it may be a name or a reason).
    errors = [
        {
            "loc": [str(part) for part in error.get("loc", ())],
            "type": str(error.get("type", "")),
            "msg": str(error.get("msg", ""))[:200],
        }
        for error in exc.errors()[:20]
    ]
    return _json(
        422,
        error_body(
            "invalid_input", "Request failed validation.", False, {"errors": errors}
        ),
    )


_HTTP_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    415: "unsupported_media_type",
}


async def _on_http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 400:
        # FastAPI raises a bare 400 when the body cannot be parsed at all
        # (invalid UTF-8, nesting too deep for the JSON decoder). That is a
        # validation failure like any other malformed JSON: 422, input never
        # echoed. (The invalid Content-Length 400 is sent by the body-limit
        # middleware and never reaches this handler.)
        return _json(
            422,
            error_body("invalid_input", "Request body is not valid JSON.", False),
        )
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    message = {
        "not_found": "No such route.",
        "method_not_allowed": "Method not allowed for this route.",
    }.get(code, "The request could not be processed.")
    headers = dict(exc.headers or {})
    return _json(exc.status_code, error_body(code, message, False), headers or None)


async def _on_unexpected(request: Request, exc: Exception) -> JSONResponse:
    # Runs outside the user middleware stack, so security headers are added here.
    request_id = request.scope.get(SCOPE_REQUEST_ID)
    logger.error(
        "unexpected error type=%s path=%s",
        type(exc).__name__,
        getattr(request.scope.get("route"), "path", "unrouted"),
        extra={"request_id": request_id if isinstance(request_id, str) else "-"},
    )
    headers = dict(SECURITY_HEADERS)
    if isinstance(request_id, str):
        headers[REQUEST_ID_HEADER] = request_id
    return _json(
        500,
        error_body("internal_error", "Internal error; no details are returned.", False),
        headers,
    )


# ---------------------------------------------------------------------------
# Application assembly.
# ---------------------------------------------------------------------------


def create_app(
    settings: ApiSettings,
    *,
    service: Any = None,
    context_providers: Sequence[ContextProvider] = (),
    entities: Sequence[Entity] | None = None,
) -> FastAPI:
    """Build the API around one ``ReviewService``.

    With ``service=None`` the workspace in ``settings`` is opened writable
    (never created; use ``python -m watchlist.review v2 init``) with the given
    ``context_providers``. An injected service is used as is; passing
    providers as well is an error because they belong to the service.
    ``entities`` overrides the packaged fictional reference records served by
    ``GET /v2/entities/{id}`` (tests only).
    """
    from watchlist.entities import load_entities
    from watchlist.review.nodes import reference_digest
    from watchlist.review.service import ReviewService

    if not isinstance(settings, ApiSettings):
        raise TypeError("settings must be an ApiSettings")
    if service is None:
        if settings.workspace is None or settings.config_path is None:
            raise ValueError("settings.workspace and settings.config_path are required")
        service = ReviewService.open(
            settings.workspace,
            settings.config_path,
            context_providers=tuple(context_providers),
        )
    elif context_providers:
        raise ValueError("pass context_providers to the injected service instead")
    records = tuple(load_entities() if entities is None else entities)
    observability = _observability(settings)
    runtime = _Runtime(
        settings=settings,
        service=observability.instrument(service),
        keys=KeyStore(settings.api_keys),
        writes=WriteQueue(settings.write_queue_max),
        entities={entity.entity_id: entity for entity in records},
        dataset=service.config.retriever.dataset,
        reference_sha256=reference_digest(records),
        observability=observability,
    )
    writes = runtime.writes
    observability.metrics.gauge(
        "watchlist_write_queue_outstanding",
        "Write operations running or waiting in this process.",
        callback=lambda: writes.outstanding,
    )
    observability.metrics.gauge(
        "watchlist_write_queue_limit",
        "Write operations allowed to wait behind the running one.",
        callback=lambda: settings.write_queue_max,
    )
    return _assemble(settings, runtime)


def _observability(settings: ApiSettings) -> Observability:
    """Build per-app metrics/tracing and install the log hooks (idempotent)."""
    observability = Observability(
        trace_path=settings.trace_path, trace_max_bytes=settings.trace_max_bytes
    )
    install_log_filter("watchlist.api", observability.redactor)
    if settings.log_format == "json":
        configure_json_logging(redactor=observability.redactor)
    return observability


def openapi_document() -> dict[str, Any]:
    """The OpenAPI schema, built without opening any workspace."""
    return _assemble(ApiSettings(), None).openapi()


def _check_workspace_readable(service: Any) -> str:
    """Open the service's workspace read-only once, then drop it.

    Returns ``"ok"``, or ``"busy"`` when the single lock attempt finds the lock
    held: the files were found complete before the attempt, and a held lock
    means an operation is running, which is not a readiness failure (requests
    themselves get retryable ``503 workspace_busy`` if it lasts). Every other
    failure raises.
    """
    from watchlist.review.service import ReviewService

    try:
        ReviewService.open(
            service.workspace,
            service.config,
            read_only=True,
            lock_timeout=READY_LOCK_TIMEOUT_SECONDS,
        )
    except ReviewError as exc:
        if exc.code == "workspace_busy":
            return "busy"
        raise
    return "ok"


class ReadinessProbe:
    """Coalesced, briefly cached readiness check (Step 27 hardening).

    ``/ready`` requires a viewer key; authenticated probe floods must not contend
    for the single workspace lock that every real request needs. Concurrent
    probes share one check (a thread lock), and a healthy result is reused for
    ``cache_seconds``, so at most about one lock attempt per window is made.
    Failures are not cached: recovery is visible on the next probe.
    """

    def __init__(
        self,
        check: Callable[[], str],
        cache_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clock = clock
        self._check = check
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: tuple[float, str] | None = None
        self.runs = 0

    def cached(self) -> str | None:
        """The cached healthy result if still fresh (no locking, no I/O)."""
        cached = self._cached
        if cached is not None and self._clock() - cached[0] < self._cache_seconds:
            return cached[1]
        return None

    def state(self) -> str:
        """Run (or join) the check; blocking, call from a worker thread."""
        with self._lock:
            fresh = self.cached()
            if fresh is not None:
                return fresh
            self.runs += 1
            result = self._check()
            self._cached = (self._clock(), result)
            return result


def _case_payload(view: CaseView) -> dict[str, Any]:
    return {
        "case": view.to_dict(),
        "revision": case_revision(view),
        "limitation": LIMITATION,
    }


def _assemble(settings: ApiSettings, runtime: _Runtime | None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if runtime is not None:
                await asyncio.to_thread(runtime.writes.shutdown)

    app = FastAPI(
        title="Polyglot Watchlist review API (research only)",
        version=API_VERSION,
        description=(
            "Research-only prototype over fictional data. "
            + LIMITATION
            + " Review outcomes are only candidate_for_follow_up, none_selected "
            "or unresolved, recorded by an authenticated human reviewer."
        ),
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        telemetry={
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        },
    )
    app.state.runtime = runtime
    app.add_exception_handler(ReviewError, _on_review_error)
    app.add_exception_handler(ApiError, _on_api_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_error)
    app.add_exception_handler(Exception, _on_unexpected)
    observability = (
        runtime.observability
        if runtime is not None and runtime.observability is not None
        else Observability()
    )
    app.state.observability = observability
    # Outermost first: request ID/metrics/span, security headers, unexpected
    # error capture, body limit (and body deadline), in-flight limit. The
    # in-flight limit is inside the body limit so a stalled body never holds a
    # slot. (add_middleware wraps, so the last added runs first.)
    app.add_middleware(InFlightLimitMiddleware, limit=settings.max_in_flight)
    app.add_middleware(
        BodyLimitMiddleware,
        limit=settings.max_body_bytes,
        timeout=settings.body_timeout_seconds,
    )
    app.add_middleware(UnexpectedErrorMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        ObservabilityMiddleware,
        observability=observability,
    )
    readiness = (
        ReadinessProbe(
            partial(_check_workspace_readable, runtime.service), READY_CACHE_SECONDS
        )
        if runtime is not None
        else None
    )
    app.state.readiness = readiness

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> dict[str, Any]:
        """Liveness only; needs no key and reveals no workspace details."""
        return {
            "status": "ok",
            "api_version": API_VERSION,
            "research_only": True,
            "limitation": LIMITATION,
        }

    @app.get("/ready", include_in_schema=False)
    async def ready(request: Request, _principal: Viewer) -> dict[str, Any]:
        """Readiness: the workspace files are complete and open read-only.

        Requires a viewer key; reveals only a stable cause code, never paths. A lock
        held by a running operation is reported as ready with check ``busy``.
        Results are coalesced and briefly cached (``ReadinessProbe``).
        """
        _runtime(request)
        assert readiness is not None
        try:
            state = readiness.cached() or await asyncio.to_thread(readiness.state)
        except ReviewError as exc:
            raise ApiError(
                503,
                "not_ready",
                "The review workspace cannot be opened read-only right now.",
                retryable=exc.retryable,
                details={"cause": exc.code},
                headers={"retry-after": "1"} if exc.retryable else None,
            ) from None
        except Exception as exc:
            logger.warning("readiness check failed type=%s", type(exc).__name__)
            raise ApiError(
                503,
                "not_ready",
                "The review workspace cannot be opened read-only right now.",
                details={"cause": "unexpected_error"},
            ) from None
        return {
            "status": "ready",
            "checks": {"workspace_read_only_open": state},
            "api_version": API_VERSION,
            "research_only": True,
            "limitation": LIMITATION,
        }

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request, principal: Viewer) -> Response:
        """In-process counters/histograms in the Prometheus text format."""
        _runtime(request)
        return Response(
            content=observability.metrics.render(), media_type=PROMETHEUS_CONTENT_TYPE
        )

    @app.post(
        "/v2/cases",
        response_model=CaseResponse,
        responses=_ERRORS,
        tags=["cases"],
    )
    async def start_case(
        body: StartCaseRequest, request: Request, principal: Reviewer, _: StrictJson
    ) -> dict[str, Any]:
        """Start a screening case and stop at the human-review step.

        Returns possible record matches for review, never a decision. The
        case creator is the API key's actor label.
        """
        rt = _runtime(request)
        view = await rt.write(
            lambda: rt.service.start_case(
                body.name,
                origin=body.origin,
                actor=principal.actor,
                request_id=body.request_id,
            ),
            retry_safe=body.request_id is not None,
        )
        return _case_payload(view)

    @app.get(
        "/v2/cases", response_model=CaseListResponse, responses=_ERRORS, tags=["cases"]
    )
    def list_cases(
        request: Request,
        principal: Viewer,
        limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_MAX,
        offset: Annotated[int, Query(ge=0, le=10**9)] = 0,
        origin: Origin | None = None,
    ) -> dict[str, Any]:
        """Catalogue page in creation order (no names, evidence or notes)."""
        rt = _runtime(request)
        cases = rt.service.list_cases(limit, offset, origin=origin)
        return {
            "cases": [item.to_dict() for item in cases],
            "limit": limit,
            "offset": offset,
            "next_offset": offset + len(cases) if len(cases) == limit else None,
            "limitation": LIMITATION,
        }

    @app.get(
        "/v2/cases/{case_id}",
        response_model=CaseResponse,
        responses=_ERRORS,
        tags=["cases"],
    )
    def get_case(
        case_id: CaseId, request: Request, principal: Viewer
    ) -> dict[str, Any]:
        """One case with its packet, review, explanation and status."""
        return _case_payload(_runtime(request).service.get_case(case_id))

    @app.post(
        "/v2/cases/{case_id}/review",
        response_model=CaseResponse,
        responses=_ERRORS,
        tags=["cases"],
    )
    async def submit_review(
        case_id: CaseId,
        body: ReviewRequest,
        request: Request,
        principal: Reviewer,
        _: StrictJson,
    ) -> dict[str, Any]:
        """Record one human review. Retrying the same ``submission_id`` is safe.

        ``revision`` and ``packet_sha256`` must match what the reviewer saw;
        otherwise the form is stale (409). The reviewer is the key's actor and
        the origin is the case's own origin.
        """
        rt = _runtime(request)
        uuid_text(case_id, "case_id")

        def operation() -> CaseView:
            current = rt.service.get_case(case_id)
            accepted = current.review
            replay = (
                accepted is not None
                and accepted.submission.submission_id == body.submission_id
            )
            if not replay and case_revision(current) != body.revision:
                raise StaleSubmissionError(
                    "the case changed since this revision; reload it before reviewing",
                    details={"case_id": case_id, "status": current.status},
                )
            form = ReviewSubmission(
                submission_id=body.submission_id,
                packet_sha256=body.packet_sha256,
                outcome=body.outcome,
                candidate_entity_id=body.candidate_entity_id,
                reason=body.reason,
                actor=principal.actor,
                origin=current.header.origin,
            )
            return rt.service.submit_review(case_id, form)

        return _case_payload(await rt.write(operation))

    @app.post(
        "/v2/cases/{case_id}/recover",
        response_model=CaseResponse,
        responses=_ERRORS,
        tags=["cases"],
    )
    async def recover(
        case_id: CaseId, request: Request, principal: Reviewer
    ) -> dict[str, Any]:
        """Finish interrupted work from durable state; never invents a review."""
        rt = _runtime(request)
        return _case_payload(await rt.write(lambda: rt.service.recover(case_id)))

    @app.post(
        "/v2/cases/{case_id}/cancel",
        response_model=CaseResponse,
        responses=_ERRORS,
        tags=["cases"],
    )
    async def cancel(
        case_id: CaseId,
        body: CancelRequest,
        request: Request,
        principal: Reviewer,
        _: StrictJson,
    ) -> dict[str, Any]:
        """Cancel before any review is accepted; cancellation is not an outcome."""
        rt = _runtime(request)
        view = await rt.write(
            lambda: rt.service.cancel_case(
                case_id, actor=principal.actor, reason=body.reason
            )
        )
        return _case_payload(view)

    @app.get(
        "/v2/cases/{case_id}/audit",
        response_model=AuditResponse,
        responses=_ERRORS,
        tags=["audit"],
    )
    def audit(case_id: CaseId, request: Request, principal: Viewer) -> dict[str, Any]:
        """Every hash-chained audit event of a case."""
        view = _runtime(request).service.audit(case_id)
        return {"audit": view.to_dict(), "limitation": LIMITATION}

    @app.get(
        "/v2/cases/{case_id}/audit/verify",
        response_model=VerificationResponse,
        responses=_ERRORS,
        tags=["audit"],
    )
    def verify_audit(
        case_id: CaseId,
        request: Request,
        principal: Viewer,
        anchor_count: Annotated[int | None, Query(ge=1, le=10**9)] = None,
        anchor_head_sha256: Annotated[str | None, Query(pattern=_SHA256)] = None,
    ) -> dict[str, Any]:
        """Local tamper-evidence check, optionally against an anchor kept elsewhere."""
        if (anchor_count is None) != (anchor_head_sha256 is None):
            raise ApiError(
                422,
                "invalid_input",
                "anchor_count and anchor_head_sha256 must be given together.",
            )
        anchor = (
            None
            if anchor_count is None
            else AuditAnchor(
                uuid_text(case_id, "case_id"), anchor_count, anchor_head_sha256
            )
        )
        result = _runtime(request).service.verify_audit(case_id, anchor)
        return {"verification": result.to_dict(), "limitation": LIMITATION}

    @app.get(
        "/v2/entities/{entity_id}",
        response_model=EntityResponse,
        responses=_ERRORS,
        tags=["reference"],
    )
    def get_entity(
        entity_id: Annotated[str, PathParam(max_length=128)],
        request: Request,
        principal: Viewer,
    ) -> dict[str, Any]:
        """A fictional reference record with aliases and provenance (if any)."""
        from watchlist.review.nodes import _evidence_for

        rt = _runtime(request)
        if not _ENTITY_ID.fullmatch(entity_id):
            raise ApiError(422, "invalid_input", "entity_id has an invalid format.")
        entity = rt.entities.get(entity_id)
        if entity is None:
            raise ApiError(404, "entity_not_found", "No reference record with this ID.")
        evidence, _ = _evidence_for(entity)
        first = evidence[0]
        return {
            "entity": {
                "entity_id": entity.entity_id,
                "name": entity.name,
                "entity_type": entity.entity_type,
                "synthetic": entity.synthetic,
                "source": entity.source,
                "attribution": first.attribution,
                "aliases": [
                    {
                        "alias_id": item.alias_id,
                        "name": item.original_text,
                        "evidence_id": item.evidence_id,
                    }
                    for item in evidence
                ],
                "provenance": first.provenance,
                "dataset": rt.dataset,
                "reference_sha256": rt.reference_sha256,
            },
            "limitation": LIMITATION,
        }

    return app
