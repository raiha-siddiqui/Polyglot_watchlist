"""Step 27: redaction, JSON logs, request IDs, spans, metrics, health and readiness."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
import secrets

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from review_v2_support import FakeRetriever, make_config
from watchlist import observability as obs
from watchlist.api import ApiSettings, create_app, openapi_document
from watchlist.api.auth import ApiKey, hash_key
from watchlist.api.settings import SettingsError
from watchlist.review.contracts import LIMITATION, NotFoundError
from watchlist.review.service import ReviewService

REVIEWER = secrets.token_urlsafe(24)
VIEWER = secrets.token_urlsafe(24)
KEYS = (
    ApiKey("fictional-reviewer", "reviewer", hash_key(REVIEWER)),
    ApiKey("fictional-viewer", "viewer", hash_key(VIEWER)),
)
AUTH = {"Authorization": f"Bearer {REVIEWER}"}
VIEW_AUTH = {"Authorization": f"Bearer {VIEWER}"}
TEST_KEY = b"fictional-test-redaction-key"


def build(tmp_path: Path, **settings):
    service = ReviewService.initialize(
        tmp_path / "ws", make_config(), retriever=FakeRetriever()
    )
    return create_app(ApiSettings(api_keys=KEYS, **settings), service=service)


# -- request IDs ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, accepted",
    [
        ("fictional-req-001", True),
        ("A1._-", True),
        ("x" * 64, True),
        ("x" * 65, False),
        ("-leading-dash", False),
        ("has space", False),
        ("line\r\nbreak", False),
        ("bidi\u202eid", False),
        ('quote"id', False),
        ("", False),
        (b"bytes-ok", True),
        (b"\xff\xfe", False),
        (None, False),
    ],
)
def test_accept_request_id(value, accepted):
    result = obs.accept_request_id(value)
    assert (result is not None) is accepted


def test_new_request_ids_are_unique_and_accepted():
    ids = {obs.new_request_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(obs.accept_request_id(value) == value for value in ids)


def test_request_id_scope_binds_and_resets():
    assert obs.current_request_id() is None
    with obs.request_id_scope("fictional-scope") as value:
        assert value == obs.current_request_id() == "fictional-scope"
    assert obs.current_request_id() is None
    with obs.request_id_scope("bad id") as value:
        assert value != "bad id" and obs.accept_request_id(value) == value


# -- redaction policy --------------------------------------------------------------


def test_secret_fields_are_redacted_not_hashed():
    redactor = obs.Redactor(TEST_KEY)
    token = secrets.token_urlsafe(24)
    fields = redactor.fields(
        {
            "authorization": f"Bearer {token}",
            "api_key": token,
            "X-Api-Key": token,
            "session_token": token,
            "client_secret": token,
            "password": token,
            "cookie": token,
            "status": "ok",
        }
    )
    assert fields["status"] == "ok"
    for key, value in fields.items():
        if key != "status":
            assert value == obs.REDACTED, key
    assert token not in json.dumps(fields)


def test_personal_text_fields_are_fingerprinted():
    redactor = obs.Redactor(TEST_KEY)
    first = redactor.field("name", "Luma Vesper")
    assert first.startswith("hmac-sha256:") and first.endswith(";chars=11")
    assert "Luma" not in first
    # Deterministic for one key, different for another key.
    assert redactor.field("name", "Luma Vesper") == first
    assert obs.Redactor(b"another-fictional-key").field("name", "Luma Vesper") != first
    assert redactor.field("reason", "Fictional reason text").startswith("hmac-sha256:")
    assert redactor.field("NOTES", "x").startswith("hmac-sha256:")
    assert redactor.field("aliases", ["A", "B"])[0].startswith("hmac-sha256:")
    assert redactor.field("name", None) is None


@pytest.mark.parametrize(
    "key",
    [
        "display_name",
        "entity_name",
        "candidate_name",
        "full_name",
        "input_name",
        "Name",
        "matched_alias",
        "query_text",
        "cancel_reason",
        "reviewer-notes",
        "original_text",
        "normalized_name",
        "review.comment",
    ],
)
def test_prefixed_and_suffixed_personal_keys_are_fingerprinted(key):
    # Regression (reviewer finding 3): exact-name matching let these through.
    value = obs.Redactor(TEST_KEY).field(key, "Luma Vesper")
    assert value.startswith("hmac-sha256:") and "Luma" not in value


@pytest.mark.parametrize("key", ["status", "case_id", "route", "code", "origin"])
def test_operational_keys_stay_readable(key):
    assert obs.Redactor(TEST_KEY).field(key, "awaiting_review") == "awaiting_review"


def test_real_case_view_through_the_redactor_has_no_name(tmp_path):
    service = ReviewService.initialize(
        tmp_path / "ws", make_config(), retriever=FakeRetriever()
    )
    view = service.start_case("Luma Vesper", origin="test", actor="fictional-a")
    redacted = json.dumps(obs.Redactor(TEST_KEY).fields(view.to_dict()))
    assert "Luma" not in redacted and "Vesper" not in redacted
    assert "luma" not in redacted and "vesper" not in redacted
    assert view.case_id in redacted  # identifiers stay usable for correlation


def test_scrub_removes_key_value_secrets_in_free_text():
    redactor = obs.Redactor(TEST_KEY)
    first, second = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    for text in (
        f"x-api-key: {first} ?api_key={second}",
        f"GET /v2/cases?apikey={first}&token={second}",
        f"password={first}; client secret: {second}",
    ):
        scrubbed = redactor.scrub(text)
        assert first not in scrubbed and second not in scrubbed, scrubbed
        assert obs.REDACTED in scrubbed
    # Format placeholders survive, so log format strings keep their arguments.
    assert redactor.scrub("api_key=%s token={}") == "api_key=%s token={}"
    assert redactor.scrub("tokens=5 keys") == "tokens=5 keys"


def test_default_redactor_key_is_random_per_instance():
    assert obs.Redactor().fingerprint("Luma") != obs.Redactor().fingerprint("Luma")


def test_scrub_escapes_controls_bidi_and_bearer_and_truncates():
    redactor = obs.Redactor(TEST_KEY, max_chars=32)
    token = secrets.token_urlsafe(24)
    text = redactor.scrub(f"a\nb\r\u202ec\u200bd\u2028 Bearer {token}")
    assert "\n" not in text and "\r" not in text
    assert "\u202e" not in text and "\u200b" not in text and "\u2028" not in text
    assert "\\u000a" in text and "\\u202e" in text
    assert token not in text
    long = redactor.scrub("y" * 100)
    assert long.startswith("y" * 32) and long.endswith("...[+68 chars]")
    assert redactor.scrub("plain text") == "plain text"


def test_values_of_other_types():
    redactor = obs.Redactor(TEST_KEY)
    assert redactor.value(Path("C:/Users/someone/secret-dir/trace.jsonl")) == (
        "trace.jsonl"
    )
    assert redactor.value(float("nan")) == "nan"
    assert redactor.value(object()) == "object"
    nested = redactor.value({"a": {"b": {"c": {"d": {"e": 1}}}}})
    assert nested["a"]["b"]["c"]["d"] == "[truncated]"
    with pytest.raises(ValueError):
        obs.Redactor(b"")
    with pytest.raises(ValueError):
        obs.Redactor(max_chars=1)


# -- logging ------------------------------------------------------------------------


def _json_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = obs.configure_json_logging(
        stream, logger_name=name, redactor=obs.Redactor(TEST_KEY)
    )
    logger = logging.getLogger(name)
    assert handler in logger.handlers
    return logger, stream


def test_json_formatter_one_ascii_line_with_redacted_fields():
    logger, stream = _json_logger("watchlist.test_obs_json")
    token = secrets.token_urlsafe(24)
    with obs.request_id_scope("fictional-log-req"):
        logger.warning(
            "user said %s",
            'x\n{"level":"CRITICAL","message":"forged"}',
            extra={"fields": {"name": "Luma Vesper", "authorization": token}},
        )
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["level"] == "WARNING"
    assert record["request_id"] == "fictional-log-req"
    assert "\n" not in record["message"] and "forged" in record["message"]
    assert record["fields"]["authorization"] == obs.REDACTED
    assert record["fields"]["name"].startswith("hmac-sha256:")
    assert lines[0].isascii() and token not in lines[0] and "Luma" not in lines[0]


def test_json_formatter_writes_exception_type_only():
    logger, stream = _json_logger("watchlist.test_obs_exc")
    try:
        raise RuntimeError("C:/Users/someone/private path")
    except RuntimeError:
        logger.exception("failed")
    record = json.loads(stream.getvalue())
    assert record["exc_type"] == "RuntimeError"
    assert "private path" not in stream.getvalue()


def test_configure_json_logging_is_idempotent():
    name = "watchlist.test_obs_idem"
    obs.configure_json_logging(io.StringIO(), logger_name=name)
    obs.configure_json_logging(io.StringIO(), logger_name=name)
    marked = [
        h
        for h in logging.getLogger(name).handlers
        if getattr(h, "_watchlist_json_handler", False)
    ]
    assert len(marked) == 1


def test_log_filter_neutralizes_arguments_for_any_formatter(caplog):
    logger = logging.getLogger("watchlist.test_obs_filter")
    obs.install_log_filter("watchlist.test_obs_filter")
    obs.install_log_filter("watchlist.test_obs_filter")
    assert sum(isinstance(f, obs.LogContextFilter) for f in logger.filters) == 1
    caplog.set_level(logging.INFO, logger="watchlist.test_obs_filter")
    logger.info("path=%s", "/v2/cases/a\nINFO forged line")
    logger.info("mapping %(x)s", {"x": "a\rb"})
    messages = [r.getMessage() for r in caplog.records]
    assert all("\n" not in m and "\r" not in m for m in messages)
    assert caplog.records[0].request_id == "-"


# -- metrics -------------------------------------------------------------------------


def test_prometheus_rendering_exact():
    registry = obs.MetricsRegistry()
    counter = registry.counter("demo_total", "Demo counter.", ("kind",))
    counter.inc(kind="a")
    counter.inc(2, kind='q"u\\o\nte')
    histogram = registry.histogram(
        "demo_seconds", "Demo histogram.", buckets=(0.1, 1.0)
    )
    histogram.observe(0.1)
    histogram.observe(0.5)
    histogram.observe(5)
    registry.gauge("demo_gauge", "Demo gauge.", callback=lambda: 3)
    assert registry.render() == (
        "# HELP demo_gauge Demo gauge.\n"
        "# TYPE demo_gauge gauge\n"
        "demo_gauge 3\n"
        "# HELP demo_seconds Demo histogram.\n"
        "# TYPE demo_seconds histogram\n"
        'demo_seconds_bucket{le="0.1"} 1\n'
        'demo_seconds_bucket{le="1"} 2\n'
        'demo_seconds_bucket{le="+Inf"} 3\n'
        "demo_seconds_sum 5.6\n"
        "demo_seconds_count 3\n"
        "# HELP demo_total Demo counter.\n"
        "# TYPE demo_total counter\n"
        'demo_total{kind="a"} 1\n'
        'demo_total{kind="q\\"u\\\\o\\nte"} 2\n'
    )


def test_metric_validation_and_overflow():
    registry = obs.MetricsRegistry()
    with pytest.raises(ValueError):
        registry.counter("bad name", "x")
    with pytest.raises(ValueError):
        registry.counter("ok_total", "x", ("bad-label",))
    counter = registry.counter("ok_total", "x", ("k",))
    with pytest.raises(ValueError):
        registry.counter("ok_total", "again")
    with pytest.raises(ValueError):
        counter.inc(-1, k="a")
    with pytest.raises(ValueError):
        counter.inc(k="a", extra="b")
    small = obs.Counter("small_total", "x", ("k",), series_limit=2)
    for value in range(10):
        small.inc(k=str(value))
    assert small.value(k="_overflow") == 8 and small.total() == 10
    with pytest.raises(ValueError):
        registry.histogram("h_seconds", "x", buckets=(1.0, 0.5))
    with pytest.raises(ValueError):
        registry.histogram("h2_seconds", "x", ("le",))


def test_broken_gauge_callback_does_not_break_rendering():
    registry = obs.MetricsRegistry()
    registry.gauge("broken", "x", callback=lambda: 1 / 0)
    assert "# TYPE broken gauge" in registry.render()


# -- spans ---------------------------------------------------------------------------


def test_tracer_writes_nested_spans_with_outcomes(tmp_path):
    path = tmp_path / "trace.jsonl"
    o = obs.Observability(trace_path=path, redactor=obs.Redactor(TEST_KEY))
    with obs.request_id_scope("fictional-trace"):
        with o.span("outer", name="Luma Vesper") as outer:
            with pytest.raises(NotFoundError):
                with o.span("inner"):
                    raise NotFoundError("no such case")
            with pytest.raises(KeyError):
                with o.span("broken"):
                    raise KeyError("x")
            outer.set("items", 2)
    records = obs.read_trace(path)
    by_op = {r["operation"]: r for r in records}
    assert [r["operation"] for r in records] == ["inner", "broken", "outer"]
    assert by_op["inner"]["outcome"] == "case_not_found"
    assert by_op["broken"]["outcome"] == "unexpected_error"
    assert by_op["outer"]["outcome"] == "ok"
    assert {r["trace_id"] for r in records} == {"fictional-trace"}
    assert by_op["inner"]["parent_span_id"] == by_op["outer"]["span_id"]
    assert by_op["outer"]["parent_span_id"] is None
    assert by_op["outer"]["attrs"]["name"].startswith("hmac-sha256:")
    assert by_op["outer"]["attrs"]["items"] == 2
    assert all(r["duration_ms"] >= 0 for r in records)
    assert o.tracer.written.total() == 3
    assert o.tracer.span_total.value(operation="inner", outcome="case_not_found") == 1
    assert "Luma" not in path.read_text(encoding="utf-8")


def test_tracer_rotates_at_max_bytes(tmp_path):
    path = tmp_path / "trace.jsonl"
    o = obs.Observability(trace_path=path, trace_max_bytes=4096)
    for _ in range(60):
        with o.span("rotate-check", note="x"):
            pass
    rotated = tmp_path / "trace.jsonl.1"
    assert rotated.exists() and path.exists()
    assert path.stat().st_size <= 4096
    assert all(json.loads(line) for line in path.read_text("utf-8").splitlines())


def test_tracer_sink_failure_drops_and_counts(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="watchlist.observability")
    o = obs.Observability(trace_path=tmp_path)  # a directory: every write fails
    for _ in range(3):
        with o.span("sink-down"):
            pass
    assert o.tracer.dropped.total() == 3 and o.tracer.written.total() == 0
    assert o.tracer.span_total.value(operation="sink-down", outcome="ok") == 3
    warnings = [r for r in caplog.records if r.name == "watchlist.observability"]
    assert len(warnings) == 1  # warned once, not per record


def test_tracer_without_path_only_counts():
    o = obs.Observability()
    with o.span("memory-only"):
        pass
    assert o.tracer.written.total() == 0 and o.tracer.dropped.total() == 0
    assert o.tracer.span_seconds.count(operation="memory-only") == 1


def test_outcome_code():
    assert obs.outcome_code(None) == "ok"
    assert obs.outcome_code(NotFoundError("x")) == "case_not_found"
    assert obs.outcome_code(ValueError("x")) == "unexpected_error"


# -- instrumented service ------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []
        self.flag = "original"

    def get_case(self, case_id):
        self.calls.append(case_id)
        return {"case_id": case_id}

    def helper(self):
        return "not traced"


def test_instrumented_service_is_transparent(tmp_path):
    o = obs.Observability(trace_path=tmp_path / "t.jsonl")
    inner = _Recorder()
    proxy = o.instrument(inner)
    assert o.instrument(proxy) is proxy
    assert proxy.get_case("'; DROP TABLE cases;--") == {
        "case_id": "'; DROP TABLE cases;--"
    }
    assert proxy.helper() == "not traced"
    proxy.flag = "changed"
    assert inner.flag == "changed" and proxy.wrapped is inner
    (record,) = obs.read_trace(tmp_path / "t.jsonl")
    assert record["operation"] == "review.get_case"
    assert record["attrs"]["case_id"].startswith("invalid:hmac-sha256:")
    assert "DROP" not in json.dumps(record)


# -- settings ------------------------------------------------------------------------


def test_settings_from_env_observability_fields(tmp_path):
    entry = f"a:viewer:{hash_key('a' * 20)}"
    settings = ApiSettings.from_env(
        workspace=None,
        config_path=None,
        environ={
            "WATCHLIST_API_KEYS": entry,
            "WATCHLIST_API_MAX_IN_FLIGHT": "7",
            "WATCHLIST_TRACE_PATH": str(tmp_path / "trace.jsonl"),
            "WATCHLIST_LOG_FORMAT": "JSON",
            "WATCHLIST_API_BODY_TIMEOUT_SECONDS": "2.5",
        },
    )
    assert settings.body_timeout_seconds == 2.5
    assert settings.max_in_flight == 7
    assert settings.trace_path == tmp_path / "trace.jsonl"
    assert settings.log_format == "json"
    defaults = ApiSettings.from_env(
        workspace=None, config_path=None, environ={"WATCHLIST_API_KEYS": entry}
    )
    assert defaults.max_in_flight == 64 and defaults.trace_path is None
    assert defaults.log_format == "text"
    assert defaults.body_timeout_seconds == 10.0


@pytest.mark.parametrize(
    "changes",
    [
        {"max_in_flight": 0},
        {"max_in_flight": True},
        {"trace_path": "not-a-path-object"},
        {"trace_max_bytes": 10},
        {"log_format": "xml"},
        {"body_timeout_seconds": 0},
        {"body_timeout_seconds": 301},
        {"body_timeout_seconds": float("nan")},
        {"body_timeout_seconds": True},
    ],
)
def test_invalid_observability_settings(changes):
    with pytest.raises(SettingsError):
        ApiSettings(**changes)


# -- API integration -----------------------------------------------------------------


def test_request_id_always_generated(tmp_path):
    with TestClient(build(tmp_path)) as client:
        given = client.get("/health", headers={"X-Request-ID": "fictional-req-001"})
        assert given.headers["x-request-id"] != "fictional-req-001"
        generated = client.get("/health", headers={"X-Request-ID": "bad id"})
        value = generated.headers["x-request-id"]
        assert value != "bad id" and len(value) == 32
        assert client.get("/health").headers["x-request-id"] != value
        for response in (
            client.get("/v2/cases"),
            client.get("/nope"),
            client.get("/v2/cases/not-a-uuid", headers=AUTH),
            client.post("/v2/cases", content=b"x" * 70000, headers=AUTH),
        ):
            assert response.status_code >= 400
            assert obs.accept_request_id(response.headers["x-request-id"])
            assert response.headers.get_list("x-request-id") == [
                response.headers["x-request-id"]
            ]


def test_request_id_matching_the_credential_is_not_echoed(tmp_path):
    with TestClient(build(tmp_path)) as client:
        response = client.get(
            "/v2/cases", headers={**VIEW_AUTH, "X-Request-ID": VIEWER}
        )
        assert response.status_code == 200
        assert response.headers["x-request-id"] != VIEWER


def test_api_key_sent_only_as_request_id_is_not_echoed_or_traced(tmp_path):
    # Reviewer side note: a key sent by mistake as X-Request-ID (no
    # Authorization header) must not be echoed, logged or written as trace_id.
    trace = tmp_path / "trace.jsonl"
    with TestClient(build(tmp_path, trace_path=trace)) as client:
        for headers in (
            {"X-Request-ID": REVIEWER},
            {"X-Request-ID": VIEWER, "X-Api-Key": VIEWER},
        ):
            response = client.get("/v2/cases", headers=headers)
            assert response.status_code == 401
            assert response.headers["x-request-id"] not in (REVIEWER, VIEWER)
        kept = client.get("/health", headers={"X-Request-ID": "fictional-req-002"})
        assert kept.headers["x-request-id"] != "fictional-req-002"
    text = trace.read_text(encoding="utf-8")
    assert REVIEWER not in text and VIEWER not in text


def test_request_and_service_spans_share_the_trace(tmp_path):
    trace = tmp_path / "trace.jsonl"
    app = build(tmp_path, trace_path=trace)
    with TestClient(app) as client:
        response = client.post(
            "/v2/cases",
            json={"name": "Luma Vesper", "origin": "test"},
            headers={**AUTH, "X-Request-ID": "fictional-start-1"},
        )
        assert response.status_code == 200
        case_id = response.json()["case"]["case_id"]
        missing = client.get(
            "/v2/cases/00000000-0000-4000-8000-000000000000",
            headers={**AUTH, "X-Request-ID": "fictional-missing-1"},
        )
        assert missing.status_code == 404
    records = obs.read_trace(trace)
    start = [r for r in records if r["trace_id"] == response.headers["x-request-id"]]
    assert {r["operation"] for r in start} == {
        "http POST /v2/cases",
        "review.start_case",
    }
    http = next(r for r in start if r["operation"].startswith("http"))
    service = next(r for r in start if r["operation"] == "review.start_case")
    # The service ran in the write-queue thread yet kept the request context.
    assert service["parent_span_id"] == http["span_id"]
    assert service["attrs"]["case_id"] == case_id
    assert service["attrs"]["status"] == "awaiting_review"
    assert service["attrs"]["actor"] == "fictional-reviewer"
    assert http["attrs"] == {"method": "POST", "route": "/v2/cases", "status": 200}
    lost = [r for r in records if r["trace_id"] == missing.headers["x-request-id"]]
    assert {r["outcome"] for r in lost} == {"case_not_found"}
    assert "Luma" not in trace.read_text(encoding="utf-8")


def test_metrics_endpoint_requires_a_key_and_uses_route_templates(tmp_path):
    with TestClient(build(tmp_path)) as client:
        assert client.get("/metrics").status_code == 401
        bad = client.get("/metrics", headers={"Authorization": "Bearer x"})
        assert bad.status_code == 401
        client.post(
            "/v2/cases", json={"name": "Luma Vesper", "origin": "test"}, headers=AUTH
        )
        client.get("/v2/cases/00000000-0000-4000-8000-000000000000", headers=AUTH)
        client.get("/v2/cases/..%2F..%2Fetc", headers=AUTH)
        response = client.get("/metrics", headers=VIEW_AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == obs.PROMETHEUS_CONTENT_TYPE
    assert response.headers["x-content-type-options"] == "nosniff"
    text = response.text
    assert (
        'watchlist_http_requests_total{method="POST",route="/v2/cases",'
        'status="200",code="ok"} 1'
    ) in text
    assert (
        'watchlist_http_requests_total{method="GET",route="/v2/cases/{case_id}",'
        'status="404",code="case_not_found"} 1'
    ) in text
    assert 'route="unrouted",status="404",code="not_found"' in text
    assert 'watchlist_spans_total{operation="review.start_case",outcome="ok"} 1' in text
    assert "watchlist_write_queue_outstanding 0" in text
    assert "watchlist_write_queue_limit 8" in text
    assert "etc" not in text and REVIEWER not in text and VIEWER not in text
    # Every sample line is "name{labels} value" or "name value".
    for line in text.splitlines():
        if not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            float(value.replace("+Inf", "inf"))


def test_health_and_ready(tmp_path):
    with TestClient(build(tmp_path)) as client:
        health = client.get("/health")
        assert health.status_code == 200 and health.json()["status"] == "ok"
        ready = client.get("/ready", headers=AUTH)
        assert ready.status_code == 200
        body = ready.json()
        assert body["status"] == "ready" and body["limitation"] == LIMITATION
        assert body["checks"] == {"workspace_read_only_open": "ok"}


def test_ready_reports_an_unusable_workspace_without_paths(tmp_path):
    app = build(tmp_path)
    with TestClient(app) as client:
        db = next(p for p in (tmp_path / "ws").iterdir() if p.suffix == ".sqlite")
        moved = db.with_name(db.name + ".moved")
        db.rename(moved)
        try:
            response = client.get("/ready", headers=AUTH)
            assert response.status_code == 503
            body = response.json()
            assert body["code"] == "not_ready"
            assert body["details"] == {"cause": "workspace_invalid"}
            assert body["retryable"] is False
            assert str(tmp_path) not in response.text
            assert client.get("/health").status_code == 200  # liveness unaffected
        finally:
            moved.rename(db)
        assert client.get("/ready", headers=AUTH).status_code == 200


def test_readiness_probe_coalesces_and_caches_only_healthy_results():
    from watchlist.api.app import ReadinessProbe

    results = iter(["ok", RuntimeError("broken"), "busy"])
    clock = {"calls": 0}

    def check():
        clock["calls"] += 1
        item = next(results)
        if isinstance(item, Exception):
            raise item
        return item

    probe = ReadinessProbe(check, cache_seconds=60.0)
    assert probe.cached() is None
    assert probe.state() == "ok" and probe.state() == "ok"
    assert probe.cached() == "ok" and probe.runs == 1
    probe._cached = None  # expire
    with pytest.raises(RuntimeError):
        probe.state()
    assert probe.cached() is None  # failures are not cached
    assert probe.state() == "busy" and probe.runs == 3


def test_ready_and_metrics_stay_out_of_the_published_schema():
    paths = openapi_document()["paths"]
    assert "/ready" not in paths and "/metrics" not in paths
    assert "/health" in paths


def test_json_log_format_setting_installs_the_handler(tmp_path):
    logger = logging.getLogger("watchlist")
    before = list(logger.handlers)
    level = logger.level
    try:
        build(tmp_path, log_format="json")
        marked = [
            h for h in logger.handlers if getattr(h, "_watchlist_json_handler", False)
        ]
        assert len(marked) == 1
    finally:
        for handler in list(logger.handlers):
            if handler not in before:
                logger.removeHandler(handler)
        logger.setLevel(level)


def test_health_request_ids_cannot_probe_valid_api_keys(tmp_path, monkeypatch):
    from watchlist.api.auth import KeyStore

    calls = []
    original = KeyStore.authenticate

    def record(self, token):
        calls.append(token)
        return original(self, token)

    monkeypatch.setattr(KeyStore, "authenticate", record)
    with TestClient(build(tmp_path)) as client:
        for guess in (REVIEWER, VIEWER, "incorrect-fictional-key-guess"):
            response = client.get("/health", headers={"X-Request-ID": guess})
            assert response.status_code == 200
            assert response.headers["x-request-id"] != guess
            assert len(response.headers["x-request-id"]) == 32
    assert calls == []
