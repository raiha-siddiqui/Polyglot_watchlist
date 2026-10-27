"""Step 27 load shedding and failure behaviour of the review API (fictional data).

Overload must end in structured ``429``/``503`` errors with ``Retry-After``,
never ``500``; a workspace locked by another process is ``503 workspace_busy``
(retryable); storage and trace-sink failures are reported without corrupting
state; the load smoke script runs and reports percentiles.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import importlib.util
import os
from pathlib import Path
import secrets
import stat
import subprocess
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from review_v2_support import (
    ROOT,
    FakeRetriever,
    make_config,
    worker_command,
    worker_env,
)
from watchlist import observability as obs
from watchlist.api import ApiSettings, create_app
from watchlist.api import app as app_module
from watchlist.api.auth import ApiKey, hash_key
from watchlist.review.service import ReviewService

REVIEWER = secrets.token_urlsafe(24)
KEYS = (ApiKey("fictional-reviewer", "reviewer", hash_key(REVIEWER)),)
AUTH = {"Authorization": f"Bearer {REVIEWER}"}
START = {"name": "Luma Vesper", "origin": "test"}


class GatedRetriever(FakeRetriever):
    """Blocks inside ``search`` until released, holding the single write worker."""

    def __init__(self):
        super().__init__()
        self.gate = threading.Event()

    def search(self, normalized_name, *, top_k):
        assert self.gate.wait(30), "test did not release the retriever"
        return super().search(normalized_name, top_k=top_k)


class BlockingReads:
    """Service stand-in whose ``list_cases`` waits on a gate (holds reads in flight)."""

    def __init__(self, service):
        self._service = service
        self.gate = threading.Event()
        self.entered = 0
        self._lock = threading.Lock()

    def __getattr__(self, name):
        return getattr(self._service, name)

    def list_cases(self, *args, **kwargs):
        with self._lock:
            self.entered += 1
        assert self.gate.wait(30), "test did not release the reads"
        return self._service.list_cases(*args, **kwargs)


def wait_until(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def new_service(tmp_path, retriever=None, **config):
    return ReviewService.initialize(
        tmp_path / "ws", make_config(**config), retriever=retriever or FakeRetriever()
    )


def statuses(responses):
    return sorted(r.status_code for r in responses)


# -- overload is 429, never 500 ------------------------------------------------------


def test_concurrent_writes_beyond_the_queue_get_429_not_500(tmp_path):
    retriever = GatedRetriever()
    app = create_app(
        ApiSettings(api_keys=KEYS, write_queue_max=2),
        service=new_service(tmp_path, retriever),
    )
    total = 20
    with TestClient(app) as client, ThreadPoolExecutor(total) as pool:
        futures = [
            pool.submit(client.post, "/v2/cases", json=START, headers=AUTH)
            for _ in range(total)
        ]
        # 1 running + 2 waiting are admitted; the other 17 are refused at once.
        wait_until(lambda: sum(f.done() for f in futures) == total - 3)
        retriever.gate.set()
        responses = [f.result(60) for f in futures]
        assert statuses(responses) == [200] * 3 + [429] * (total - 3)
        for response in responses:
            if response.status_code == 429:
                body = response.json()
                assert body["code"] == "queue_full" and body["retryable"] is True
                assert response.headers["retry-after"] == "1"
        listing = client.get("/v2/cases", headers=AUTH).json()["cases"]
        assert len(listing) == 3
        metrics = client.get("/metrics", headers=AUTH).text
    assert f'status="429",code="queue_full"}} {total - 3}' in metrics
    assert 'status="500"' not in metrics


def test_concurrent_reads_beyond_the_in_flight_limit_get_429(tmp_path):
    blocking = BlockingReads(new_service(tmp_path))
    app = create_app(ApiSettings(api_keys=KEYS, max_in_flight=4), service=blocking)
    total = 12
    with TestClient(app) as client, ThreadPoolExecutor(total) as pool:
        futures = [
            pool.submit(client.get, "/v2/cases", headers=AUTH) for _ in range(total)
        ]
        wait_until(lambda: sum(f.done() for f in futures) == total - 4)
        assert blocking.entered == 4
        # Liveness stays observable while saturated; readiness is shed like reads.
        assert client.get("/health").status_code == 200
        shed = client.get("/ready", headers=AUTH)
        assert shed.status_code == 429 and shed.json()["code"] == "too_many_requests"
        blocking.gate.set()
        responses = [f.result(60) for f in futures]
    assert statuses(responses) == [200] * 4 + [429] * (total - 4)
    refused = next(r for r in responses if r.status_code == 429)
    body = refused.json()
    assert body == {
        "code": "too_many_requests",
        "message": "Too many requests are being handled; retry later.",
        "retryable": True,
        "details": {"limit": 4, "retry_after_seconds": 1},
    }
    assert refused.headers["retry-after"] == "1"
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert obs.accept_request_id(refused.headers["x-request-id"])


def test_many_concurrent_mixed_requests_never_500(tmp_path):
    app = create_app(ApiSettings(api_keys=KEYS), service=new_service(tmp_path))
    with TestClient(app) as client:
        case_id = client.post("/v2/cases", json=START, headers=AUTH).json()["case"][
            "case_id"
        ]
        calls = []
        for index in range(120):
            kind = index % 4
            if kind == 0:
                calls.append(lambda: client.get(f"/v2/cases/{case_id}", headers=AUTH))
            elif kind == 1:
                calls.append(lambda: client.get("/v2/cases", headers=AUTH))
            elif kind == 2:
                calls.append(
                    lambda: client.get(f"/v2/cases/{case_id}/audit", headers=AUTH)
                )
            else:
                calls.append(lambda: client.post("/v2/cases", json=START, headers=AUTH))
        with ThreadPoolExecutor(32) as pool:
            responses = list(pool.map(lambda call: call(), calls))
        codes = {r.status_code for r in responses}
        # Observed: with 32 threads the exclusive workspace lock (20 ms polling,
        # no fairness) can starve a read past the 5 s lock timeout. That must
        # surface as retryable 503 workspace_busy, never as 500.
        assert codes <= {200, 429, 503}, codes
        for response in responses:
            if response.status_code == 503:
                body = response.json()
                assert body["code"] == "workspace_busy" and body["retryable"]
                assert response.headers["retry-after"] == "1"
        # The success fraction during contention depends on scheduling, not safety.
        # Once the burst drains, EVERY caller must be able to complete; this also
        # proves the service did not leave its queue or lock stuck after shedding.
        for call, response in zip(calls, responses, strict=True):
            if response.status_code != 200:
                response = call()
            assert response.status_code == 200, response.text
        metrics = client.get("/metrics", headers=AUTH).text
        verified = client.get(f"/v2/cases/{case_id}/audit/verify", headers=AUTH)
    assert verified.json()["verification"]["ok"] is True
    assert 'status="500"' not in metrics


# -- readiness cannot be used against real requests ---------------------------------


def test_ready_stays_200_while_a_write_holds_the_lock(tmp_path):
    """Regression (reviewer finding 1A): one slow write made /ready 503."""
    retriever = GatedRetriever()
    app = create_app(
        ApiSettings(api_keys=KEYS), service=new_service(tmp_path, retriever)
    )
    with TestClient(app) as client, ThreadPoolExecutor(1) as pool:
        pending = pool.submit(client.post, "/v2/cases", json=START, headers=AUTH)
        wait_until(lambda: app.state.runtime.writes.outstanding == 1)
        time.sleep(0.2)  # the write is inside retrieval, holding the lock
        try:
            started = time.monotonic()
            ready = client.get("/ready", headers=AUTH)
            assert time.monotonic() - started < 2.0
            assert ready.status_code == 200, ready.text
            assert ready.json()["checks"]["workspace_read_only_open"] in ("ok", "busy")
        finally:
            retriever.gate.set()
        assert pending.result(60).status_code == 200
        assert client.get("/ready", headers=AUTH).status_code == 200


def test_ready_flood_does_not_make_authenticated_reads_fail(tmp_path):
    """Concurrent probe bursts must coalesce, spare the lock and allow real reads.

    An injected cache clock gives three exact windows. A gate holds the first
    probe in each window while authenticated reads and the rest of the burst
    run. No unbounded CPU flood or elapsed-time ratio decides this regression.
    """
    app = create_app(ApiSettings(api_keys=KEYS), service=new_service(tmp_path))
    probe = app.state.readiness
    now = [0.0]
    probe._clock = lambda: now[0]
    check = probe._check
    entered, release = threading.Event(), threading.Event()

    def gated_check():
        entered.set()
        assert release.wait(60), "test did not release the readiness check"
        return check()

    probe._check = gated_check
    with TestClient(app) as client, ThreadPoolExecutor(14) as pool:
        case_id = client.post("/v2/cases", json=START, headers=AUTH).json()["case"][
            "case_id"
        ]
        for window in range(3):
            now[0] = window * (app_module.READY_CACHE_SECONDS + 1)
            entered.clear()
            release.clear()
            probes = [pool.submit(client.get, "/ready", headers=AUTH) for _ in range(8)]
            try:
                assert entered.wait(60), "readiness check did not start"
                reads = [
                    pool.submit(client.get, f"/v2/cases/{case_id}", headers=AUTH)
                    for _ in range(6)
                ]
                responses = [f.result(60) for f in reads]
                assert [r.status_code for r in responses] == [200] * 6
                assert all(r.json()["case"]["case_id"] == case_id for r in responses)
            finally:
                release.set()
            assert [f.result(60).status_code for f in probes] == [200] * 8
            # A second burst in this exact window must use the same result.
            cached = [
                pool.submit(client.get, "/ready", headers=AUTH) for _ in range(16)
            ]
            assert [f.result(60).status_code for f in cached] == [200] * 16
            assert probe.runs == window + 1
        assert client.get(f"/v2/cases/{case_id}/audit/verify", headers=AUTH).json()[
            "verification"
        ]["ok"]


# -- workspace lock held by another process -----------------------------------------


def test_workspace_locked_by_another_process_is_503_retryable(tmp_path):
    service = new_service(tmp_path, lock_timeout=0.2)
    app = create_app(ApiSettings(api_keys=KEYS), service=service)
    with TestClient(app) as client:
        case_id = client.post("/v2/cases", json=START, headers=AUTH).json()["case"][
            "case_id"
        ]
        holder = subprocess.Popen(
            worker_command("hold-lock", str(tmp_path / "ws"), "none"),
            cwd=ROOT,
            env=worker_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            assert holder.stdout.readline().strip() == "locked"
            for response in (
                client.get(f"/v2/cases/{case_id}", headers=AUTH),
                client.post("/v2/cases", json=START, headers=AUTH),
            ):
                assert response.status_code == 503, response.text
                body = response.json()
                assert body["code"] == "workspace_busy" and body["retryable"] is True
                assert response.headers["retry-after"] == "1"
                assert str(tmp_path) not in response.text
            # A held lock means an operation is running, not that this
            # instance is unusable: ready, with the lock check reported busy.
            ready = client.get("/ready", headers=AUTH)
            assert ready.status_code == 200, ready.text
            assert ready.json()["checks"] == {"workspace_read_only_open": "busy"}
            assert client.get("/health").status_code == 200
        finally:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(30)
            for stream in (holder.stdin, holder.stdout, holder.stderr):
                stream.close()
        assert holder.returncode == 0
        assert client.get("/ready", headers=AUTH).status_code == 200
        assert client.get(f"/v2/cases/{case_id}", headers=AUTH).status_code == 200
        assert client.post("/v2/cases", json=START, headers=AUTH).status_code == 200
        metrics = client.get("/metrics", headers=AUTH).text
    assert 'outcome="workspace_busy"' in metrics


# -- storage and sink failures --------------------------------------------------------


def _database(tmp_path: Path) -> Path:
    return next(p for p in (tmp_path / "ws").iterdir() if p.suffix == ".sqlite")


def test_read_only_database_file_fails_writes_without_corruption(tmp_path):
    """Simulates a read-only volume or a permissions mistake on the database."""
    app = create_app(ApiSettings(api_keys=KEYS), service=new_service(tmp_path))
    database = _database(tmp_path)
    with TestClient(app) as client:
        first = client.post("/v2/cases", json=START, headers=AUTH).json()
        case_id = first["case"]["case_id"]
        mode = database.stat().st_mode
        os.chmod(database, stat.S_IREAD)
        try:
            try:
                with open(database, "r+b"):
                    writable = True
            except OSError:
                writable = False
            if writable:  # e.g. running as root on POSIX: cannot simulate
                pytest.skip("cannot make the database read-only for this user")
            refused = client.post("/v2/cases", json=START, headers=AUTH)
            assert refused.status_code == 503, refused.text
            assert refused.json()["code"] == "workspace_invalid"
            body = refused.json()
            assert set(body) == {"code", "message", "retryable", "details"}
            assert body["details"] == {}
            assert str(tmp_path) not in refused.text
            # Reads and readiness keep working on a read-only database.
            assert client.get(f"/v2/cases/{case_id}", headers=AUTH).status_code == 200
            assert client.get("/ready", headers=AUTH).status_code == 200
        finally:
            os.chmod(database, mode | stat.S_IWRITE)
        cases = client.get("/v2/cases", headers=AUTH).json()["cases"]
        assert [item["case_id"] for item in cases] == [case_id]
        again = client.post("/v2/cases", json=START, headers=AUTH)
        assert again.status_code == 200
        for item in (case_id, again.json()["case"]["case_id"]):
            verify = client.get(f"/v2/cases/{item}/audit/verify", headers=AUTH)
            assert verify.json()["verification"]["ok"] is True


def test_trace_sink_on_a_missing_directory_never_breaks_requests(tmp_path):
    trace = tmp_path / "missing-dir" / "trace.jsonl"
    app = create_app(
        ApiSettings(api_keys=KEYS, trace_path=trace), service=new_service(tmp_path)
    )
    with TestClient(app) as client:
        assert client.post("/v2/cases", json=START, headers=AUTH).status_code == 200
        assert client.get("/v2/cases", headers=AUTH).status_code == 200
        metrics = client.get("/metrics", headers=AUTH).text
    assert not trace.parent.exists()  # never created implicitly
    dropped = [
        line
        for line in metrics.splitlines()
        if line.startswith("watchlist_trace_records_dropped_total ")
    ]
    assert dropped and float(dropped[0].split()[1]) >= 4


def test_disk_full_on_the_trace_sink_is_counted_not_raised(tmp_path, monkeypatch):
    trace = tmp_path / "trace.jsonl"
    app = create_app(
        ApiSettings(api_keys=KEYS, trace_path=trace), service=new_service(tmp_path)
    )
    observability = app.state.observability

    def full(_line):
        raise OSError(errno.ENOSPC, "No space left on device")

    with TestClient(app) as client:
        assert client.get("/v2/cases", headers=AUTH).status_code == 200
        written = observability.tracer.written.total()
        monkeypatch.setattr(observability.tracer, "_append", full)
        response = client.post("/v2/cases", json=START, headers=AUTH)
        assert response.status_code == 200
        assert observability.tracer.dropped.total() >= 2
        monkeypatch.undo()
        assert client.get("/v2/cases", headers=AUTH).status_code == 200
        assert observability.tracer.written.total() > written


# -- the load smoke script ------------------------------------------------------------


def test_load_smoke_script_reports_percentiles():
    spec = importlib.util.spec_from_file_location(
        "watchlist_load_smoke_test", ROOT / "scripts" / "load_smoke.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.percentile([], 50) is None
    assert module.percentile([3.0, 1.0, 2.0, 4.0], 50) == 2.0
    assert module.percentile(list(range(1, 101)), 95) == 95
    report = module.run(6, 3, seed=1)
    assert report["label"].startswith("local in-process fictional measurement")
    for phase in report["phases"].values():
        assert phase["requests"] == 6
        assert phase["server_errors_5xx"] == 0
        assert phase["internal_errors_500"] == 0
        assert phase["latency_ms"]["p50"] <= phase["latency_ms"]["p95"]
        assert phase["latency_ms"]["p95"] <= phase["latency_ms"]["max"]
    assert sum(report["phases"]["start_case"]["status_counts"].values()) == 6
