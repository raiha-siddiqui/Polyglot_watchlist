"""Concurrency, write-queue limits, HTTP waits and restart behaviour of the API."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import secrets
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from review_v2_support import CONFIG_PATH, ROOT, FakeRetriever, make_config, worker_env
from watchlist.api import ApiSettings, create_app
from watchlist.api.auth import ApiKey, hash_key
from watchlist.api.settings import ENV_KEYS as KEYS_VAR, ENV_KEYS_FILE as KEYS_FILE_VAR
from watchlist.review.service import ReviewService

REVIEWER = secrets.token_urlsafe(24)
REVIEWER_2 = secrets.token_urlsafe(24)
KEYS = (
    ApiKey("fictional-reviewer", "reviewer", hash_key(REVIEWER)),
    ApiKey("fictional-reviewer-2", "reviewer", hash_key(REVIEWER_2)),
)
AUTH = {"Authorization": f"Bearer {REVIEWER}"}
AUTH_2 = {"Authorization": f"Bearer {REVIEWER_2}"}
START = {"name": "Luma Vesper", "origin": "test"}


class GatedRetriever(FakeRetriever):
    """Blocks inside ``search`` until released, to hold the single write worker."""

    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.gate = threading.Event()

    def search(self, normalized_name, *, top_k):
        self.entered.set()
        assert self.gate.wait(30), "test did not release the retriever"
        return super().search(normalized_name, top_k=top_k)


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def form(case, **changes):
    body = {
        "submission_id": str(uuid4()),
        "revision": case["revision"],
        "packet_sha256": case["case"]["packet_sha256"],
        "outcome": "candidate_for_follow_up",
        "candidate_entity_id": "syn-001",
        "reason": "Fictional concurrency check.",
    }
    body.update(changes)
    return body


def race(*calls):
    """Run zero-argument callables at the same moment; return their results."""
    barrier = threading.Barrier(len(calls))

    def run(call):
        barrier.wait(10)
        return call()

    with ThreadPoolExecutor(len(calls)) as pool:
        return [f.result(60) for f in [pool.submit(run, call) for call in calls]]


def review_count(client, case_id):
    events = client.get(f"/v2/cases/{case_id}/audit", headers=AUTH).json()["audit"][
        "events"
    ]
    return sum(e["event_type"] == "review_submitted" for e in events)


@pytest.fixture
def workspace(tmp_path):
    ReviewService.initialize(tmp_path / "ws", make_config(lock_timeout=10.0))
    return tmp_path / "ws"


def app_for(workspace, retriever=None, **settings):
    service = ReviewService.open(
        workspace,
        make_config(lock_timeout=10.0),
        retriever=retriever or FakeRetriever(),
    )
    return create_app(ApiSettings(api_keys=KEYS, **settings), service=service)


# -- exactly one accepted review --------------------------------------------------------


def test_two_concurrent_submissions_accept_exactly_one(workspace):
    with TestClient(app_for(workspace)) as client:
        case = client.post("/v2/cases", json=START, headers=AUTH).json()
        case_id = case["case"]["case_id"]
        responses = race(
            lambda: client.post(
                f"/v2/cases/{case_id}/review", json=form(case), headers=AUTH
            ),
            lambda: client.post(
                f"/v2/cases/{case_id}/review",
                json=form(case, outcome="none_selected", candidate_entity_id=None),
                headers=AUTH_2,
            ),
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200, 409], [r.text for r in responses]
        loser = next(r for r in responses if r.status_code == 409)
        assert loser.json()["code"] == "stale_submission"
        assert review_count(client, case_id) == 1


class _StaleReads:
    """Service proxy whose ``get_case`` keeps returning a pre-review snapshot.

    This defeats the API's own revision pre-check, so both racing forms reach
    ``ReviewService.submit_review`` and only the service's one-review-per-case
    guard can reject the loser.
    """

    def __init__(self, service, snapshot):
        self._service = service
        self._snapshot = snapshot
        self.submits = 0

    def get_case(self, case_id):
        if case_id == self._snapshot.header.case_id:
            return self._snapshot
        return self._service.get_case(case_id)

    def submit_review(self, case_id, form):
        self.submits += 1
        return self._service.submit_review(case_id, form)

    def __getattr__(self, name):
        return getattr(self._service, name)


def test_service_guard_rejects_second_review_when_precheck_is_bypassed(workspace):
    app = app_for(workspace)
    runtime = app.state.runtime
    with TestClient(app) as client:
        case = client.post("/v2/cases", json=START, headers=AUTH).json()
        case_id = case["case"]["case_id"]
        proxy = _StaleReads(runtime.service, runtime.service.get_case(case_id))
        runtime.service = proxy
        responses = race(
            lambda: client.post(
                f"/v2/cases/{case_id}/review", json=form(case), headers=AUTH
            ),
            lambda: client.post(
                f"/v2/cases/{case_id}/review",
                json=form(case, outcome="none_selected", candidate_entity_id=None),
                headers=AUTH_2,
            ),
        )
        assert proxy.submits == 2
        assert sorted(r.status_code for r in responses) == [200, 409]
        loser = next(r for r in responses if r.status_code == 409).json()
        assert loser["code"] == "stale_submission"
        assert "already has an accepted review" in loser["message"]
        assert review_count(client, case_id) == 1


def test_two_apps_on_one_workspace_accept_exactly_one(workspace):
    with TestClient(app_for(workspace)) as one, TestClient(app_for(workspace)) as two:
        case = one.post("/v2/cases", json=START, headers=AUTH).json()
        case_id = case["case"]["case_id"]
        assert (
            two.get(f"/v2/cases/{case_id}", headers=AUTH).json()["revision"]
            == case["revision"]
        )
        responses = race(
            lambda: one.post(
                f"/v2/cases/{case_id}/review", json=form(case), headers=AUTH
            ),
            lambda: two.post(
                f"/v2/cases/{case_id}/review", json=form(case), headers=AUTH_2
            ),
        )
        assert sorted(r.status_code for r in responses) == [200, 409]
        assert {r.json().get("code") for r in responses if r.status_code == 409} == {
            "stale_submission"
        }
        assert review_count(one, case_id) == 1


def test_duplicate_concurrent_retry_is_idempotent(workspace):
    with TestClient(app_for(workspace)) as one, TestClient(app_for(workspace)) as two:
        case = one.post("/v2/cases", json=START, headers=AUTH).json()
        case_id = case["case"]["case_id"]
        body = form(case)
        responses = race(
            lambda: one.post(f"/v2/cases/{case_id}/review", json=body, headers=AUTH),
            lambda: two.post(f"/v2/cases/{case_id}/review", json=body, headers=AUTH),
        )
        assert [r.status_code for r in responses] == [200, 200]
        assert responses[0].json()["case"] == responses[1].json()["case"]
        assert review_count(one, case_id) == 1


def test_concurrent_start_with_one_request_id_creates_one_case(workspace):
    body = {**START, "request_id": str(uuid4())}
    with TestClient(app_for(workspace)) as one, TestClient(app_for(workspace)) as two:
        responses = race(
            lambda: one.post("/v2/cases", json=body, headers=AUTH),
            lambda: two.post("/v2/cases", json=body, headers=AUTH),
        )
        assert [r.status_code for r in responses] == [200, 200]
        assert len({r.json()["case"]["case_id"] for r in responses}) == 1
        assert len(one.get("/v2/cases", headers=AUTH).json()["cases"]) == 1


# -- bounded queue and HTTP wait ---------------------------------------------------------


def test_full_write_queue_returns_429(workspace):
    retriever = GatedRetriever()
    app = app_for(workspace, retriever, write_queue_max=1)
    writes = app.state.runtime.writes
    with TestClient(app) as client, ThreadPoolExecutor(2) as pool:
        running = pool.submit(client.post, "/v2/cases", json=START, headers=AUTH)
        assert retriever.entered.wait(10)
        queued = pool.submit(
            client.post,
            "/v2/cases",
            json={**START, "name": "Mira Velune"},
            headers=AUTH,
        )
        wait_until(lambda: writes.outstanding == 2)
        refused = client.post(
            "/v2/cases", json={**START, "name": "Naro Zefir"}, headers=AUTH
        )
        assert refused.status_code == 429
        body = refused.json()
        assert body["code"] == "queue_full" and body["retryable"] is True
        assert body["details"]["limit"] == 1
        assert refused.headers["retry-after"] == "1"
        retriever.gate.set()
        assert running.result(30).status_code == 200
        assert queued.result(30).status_code == 200
        names = [
            client.get(f"/v2/cases/{c['case_id']}", headers=AUTH).json()["case"][
                "header"
            ]["name"]
            for c in client.get("/v2/cases", headers=AUTH).json()["cases"]
        ]
        assert sorted(names) == ["Luma Vesper", "Mira Velune"]


def test_default_queue_admits_eight_waiting_writes():
    from watchlist.api.app import WriteQueue
    from watchlist.api.errors import ApiError

    queue = WriteQueue(ApiSettings().write_queue_max)
    gate = threading.Event()
    futures = [queue.submit(gate.wait) for _ in range(9)]
    with pytest.raises(ApiError) as caught:
        queue.submit(gate.wait)
    assert caught.value.status == 429 and caught.value.code == "queue_full"
    gate.set()
    for future in futures:
        future.result(10)
    queue.shutdown()
    assert queue.outstanding == 0


def test_http_wait_expiry_is_retryable_and_never_duplicates(workspace):
    retriever = GatedRetriever()
    app = app_for(workspace, retriever, write_wait_seconds=0.3)
    writes = app.state.runtime.writes
    body = {**START, "request_id": str(uuid4())}
    with TestClient(app) as client:
        pending = client.post("/v2/cases", json=body, headers=AUTH)
        assert pending.status_code == 503
        assert pending.json()["code"] == "operation_pending"
        assert pending.json()["retryable"] is True
        retriever.gate.set()
        wait_until(lambda: writes.outstanding == 0)
        retry = client.post("/v2/cases", json=body, headers=AUTH)
        assert retry.status_code == 200
        assert retry.json()["case"]["status"] == "awaiting_review"
        assert len(client.get("/v2/cases", headers=AUTH).json()["cases"]) == 1
        assert retriever.calls == 1


def test_http_wait_expiry_without_request_id_is_not_retryable(workspace):
    # Regression: a start without request_id is not idempotent, so the pending
    # error must not invite a blind retry (which would create a second case).
    retriever = GatedRetriever()
    app = app_for(workspace, retriever, write_wait_seconds=0.3)
    writes = app.state.runtime.writes
    with TestClient(app) as client:
        pending = client.post("/v2/cases", json=START, headers=AUTH)
        assert pending.status_code == 503
        error = pending.json()
        assert error["code"] == "operation_pending"
        assert error["retryable"] is False
        assert error["details"] == {"next_action": "list_cases"}
        assert "retry-after" not in pending.headers
        retriever.gate.set()
        wait_until(lambda: writes.outstanding == 0)
        cases = client.get("/v2/cases", headers=AUTH).json()["cases"]
        assert len(cases) == 1
        assert cases[0]["status"] == "awaiting_review"
        assert retriever.calls == 1


# -- restart --------------------------------------------------------------------------------


def test_restart_with_new_app_keeps_cases(tmp_path):
    workspace = tmp_path / "ws"
    ReviewService.initialize(workspace, CONFIG_PATH)
    settings = ApiSettings(workspace=workspace, config_path=CONFIG_PATH, api_keys=KEYS)
    with TestClient(create_app(settings)) as first:
        case = first.post("/v2/cases", json=START, headers=AUTH).json()
        case_id = case["case"]["case_id"]
        other = first.post(
            "/v2/cases", json={**START, "name": "Mira Velune"}, headers=AUTH
        )
        other_id = other.json()["case"]["case_id"]
    with TestClient(create_app(settings)) as second:
        again = second.get(f"/v2/cases/{case_id}", headers=AUTH).json()
        assert again == case
        listed = [
            c["case_id"] for c in second.get("/v2/cases", headers=AUTH).json()["cases"]
        ]
        assert listed == [case_id, other_id]
        top = case["case"]["packet"]["retrieval"]["candidates"][0]["entity_id"]
        done = second.post(
            f"/v2/cases/{case_id}/review",
            json=form(case, candidate_entity_id=top),
            headers=AUTH_2,
        )
        assert done.status_code == 200 and done.json()["case"]["status"] == "completed"
    with TestClient(create_app(settings)) as third:
        assert (
            third.get(f"/v2/cases/{case_id}", headers=AUTH).json()["case"]["status"]
            == "completed"
        )
        assert third.get(f"/v2/cases/{case_id}/audit/verify", headers=AUTH).json()[
            "verification"
        ]["ok"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(workspace, port):
    env = worker_env()
    env[KEYS_VAR] = f"fictional-reviewer:reviewer:{hash_key(REVIEWER)}"
    env.pop(KEYS_FILE_VAR, None)
    return subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "watchlist.api",
            "serve",
            "--workspace",
            str(workspace),
            "--config",
            str(CONFIG_PATH),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _wait_healthy(base, process):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(process.stderr.read().decode("utf-8", "replace"))
        try:
            if httpx.get(f"{base}/health", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise AssertionError("server did not become healthy")


def _stop(process):
    process.terminate()
    try:
        process.wait(15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(15)
    process.stderr.close()


def test_uvicorn_server_restart_keeps_cases(tmp_path):
    """Real loopback server process, killed and restarted on the same workspace."""
    workspace = tmp_path / "ws"
    ReviewService.initialize(workspace, CONFIG_PATH)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    process = _serve(workspace, port)
    try:
        _wait_healthy(base, process)
        health = httpx.get(f"{base}/health", timeout=5)
        assert "server" not in health.headers
        assert health.headers["x-content-type-options"] == "nosniff"
        created = httpx.post(f"{base}/v2/cases", json=START, headers=AUTH, timeout=30)
        assert created.status_code == 200, created.text
        case_id = created.json()["case"]["case_id"]
        assert httpx.get(f"{base}/v2/cases", timeout=5).status_code == 401
    finally:
        _stop(process)
    process = _serve(workspace, port)
    try:
        _wait_healthy(base, process)
        again = httpx.get(f"{base}/v2/cases/{case_id}", headers=AUTH, timeout=30)
        assert again.status_code == 200
        assert again.json()["case"]["status"] == "awaiting_review"
    finally:
        _stop(process)
