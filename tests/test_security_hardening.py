"""Step 27 adversarial hardening of the review API (fictional data, in-process).

Covers oversized bodies, Unicode bidi/zero-width names, SQL-like and
path-traversal identifiers, header injection, log injection and secrets in
logs/traces/metrics. Every attack must end in a structured 4xx (or a generic
5xx without details), with no state change and nothing sensitive recorded.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
import secrets
import socket
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
import httpx

from review_v2_support import FakeRetriever, make_config
from watchlist import observability as obs
from watchlist.api import ApiSettings, create_app
from watchlist.api.auth import ApiKey, hash_key
from watchlist.review.service import ReviewService

REVIEWER = secrets.token_urlsafe(24)
VIEWER = secrets.token_urlsafe(24)
KEYS = (
    ApiKey("fictional-reviewer", "reviewer", hash_key(REVIEWER)),
    ApiKey("fictional-viewer", "viewer", hash_key(VIEWER)),
)
AUTH = {"Authorization": f"Bearer {REVIEWER}"}
VIEW_AUTH = {"Authorization": f"Bearer {VIEWER}"}
NAME = "Luma Vesper"
REASON = "Fictional hardening reason about the possible record match."


def build(tmp_path: Path, service=None, **settings):
    service = service or ReviewService.initialize(
        tmp_path / "ws", make_config(), retriever=FakeRetriever()
    )
    return create_app(ApiSettings(api_keys=KEYS, **settings), service=service)


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"code", "message", "retryable", "details"}
    assert body["code"] == code
    return body


@pytest.fixture
def json_logs():
    """Capture every ``watchlist`` record as JSON lines; restore logging after."""
    logger = logging.getLogger("watchlist")
    before, level = list(logger.handlers), logger.level
    stream = io.StringIO()
    obs.configure_json_logging(stream, level=logging.DEBUG)
    try:
        yield stream
    finally:
        for handler in list(logger.handlers):
            if handler not in before:
                logger.removeHandler(handler)
        logger.setLevel(level)


def files_of(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def server_log_text(caplog) -> str:
    """Server-side records only (the test client's own httpx log is excluded)."""
    return "\n".join(
        f"{r.name} {r.getMessage()}"
        for r in caplog.records
        if r.name.startswith("watchlist")
    )


def asgi_request(app, method: str, path: str, headers: list[tuple[bytes, bytes]]):
    """Drive the ASGI app directly with raw bytes (no client-side validation).

    Nothing is swallowed: the app must not re-raise an unexpected exception
    after its 500 (a server would log the message and traceback as text).
    """
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages[1:])
    return start["status"], start["headers"], body


# -- oversized input -----------------------------------------------------------------


def test_oversized_bodies_are_413_with_request_id_and_metric(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as client:
        sized = client.post("/v2/cases", content=b"{" + b" " * 70000, headers=AUTH)
        assert_error(sized, 413, "body_too_large")

        def chunks():
            for _ in range(80):
                yield b" " * 1024

        chunked = client.post("/v2/cases", content=chunks(), headers=AUTH)
        assert_error(chunked, 413, "body_too_large")
        for response in (sized, chunked):
            assert obs.accept_request_id(response.headers["x-request-id"])
            assert response.headers["x-content-type-options"] == "nosniff"
        text = client.get("/metrics", headers=VIEW_AUTH).text
        assert 'status="413",code="body_too_large"} 2' in text
        assert client.get("/v2/cases", headers=AUTH).json()["cases"] == []


def test_huge_fields_inside_the_body_limit_are_refused_without_echo(tmp_path):
    with TestClient(build(tmp_path)) as client:
        name = "Vesper" * 8000  # 48,000 characters: under 64 KiB, over 200 points
        response = client.post(
            "/v2/cases", json={"name": name, "origin": "test"}, headers=AUTH
        )
        assert_error(response, 422, "invalid_input")
        assert "Vesper" not in response.text
        huge_query = client.get("/v2/cases?limit=" + "9" * 5000, headers=AUTH)
        assert_error(huge_query, 422, "invalid_input")
        long_id = client.get("/health", headers={"X-Request-ID": "r" * 10000})
        assert long_id.status_code == 200 and len(long_id.headers["x-request-id"]) == 32


def _raw_scope(method: str, path: str, headers: list[tuple[bytes, bytes]]) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }


def test_stalled_bodies_do_not_occupy_request_slots(tmp_path):
    """Regression (reviewer finding 2): idle unauthenticated uploads vs the cap."""
    app = build(tmp_path, max_in_flight=4)
    stalled_headers = [
        (b"content-length", b"10"),
        (b"content-type", b"application/json"),
    ]

    async def scenario():
        release = asyncio.Event()

        async def never_sends_body():
            await release.wait()
            return {"type": "http.disconnect"}

        async def discard(message):
            return None

        stalled = [
            asyncio.create_task(
                app(
                    _raw_scope("POST", "/v2/cases", stalled_headers),
                    never_sends_body,
                    discard,
                )
            )
            for _ in range(8)
        ]
        await asyncio.sleep(0.2)
        sent: list[dict] = []

        async def empty_body():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def collect(message):
            sent.append(message)

        auth = [(b"authorization", f"Bearer {REVIEWER}".encode("ascii"))]
        await app(_raw_scope("GET", "/v2/cases", auth), empty_body, collect)
        release.set()
        await asyncio.gather(*stalled, return_exceptions=True)
        return sent

    sent = asyncio.run(scenario())
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200


def test_body_that_never_arrives_gets_408_within_the_deadline(tmp_path):
    app = build(tmp_path, body_timeout_seconds=0.3)
    sent: list[dict] = []

    async def scenario():
        async def never_sends_body():
            await asyncio.sleep(30)
            return {"type": "http.disconnect"}

        async def collect(message):
            sent.append(message)

        headers = [
            (b"content-length", b"10"),
            (b"authorization", f"Bearer {REVIEWER}".encode("ascii")),
        ]
        await asyncio.wait_for(
            app(_raw_scope("POST", "/v2/cases", headers), never_sends_body, collect),
            timeout=10,
        )

    asyncio.run(scenario())
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 408
    body = json.loads(b"".join(m.get("body", b"") for m in sent[1:]))
    assert body == {
        "code": "request_timeout",
        "message": "The request body was not received in time.",
        "retryable": True,
        "details": {},
    }
    names = {name for name, _ in start["headers"]}
    assert b"x-request-id" in names and b"x-content-type-options" in names


# -- Unicode names -------------------------------------------------------------------


# Characters that must never appear raw in logs or traces.
LRM, RLM, ALM = chr(0x200E), chr(0x200F), chr(0x061C)
RLO, RLI, PDI = chr(0x202E), chr(0x2067), chr(0x2069)
ZWSP, ZWNJ, ZWJ = chr(0x200B), chr(0x200C), chr(0x200D)
UNSAFE = frozenset((LRM, RLM, ALM, RLO, RLI, PDI, ZWSP, ZWNJ, ZWJ))


@pytest.mark.parametrize(
    "name",
    [
        f"Luma{RLO}Vesper",
        f"{RLI}Luma Vesper{PDI}",
        ZWSP * 2,
        f"Luma{LRM}Vesper",
        f"Luma{RLM}Vesper",
        f"Luma{ALM}Vesper",
    ],
)
def test_bidi_controls_and_invisible_only_names_are_refused(tmp_path, name, caplog):
    caplog.set_level(logging.DEBUG)
    trace = tmp_path / "trace.jsonl"
    with TestClient(build(tmp_path, trace_path=trace)) as client:
        response = client.post(
            "/v2/cases", json={"name": name, "origin": "test"}, headers=AUTH
        )
        body = assert_error(response, 422, "invalid_input")
        assert name not in json.dumps(body, ensure_ascii=False)
        assert client.get("/v2/cases", headers=AUTH).json()["cases"] == []
    recorded = server_log_text(caplog) + trace.read_text(encoding="utf-8")
    assert not any(char in recorded for char in UNSAFE)


def test_zero_width_joiners_inside_names_are_kept_exactly_and_never_logged_raw(
    tmp_path, caplog
):
    """The service permits format characters inside names (ZWJ/ZWNJ are needed
    by some scripts; it currently also accepts U+200B). The API must store the
    exact text and observability must never write the raw character."""
    caplog.set_level(logging.DEBUG)
    trace = tmp_path / "trace.jsonl"
    with TestClient(build(tmp_path, trace_path=trace)) as client:
        for name in (f"Luma{ZWJ}Vesper", f"Luma{ZWNJ}Vesper", f"Luma{ZWSP}Vesper"):
            response = client.post(
                "/v2/cases", json={"name": name, "origin": "test"}, headers=AUTH
            )
            assert response.status_code == 200, response.text
            stored = json.dumps(response.json(), ensure_ascii=False)
            assert name in stored
    recorded = server_log_text(caplog) + trace.read_text(encoding="utf-8")
    assert not any(c in recorded for c in UNSAFE)
    assert "Luma" not in trace.read_text(encoding="utf-8")


# -- SQL-like and path-traversal identifiers ------------------------------------------

HOSTILE_IDS = [
    "1%27%20OR%20%271%27%3D%271",
    "%27%3B%20DROP%20TABLE%20cases%3B--",
    "1%20UNION%20SELECT%20*%20FROM%20cases",
    "..%2F..%2Fwriter.lock",
    "%2E%2E%2F%2E%2E%2Freview.sqlite",
    "..%5C..%5Creview.sqlite",
    "%00",
    "x%0D%0ASet-Cookie%3A%20a%3Db",
    "%E2%80%AE" + "a" * 8,
    "a" * 65,
]


@pytest.mark.parametrize("hostile", HOSTILE_IDS)
def test_hostile_case_ids_never_reach_storage(tmp_path, hostile):
    app = build(tmp_path)
    workspace = tmp_path / "ws"
    with TestClient(app) as client:
        created = client.post(
            "/v2/cases", json={"name": NAME, "origin": "test"}, headers=AUTH
        )
        case_id = created.json()["case"]["case_id"]
        before = files_of(workspace)
        responses = [
            client.get(f"/v2/cases/{hostile}", headers=AUTH),
            client.get(f"/v2/cases/{hostile}/audit", headers=AUTH),
            client.get(f"/v2/cases/{hostile}/audit/verify", headers=AUTH),
            client.post(f"/v2/cases/{hostile}/recover", headers=AUTH),
            client.post(
                f"/v2/cases/{hostile}/cancel", json={"reason": REASON}, headers=AUTH
            ),
            client.post(
                f"/v2/cases/{hostile}/review",
                json={
                    "submission_id": str(uuid4()),
                    "revision": "0" * 64,
                    "packet_sha256": "0" * 64,
                    "outcome": "unresolved",
                    "candidate_entity_id": None,
                    "reason": REASON,
                },
                headers=AUTH,
            ),
            client.get(f"/v2/entities/{hostile}", headers=AUTH),
        ]
        for response in responses:
            assert response.status_code in (404, 405, 422), (hostile, response.text)
            assert set(response.json()) == {"code", "message", "retryable", "details"}
            assert "set-cookie" not in response.headers
        assert files_of(workspace) == before
        listing = client.get("/v2/cases", headers=AUTH).json()["cases"]
        assert [item["case_id"] for item in listing] == [case_id]
        verified = client.get(f"/v2/cases/{case_id}/audit/verify", headers=AUTH)
        assert verified.json()["verification"]["ok"] is True
        metrics = client.get("/metrics", headers=VIEW_AUTH).text
        assert "DROP" not in metrics and "writer.lock" not in metrics


# -- header injection ------------------------------------------------------------------


def test_raw_header_injection_is_not_reflected(tmp_path):
    app = build(tmp_path)
    status, headers, _ = asgi_request(
        app,
        "GET",
        "/health",
        [(b"x-request-id", b"evil\r\nset-cookie: pwned=1")],
    )
    assert status == 200
    names = [name.lower() for name, _ in headers]
    assert b"set-cookie" not in names
    (request_id,) = [value for name, value in headers if name == b"x-request-id"]
    assert b"evil" not in request_id and obs.accept_request_id(request_id)

    status, headers, _ = asgi_request(
        app,
        "GET",
        "/health",
        [(b"x-request-id", b"first-ok"), (b"x-request-id", b"second-ok")],
    )
    assert [v for n, v in headers if n == b"x-request-id"][0] not in (
        b"first-ok",
        b"second-ok",
    )

    status, headers, _ = asgi_request(
        app, "GET", "/health", [(b"x-request-id", "r\u00e9q".encode("utf-8"))]
    )
    assert [v for n, v in headers if n == b"x-request-id"][0].isascii()


def test_header_injection_in_authorization_is_refused(tmp_path):
    app = build(tmp_path)
    for value in (
        f"Bearer {REVIEWER}\r\nx-admin: 1".encode("ascii"),
        f"Bearer {REVIEWER} extra".encode("ascii"),
        b"Bearer \xff\xfe" + b"a" * 30,
        b"Basic " + REVIEWER.encode("ascii"),
    ):
        status, headers, body = asgi_request(
            app, "GET", "/v2/cases", [(b"authorization", value)]
        )
        assert status == 401, value
        assert json.loads(body)["code"] == "unauthenticated"
        assert b"x-admin" not in [n for n, _ in headers]


# -- log injection and secrets -----------------------------------------------------------


class _Exploding:
    """A service stand-in whose reads raise an exception carrying secret text."""

    def __init__(self, service, secret_text: str):
        self._service = service
        self._secret = secret_text

    def __getattr__(self, name):
        return getattr(self._service, name)

    def get_case(self, case_id):
        raise RuntimeError(f"failure mentioning {self._secret} and {case_id}")


def test_newlines_in_names_and_paths_cannot_forge_log_lines(
    tmp_path, json_logs, caplog
):
    caplog.set_level(logging.DEBUG)
    forged = '\n{"level":"CRITICAL","logger":"watchlist.api","message":"forged"}\n'
    service = ReviewService.initialize(
        tmp_path / "ws", make_config(), retriever=FakeRetriever()
    )
    app = build(tmp_path, service=_Exploding(service, "Luma Vesper"))
    with TestClient(app, raise_server_exceptions=False) as client:
        bad_name = client.post(
            "/v2/cases", json={"name": "Luma" + forged, "origin": "test"}, headers=AUTH
        )
        assert_error(bad_name, 422, "invalid_input")
    # A raw (decoded) newline in the path reaches the unexpected-error log line.
    status, headers, body = asgi_request(
        app,
        "GET",
        '/v2/cases/a\n{"level":"CRITICAL"}',
        [(b"authorization", f"Bearer {REVIEWER}".encode("ascii"))],
    )
    assert status == 500 and json.loads(body)["code"] == "internal_error"
    (request_id,) = [v.decode() for n, v in headers if n == b"x-request-id"]
    assert obs.accept_request_id(request_id) and b"Luma" not in body
    lines = json_logs.getvalue().splitlines()
    assert lines, "expected log records"
    parsed = [json.loads(line) for line in lines]  # every line is one JSON object
    assert not any(record["level"] == "CRITICAL" for record in parsed)
    unexpected = [r for r in parsed if r["message"].startswith("unexpected error")]
    # (Python's URL parsing already drops the newline from request.url.path; the
    # log filter escapes any that remain - unit-tested in test_observability.)
    assert len(unexpected) == 1 and "\n" not in unexpected[0]["message"]
    assert unexpected[0]["request_id"] == request_id
    for record in caplog.records:
        assert "\n" not in record.getMessage()
    assert "Luma" not in json_logs.getvalue()
    assert "Luma" not in server_log_text(caplog)


def test_secrets_names_and_reasons_never_reach_logs_traces_or_metrics(
    tmp_path, json_logs, caplog
):
    caplog.set_level(logging.DEBUG)
    trace = tmp_path / "trace.jsonl"
    wrong = secrets.token_urlsafe(24)
    with TestClient(build(tmp_path, trace_path=trace)) as client:
        client.get("/v2/cases", headers={"Authorization": f"Bearer {wrong}"})
        client.get(f"/v2/cases?api_key={wrong}", headers=VIEW_AUTH)
        client.get("/health", headers={**AUTH, "X-Request-ID": REVIEWER})
        client.post(
            "/v2/cases", json={"name": NAME, "origin": "test"}, headers=VIEW_AUTH
        )
        created = client.post(
            "/v2/cases", json={"name": NAME, "origin": "test"}, headers=AUTH
        ).json()
        case_id = created["case"]["case_id"]
        reviewed = client.post(
            f"/v2/cases/{case_id}/review",
            json={
                "submission_id": str(uuid4()),
                "revision": created["revision"],
                "packet_sha256": created["case"]["packet_sha256"],
                "outcome": "candidate_for_follow_up",
                "candidate_entity_id": "syn-001",
                "reason": REASON,
            },
            headers=AUTH,
        )
        assert reviewed.status_code == 200
        second = client.post(
            "/v2/cases", json={"name": NAME, "origin": "test"}, headers=AUTH
        ).json()["case"]["case_id"]
        client.post(f"/v2/cases/{second}/cancel", json={"reason": REASON}, headers=AUTH)
        metrics = client.get("/metrics", headers=VIEW_AUTH).text
    recorded = {
        "caplog": server_log_text(caplog),
        "json_logs": json_logs.getvalue(),
        "trace": trace.read_text(encoding="utf-8"),
        "metrics": metrics,
    }
    assert recorded["trace"], "trace should not be empty"
    for where, text in recorded.items():
        for token in (wrong, REVIEWER, VIEWER):
            assert token not in text, where
            assert hash_key(token) not in text, where
        assert NAME not in text, where
        assert REASON not in text, where
    spans = obs.read_trace(trace)
    starts = [s for s in spans if s["operation"] == "review.start_case"]
    assert starts and all(s["attrs"]["name"].startswith("hmac-sha256:") for s in starts)


# -- the real ASGI server never sees unexpected exceptions ---------------------------


class _Collect(logging.Handler):
    """Collect fully formatted records, including any traceback text."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


def test_real_uvicorn_logs_no_exception_text_for_an_unexpected_500(tmp_path):
    """Regression (reviewer finding 4): uvicorn printed message and traceback."""
    uvicorn = pytest.importorskip("uvicorn")
    hidden_reason = "HIDDENREASON-" + secrets.token_hex(4)
    service = ReviewService.initialize(
        tmp_path / "ws", make_config(), retriever=FakeRetriever()
    )
    app = build(tmp_path, service=_Exploding(service, f"{NAME} {hidden_reason}"))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    collector = _Collect()
    loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "watchlist")]
    for logger in loggers:
        logger.addHandler(collector)
    server = uvicorn.Server(
        uvicorn.Config(
            app, host="127.0.0.1", port=port, log_config=None, access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert time.monotonic() < deadline and thread.is_alive()
            time.sleep(0.05)
        response = httpx.get(
            f"http://127.0.0.1:{port}/v2/cases/{uuid4()}",
            headers=AUTH,
            timeout=10,
        )
        assert response.status_code == 500
        assert response.json()["code"] == "internal_error"
    finally:
        server.should_exit = True
        thread.join(30)
        for logger in loggers:
            logger.removeHandler(collector)
    text = "\n".join(collector.lines)
    assert "unexpected error type=RuntimeError" in text
    assert "Traceback" not in text and "Exception in ASGI application" not in text
    assert hidden_reason not in text and NAME not in text


def test_enormous_content_length_is_a_client_error_not_an_internal_error(tmp_path):
    app = build(tmp_path)
    status, headers, body = asgi_request(
        app, "POST", "/v2/cases", [(b"content-length", b"9" * 5000)]
    )
    assert status == 413
    assert json.loads(body)["code"] == "body_too_large"
