"""Contract tests for the v2 review API over a temporary fictional workspace."""

from __future__ import annotations

import json
from pathlib import Path
import secrets
import threading
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from review_v2_support import ROOT, FakeRetriever, make_config
from watchlist.api import ApiSettings, case_revision, create_app, openapi_document
from watchlist.api.auth import ApiKey, hash_key
from watchlist.api.app import WriteQueue
from watchlist.api.errors import REVIEW_STATUS, SECURITY_HEADERS, error_body
from watchlist.api.schemas import CONTRACT_MODELS
from watchlist.review import contracts
from watchlist.review.contracts import LIMITATION, CaseView, ReviewError
from watchlist.review.locking import workspace_lock
from watchlist.review.nodes import RapidFuzzRetriever
from watchlist.review.service import ReviewService

REVIEWER = secrets.token_urlsafe(24)
VIEWER = secrets.token_urlsafe(24)
KEYS = (
    ApiKey("fictional-reviewer", "reviewer", hash_key(REVIEWER)),
    ApiKey("fictional-viewer", "viewer", hash_key(VIEWER)),
)
AUTH = {"Authorization": f"Bearer {REVIEWER}"}
VIEW_AUTH = {"Authorization": f"Bearer {VIEWER}"}


def build(tmp_path: Path, retriever=None, *, config=None, **settings):
    service = ReviewService.initialize(
        tmp_path / "ws",
        config or make_config(),
        retriever=retriever or FakeRetriever(),
    )
    app = create_app(ApiSettings(api_keys=KEYS, **settings), service=service)
    return app, service


@pytest.fixture
def client(tmp_path):
    app, _ = build(tmp_path)
    with TestClient(app) as test_client:
        yield test_client


def start(client, name="Luma Vesper", **extra):
    body = {"name": name, "origin": "test", **extra}
    return client.post("/v2/cases", json=body, headers=AUTH)


def form(case: dict, **changes):
    body = {
        "submission_id": str(uuid4()),
        "revision": case["revision"],
        "packet_sha256": case["case"]["packet_sha256"],
        "outcome": "candidate_for_follow_up",
        "candidate_entity_id": "syn-001",
        "reason": "Fictional test; follow up on the possible record match.",
    }
    body.update(changes)
    return body


def assert_error(response, status: int, code: str):
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"code", "message", "retryable", "details"}
    assert body["code"] == code
    return body


# -- health, headers, docs ---------------------------------------------------------


def test_health_needs_no_key_and_carries_notice(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["research_only"] is True
    assert body["limitation"] == LIMITATION
    for name, value in SECURITY_HEADERS:
        assert response.headers[name] == value


def test_no_cors_and_no_interactive_docs(client):
    response = client.options(
        "/v2/cases",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in response.headers
    response = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in response.headers
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert_error(client.get(path), 404, "not_found")


def test_unknown_route_and_method_are_structured(client):
    assert_error(client.get("/nope"), 404, "not_found")
    assert_error(client.delete("/v2/cases", headers=AUTH), 405, "method_not_allowed")


# -- start case ----------------------------------------------------------------------


def test_start_case_returns_candidates_with_notice_and_key_actor(client):
    response = start(client)
    assert response.status_code == 200, response.text
    body = response.json()
    case = body["case"]
    assert body["limitation"] == LIMITATION == case["limitation"]
    assert case["packet"]["limitation"] == LIMITATION
    assert case["status"] == "awaiting_review"
    assert case["next_action"] == "submit_review"
    assert case["header"]["actor"] == "fictional-reviewer"
    assert [c["entity_id"] for c in case["packet"]["retrieval"]["candidates"]] == [
        "syn-001",
        "syn-012",
    ]
    assert body["revision"] == case_revision(CaseView.from_dict(case))
    assert response.headers["x-research-notice"].startswith("research-only")


def test_actor_in_body_is_rejected(client):
    response = client.post(
        "/v2/cases",
        json={"name": "Luma Vesper", "origin": "test", "actor": "someone-else"},
        headers=AUTH,
    )
    body = assert_error(response, 422, "invalid_input")
    assert "someone-else" not in response.text
    assert body["details"]["errors"][0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "x" * 201, "origin": "test"},
        {"name": "", "origin": "test"},
        {"name": "Luma Vesper", "origin": "robot"},
        {"name": "Luma Vesper"},
        {"name": 7, "origin": "test"},
        {"name": "Luma Vesper", "origin": "test", "request_id": "not-a-uuid"},
        {
            "name": "Luma Vesper",
            "origin": "test",
            "request_id": str(uuid4()).upper(),
        },
        {"name": "Luma\u202eVesper", "origin": "test"},
        {"name": "   ", "origin": "test"},
    ],
)
def test_start_case_invalid_input_is_422(client, payload):
    response = client.post("/v2/cases", json=payload, headers=AUTH)
    assert_error(response, 422, "invalid_input")
    assert client.get("/v2/cases", headers=AUTH).json()["cases"] == []


def test_name_limit_counts_code_points(client):
    name = "Мира " * 40  # 200 code points, 360 UTF-8 bytes
    assert len(name) == 200
    assert start(client, name=name).status_code == 200


def test_malformed_duplicate_and_nan_json_are_422(client):
    for raw in (
        b"{not json",
        b'{"name": "Luma Vesper", "name": "Other", "origin": "test"}',
        b'{"name": "Luma Vesper", "origin": "test", "request_id": NaN}',
    ):
        response = client.post(
            "/v2/cases",
            content=raw,
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert_error(response, 422, "invalid_input")
    response = client.post("/v2/cases", content=b'{"name":"a"}', headers=AUTH)
    assert response.status_code == 422
    assert client.get("/v2/cases", headers=AUTH).json()["cases"] == []


@pytest.mark.parametrize(
    "raw",
    [
        b'{"name":"\xff\xfe","origin":"test"}',  # invalid UTF-8
        b"[" * 30000 + b"]" * 30000,  # nesting too deep for the decoder, < 64 KiB
    ],
    ids=["invalid-utf8", "deep-nesting"],
)
def test_unparseable_bodies_are_422_not_400(client, raw):
    assert len(raw) < 64 * 1024
    for path in ("/v2/cases", f"/v2/cases/{uuid4()}/cancel"):
        response = client.post(
            path, content=raw, headers={**AUTH, "Content-Type": "application/json"}
        )
        assert_error(response, 422, "invalid_input")
        assert "[[[" not in response.text  # input is never echoed back
    assert client.get("/v2/cases", headers=AUTH).json()["cases"] == []


def test_body_over_64_kib_is_413(client):
    big = {"name": "Luma Vesper", "origin": "test", "pad": "x" * (64 * 1024)}
    assert_error(
        client.post("/v2/cases", json=big, headers=AUTH), 413, "body_too_large"
    )

    def chunks():
        for _ in range(70):
            yield b"x" * 1024

    response = client.post(
        "/v2/cases",
        content=chunks(),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert_error(response, 413, "body_too_large")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_request_id_makes_start_idempotent_and_conflicts(client):
    request_id = str(uuid4())
    first = start(client, request_id=request_id).json()
    again = start(client, request_id=request_id).json()
    assert again["case"]["case_id"] == first["case"]["case_id"]
    assert len(client.get("/v2/cases", headers=AUTH).json()["cases"]) == 1
    conflict = start(client, name="Mira Velune", request_id=request_id)
    assert_error(conflict, 409, "request_conflict")


# -- read endpoints ------------------------------------------------------------------


def test_get_and_list_with_pagination_and_origin(client):
    ids = [
        start(client, name=f"Luma Vesper {i}").json()["case"]["case_id"]
        for i in range(3)
    ]
    demo = client.post(
        "/v2/cases",
        json={"name": "Mira Velune", "origin": "simulated_demo"},
        headers=AUTH,
    ).json()["case"]["case_id"]
    got = client.get(f"/v2/cases/{ids[0]}", headers=VIEW_AUTH).json()
    assert got["case"]["case_id"] == ids[0] and got["limitation"] == LIMITATION
    page = client.get("/v2/cases?limit=2", headers=VIEW_AUTH).json()
    assert [c["case_id"] for c in page["cases"]] == ids[:2]
    assert page["next_offset"] == 2 and page["limitation"] == LIMITATION
    assert "name" not in json.dumps(page["cases"])
    page2 = client.get("/v2/cases?limit=2&offset=2", headers=VIEW_AUTH).json()
    assert [c["case_id"] for c in page2["cases"]] == [ids[2], demo]
    page3 = client.get("/v2/cases?limit=2&offset=4", headers=VIEW_AUTH).json()
    assert page3["cases"] == [] and page3["next_offset"] is None
    only_demo = client.get("/v2/cases?origin=simulated_demo", headers=VIEW_AUTH).json()
    assert [c["case_id"] for c in only_demo["cases"]] == [demo]
    for query in ("limit=51", "limit=0", "offset=-1", "origin=robot", "limit=x"):
        assert_error(
            client.get(f"/v2/cases?{query}", headers=VIEW_AUTH), 422, "invalid_input"
        )


def test_unknown_and_malformed_case_ids(client):
    missing = str(uuid4())
    for suffix in ("", "/audit", "/audit/verify"):
        assert_error(
            client.get(f"/v2/cases/{missing}{suffix}", headers=AUTH),
            404,
            "case_not_found",
        )
        assert_error(
            client.get(f"/v2/cases/not-a-uuid{suffix}", headers=AUTH),
            422,
            "invalid_input",
        )
    assert_error(
        client.post(f"/v2/cases/{missing}/recover", headers=AUTH), 404, "case_not_found"
    )
    assert_error(
        client.post(f"/v2/cases/{missing}/cancel", json={"reason": "x"}, headers=AUTH),
        404,
        "case_not_found",
    )
    body = {
        "submission_id": str(uuid4()),
        "revision": "0" * 64,
        "packet_sha256": "0" * 64,
        "outcome": "unresolved",
        "candidate_entity_id": None,
        "reason": "Fictional.",
    }
    assert_error(
        client.post(f"/v2/cases/{missing}/review", json=body, headers=AUTH),
        404,
        "case_not_found",
    )
    assert_error(
        client.post("/v2/cases/NOT-A-UUID/review", json=body, headers=AUTH),
        422,
        "invalid_input",
    )


# -- review --------------------------------------------------------------------------


def test_review_completes_replays_and_rejects_later_forms(client):
    case = start(client).json()
    case_id = case["case"]["case_id"]
    body = form(case)
    done = client.post(f"/v2/cases/{case_id}/review", json=body, headers=AUTH)
    assert done.status_code == 200, done.text
    view = done.json()["case"]
    assert view["status"] == "completed" and view["next_action"] == "none"
    assert view["review"]["submission"]["actor"] == "fictional-reviewer"
    assert view["review"]["submission"]["origin"] == "test"
    assert view["explanation"]["limitation"] == LIMITATION
    replay = client.post(f"/v2/cases/{case_id}/review", json=body, headers=AUTH)
    assert replay.status_code == 200 and replay.json()["case"] == view
    changed = client.post(
        f"/v2/cases/{case_id}/review",
        json={**body, "reason": "Different."},
        headers=AUTH,
    )
    assert_error(changed, 409, "submission_conflict")
    other = client.post(f"/v2/cases/{case_id}/review", json=form(case), headers=AUTH)
    assert_error(other, 409, "stale_submission")
    events = client.get(f"/v2/cases/{case_id}/audit", headers=VIEW_AUTH).json()
    types = [e["event_type"] for e in events["audit"]["events"]]
    assert types.count("review_submitted") == 1 and types[-1] == "case_finalized"


def test_review_with_old_revision_or_packet_is_409(client, tmp_path):
    case = start(client).json()
    case_id = case["case"]["case_id"]
    assert_error(
        client.post(
            f"/v2/cases/{case_id}/review",
            json=form(case, revision="1" * 64),
            headers=AUTH,
        ),
        409,
        "stale_submission",
    )
    body = assert_error(
        client.post(
            f"/v2/cases/{case_id}/review",
            json=form(case, packet_sha256="2" * 64),
            headers=AUTH,
        ),
        409,
        "stale_submission",
    )
    assert body["details"]["expected_packet_sha256"] == case["case"]["packet_sha256"]
    assert (
        client.get(f"/v2/cases/{case_id}", headers=AUTH).json()["case"]["review"]
        is None
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_entity_id": "syn-999"},
        {"outcome": "none_selected"},
        {"outcome": "unresolved", "candidate_entity_id": "syn-001"},
        {"outcome": "cleared", "candidate_entity_id": None},
        {"outcome": "candidate_for_follow_up", "candidate_entity_id": None},
        {"reason": "y" * 1001},
        {"reason": "bad\x00reason"},
        {"submission_id": "not-a-uuid"},
        {"actor": "someone-else"},
        {"origin": "human"},
    ],
)
def test_review_invalid_forms_are_422(client, changes):
    case = start(client).json()
    case_id = case["case"]["case_id"]
    response = client.post(
        f"/v2/cases/{case_id}/review", json=form(case, **changes), headers=AUTH
    )
    assert_error(response, 422, "invalid_input")
    assert (
        client.get(f"/v2/cases/{case_id}", headers=AUTH).json()["case"]["status"]
        == "awaiting_review"
    )


def test_none_selected_and_unresolved_are_plain_outcomes(client):
    for outcome in ("none_selected", "unresolved"):
        case = start(client).json()
        response = client.post(
            f"/v2/cases/{case['case']['case_id']}/review",
            json=form(case, outcome=outcome, candidate_entity_id=None),
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.json()["case"]["review"]["submission"]["outcome"] == outcome


# -- recover, cancel, audit -----------------------------------------------------------


def test_recover_cancel_and_review_after_cancel(client):
    case = start(client).json()
    case_id = case["case"]["case_id"]
    recovered = client.post(f"/v2/cases/{case_id}/recover", headers=AUTH)
    assert recovered.status_code == 200
    assert recovered.json()["case"]["status"] == "awaiting_review"
    cancelled = client.post(
        f"/v2/cases/{case_id}/cancel",
        json={"reason": "Fictional duplicate request."},
        headers=AUTH,
    )
    assert cancelled.status_code == 200
    view = cancelled.json()["case"]
    assert view["status"] == "cancelled"
    assert view["cancellation"]["actor"] == "fictional-reviewer"
    again = client.post(
        f"/v2/cases/{case_id}/cancel",
        json={"reason": "Fictional duplicate request."},
        headers=AUTH,
    )
    assert again.status_code == 200
    assert_error(
        client.post(
            f"/v2/cases/{case_id}/cancel", json={"reason": "Other."}, headers=AUTH
        ),
        409,
        "invalid_state",
    )
    review = client.post(
        f"/v2/cases/{case_id}/review",
        json=form(cancelled.json()),
        headers=AUTH,
    )
    assert_error(review, 409, "invalid_state")
    assert_error(
        client.post(
            f"/v2/cases/{case_id}/cancel",
            json={"reason": "x", "actor": "a"},
            headers=AUTH,
        ),
        422,
        "invalid_input",
    )
    assert_error(
        client.post(
            f"/v2/cases/{case_id}/cancel", json={"reason": "r" * 1001}, headers=AUTH
        ),
        422,
        "invalid_input",
    )


def test_cancel_after_review_is_409(client):
    case = start(client).json()
    case_id = case["case"]["case_id"]
    client.post(f"/v2/cases/{case_id}/review", json=form(case), headers=AUTH)
    assert_error(
        client.post(
            f"/v2/cases/{case_id}/cancel", json={"reason": "Late."}, headers=AUTH
        ),
        409,
        "invalid_state",
    )


def test_audit_and_verification_with_anchor(client):
    case = start(client).json()
    case_id = case["case"]["case_id"]
    client.post(f"/v2/cases/{case_id}/review", json=form(case), headers=AUTH)
    audit = client.get(f"/v2/cases/{case_id}/audit", headers=VIEW_AUTH).json()
    assert audit["limitation"] == LIMITATION
    events = audit["audit"]["events"]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    ok = client.get(f"/v2/cases/{case_id}/audit/verify", headers=VIEW_AUTH).json()
    assert (
        ok["verification"]["ok"] is True
        and ok["verification"]["anchor_checked"] is False
    )
    anchor = f"anchor_count=2&anchor_head_sha256={events[1]['event_sha256']}"
    anchored = client.get(
        f"/v2/cases/{case_id}/audit/verify?{anchor}", headers=VIEW_AUTH
    ).json()
    assert anchored["verification"]["ok"] is True
    assert anchored["verification"]["anchor_checked"] is True
    wrong = f"anchor_count=2&anchor_head_sha256={'a' * 64}"
    bad = client.get(
        f"/v2/cases/{case_id}/audit/verify?{wrong}", headers=VIEW_AUTH
    ).json()
    assert bad["verification"]["ok"] is False
    too_long = f"anchor_count=99&anchor_head_sha256={'a' * 64}"
    short = client.get(
        f"/v2/cases/{case_id}/audit/verify?{too_long}", headers=VIEW_AUTH
    ).json()
    assert short["verification"]["ok"] is False
    assert_error(
        client.get(
            f"/v2/cases/{case_id}/audit/verify?anchor_count=2", headers=VIEW_AUTH
        ),
        422,
        "invalid_input",
    )
    assert_error(
        client.get(
            f"/v2/cases/{case_id}/audit/verify?anchor_count=2&anchor_head_sha256=XYZ",
            headers=VIEW_AUTH,
        ),
        422,
        "invalid_input",
    )


# -- reference records ---------------------------------------------------------------


def test_entity_record_with_aliases_and_notice(client):
    response = client.get("/v2/entities/syn-001", headers=VIEW_AUTH)
    assert response.status_code == 200
    body = response.json()
    entity = body["entity"]
    assert body["limitation"] == LIMITATION
    assert entity["synthetic"] is True and entity["source"] == "fictional-synthetic-v1"
    assert entity["provenance"] is None
    assert "fictional" in entity["attribution"].lower()
    assert [a["alias_id"] for a in entity["aliases"]] == ["syn-001-a1", "syn-001-a2"]
    assert entity["aliases"][0]["evidence_id"] == "syn-001/syn-001-a1"
    assert entity["dataset"] == "packaged-fictional"
    assert len(entity["reference_sha256"]) == 64
    assert_error(
        client.get("/v2/entities/syn-404", headers=VIEW_AUTH), 404, "entity_not_found"
    )
    assert_error(
        client.get("/v2/entities/..%5Csecret", headers=VIEW_AUTH), 422, "invalid_input"
    )
    assert_error(
        client.get(f"/v2/entities/{'a' * 129}", headers=VIEW_AUTH), 422, "invalid_input"
    )


def test_entity_matches_review_evidence(tmp_path):
    app, _ = build(tmp_path, RapidFuzzRetriever())
    with TestClient(app) as client:
        case = start(client).json()["case"]
        evidence = case["packet"]["retrieval"]["evidence"]
        top = case["packet"]["retrieval"]["candidates"][0]["entity_id"]
        entity = client.get(f"/v2/entities/{top}", headers=AUTH).json()["entity"]
        expected = [e["evidence_id"] for e in evidence if e["entity_id"] == top]
        assert [a["evidence_id"] for a in entity["aliases"]] == expected
        assert (
            entity["reference_sha256"]
            == case["header"]["retriever"]["reference_sha256"]
        )


# -- service errors through HTTP -----------------------------------------------------------


def test_retrieval_failure_is_503_and_recoverable(tmp_path):
    retriever = FakeRetriever(fail=True)
    app, _ = build(tmp_path, retriever)
    with TestClient(app) as client:
        response = start(client)
        body = assert_error(response, 503, "retrieval_failed")
        assert body["retryable"] is False
        assert body["details"]["next_action"] == "recover"
        assert "must not run" not in response.text  # exception text is not returned
        case_id = body["details"]["case_id"]
        assert (
            client.get(f"/v2/cases/{case_id}", headers=AUTH).json()["case"]["status"]
            == "preparing"
        )
        retriever.fail = False
        recovered = client.post(f"/v2/cases/{case_id}/recover", headers=AUTH)
        assert recovered.json()["case"]["status"] == "awaiting_review"
        retriever.fail = True
        keyed = start(client, request_id=str(uuid4()))
        body = assert_error(keyed, 503, "retrieval_failed")
        assert body["retryable"] is True and keyed.headers["retry-after"] == "1"


def test_workspace_busy_is_503_retryable(tmp_path):
    app, service = build(tmp_path, config=make_config(lock_timeout=0.0))
    with TestClient(app) as client:
        case_id = start(client).json()["case"]["case_id"]
        with workspace_lock(service.workspace, timeout=1):
            response = client.get(f"/v2/cases/{case_id}", headers=AUTH)
            body = assert_error(response, 503, "workspace_busy")
            assert body["retryable"] is True and response.headers["retry-after"] == "1"
            body = assert_error(start(client), 503, "workspace_busy")
        assert client.get(f"/v2/cases/{case_id}", headers=AUTH).status_code == 200


def test_incompatible_case_is_409_and_cancellable(tmp_path):
    service = ReviewService.initialize(
        tmp_path / "ws", make_config(), retriever=FakeRetriever()
    )
    case_id = service.start_case(
        "Luma Vesper", origin="test", actor="fictional"
    ).case_id
    changed = ReviewService.open(
        tmp_path / "ws", make_config(top_k=4), retriever=FakeRetriever()
    )
    app = create_app(ApiSettings(api_keys=KEYS), service=changed)
    with TestClient(app) as client:
        case = client.get(f"/v2/cases/{case_id}", headers=AUTH).json()
        assert case["case"]["status"] == "incompatible"
        response = client.post(
            f"/v2/cases/{case_id}/review", json=form(case), headers=AUTH
        )
        assert_error(response, 409, "incompatible_version")
        cancel = client.post(
            f"/v2/cases/{case_id}/cancel", json={"reason": "Old version."}, headers=AUTH
        )
        assert cancel.json()["case"]["status"] == "cancelled"


class _Exploding:
    """Wraps a service; selected methods raise to exercise error paths."""

    def __init__(self, inner, error):
        self._inner, self._error = inner, error

    def __getattr__(self, name):
        if name in ("get_case", "list_cases"):

            def boom(*args, **kwargs):
                raise self._error

            return boom
        return getattr(self._inner, name)


def test_unexpected_exception_is_generic_500(tmp_path):
    _, service = build(tmp_path)
    hidden_path = "C:/private/fictional-hidden-path"
    app = create_app(
        ApiSettings(api_keys=KEYS),
        service=_Exploding(service, RuntimeError(hidden_path)),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(f"/v2/cases/{uuid4()}", headers=AUTH)
        assert_error(response, 500, "internal_error")
        assert hidden_path not in response.text and "Traceback" not in response.text
        assert response.headers["x-content-type-options"] == "nosniff"


def test_server_error_text_and_unsafe_details_are_withheld(tmp_path):
    _, service = build(tmp_path)
    error = contracts.StorageIntegrityError(
        "row in C:\\private\\db is bad",
        details={"case_id": "c", "files": ["C:\\private\\db-journal"], "cause": "x"},
    )
    app = create_app(ApiSettings(api_keys=KEYS), service=_Exploding(service, error))
    with TestClient(app) as client:
        response = client.get("/v2/cases", headers=AUTH)
        body = assert_error(response, 500, "storage_inconsistent")
        assert "private" not in response.text
        assert body["details"] == {"case_id": "c", "cause": "x"}


def test_every_review_error_code_has_a_status():
    codes = {
        cls.code
        for cls in vars(contracts).values()
        if isinstance(cls, type)
        and issubclass(cls, ReviewError)
        and cls is not ReviewError
    }
    assert codes | {"entity_not_found", "reference_mismatch"} == set(REVIEW_STATUS)
    assert REVIEW_STATUS["invalid_input"] == 422
    assert REVIEW_STATUS["case_not_found"] == 404
    assert {
        REVIEW_STATUS[c]
        for c in ("stale_submission", "submission_conflict", "request_conflict")
    } == {409}
    assert REVIEW_STATUS["workspace_busy"] == 503
    assert error_body("c", "m", True, {"files": ["x"]})["details"] == {}


def test_read_only_service_refuses_writes(tmp_path):
    _, service = build(tmp_path)
    reader = ReviewService.open(service.workspace, make_config(), read_only=True)
    app = create_app(ApiSettings(api_keys=KEYS), service=reader)
    with TestClient(app) as client:
        assert_error(start(client), 403, "read_only")
        assert client.get("/v2/cases", headers=AUTH).status_code == 200


# -- schema contracts ------------------------------------------------------------------


def test_response_models_mirror_contract_fields():
    for model, name in CONTRACT_MODELS:
        contract = getattr(contracts, name)
        assert list(model.model_fields) == list(contract.__dataclass_fields__), name


def test_committed_openapi_matches_generated():
    committed = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(openapi_document()))
    paths = committed["paths"]
    assert set(paths) == {
        "/health",
        "/v2/cases",
        "/v2/cases/{case_id}",
        "/v2/cases/{case_id}/review",
        "/v2/cases/{case_id}/recover",
        "/v2/cases/{case_id}/cancel",
        "/v2/cases/{case_id}/audit",
        "/v2/cases/{case_id}/audit/verify",
        "/v2/entities/{entity_id}",
    }
    start_schema = committed["components"]["schemas"]["StartCaseRequest"]
    assert "actor" not in start_schema["properties"]
    review_schema = committed["components"]["schemas"]["ReviewRequest"]
    assert "actor" not in review_schema["properties"]


def test_create_app_argument_rules(tmp_path):
    _, service = build(tmp_path)
    with pytest.raises(ValueError):
        create_app(ApiSettings(api_keys=KEYS))
    with pytest.raises(ValueError):
        create_app(
            ApiSettings(api_keys=KEYS), service=service, context_providers=[object()]
        )
    with pytest.raises(TypeError):
        create_app({"api_keys": KEYS}, service=service)


def test_create_app_opens_existing_workspace_only(tmp_path):
    settings = ApiSettings(
        workspace=tmp_path / "missing",
        config_path=ROOT / "config" / "review-v2.json",
        api_keys=KEYS,
    )
    with pytest.raises(ReviewError) as caught:
        create_app(settings)
    assert caught.value.code == "workspace_invalid"
    assert not (tmp_path / "missing").exists()


def test_write_queue_counts_release_after_errors(tmp_path):
    from watchlist.api.app import WriteQueue

    queue = WriteQueue(0)
    gate = threading.Event()
    future = queue.submit(gate.wait)
    from watchlist.api.errors import ApiError

    with pytest.raises(ApiError) as caught:
        queue.submit(lambda: None)
    assert caught.value.status == 429
    gate.set()
    future.result(timeout=5)
    failing = queue.submit(lambda: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        failing.result(timeout=5)
    queue.shutdown()
    assert queue.outstanding == 0


@pytest.mark.parametrize("fail", [False, True])
def test_write_queue_releases_before_future_publishes_result(monkeypatch, fail):
    from concurrent.futures import Future

    queue = WriteQueue(0)
    published = []
    name = "set_exception" if fail else "set_result"
    original = getattr(Future, name)

    def publish(future, value):
        published.append(queue.outstanding)
        original(future, value)

    monkeypatch.setattr(Future, name, publish)

    def work():
        if fail:
            raise ValueError("fixture failure")
        return 7

    try:
        result = queue.submit(work)
        if fail:
            with pytest.raises(ValueError):
                result.result(30)
        else:
            assert result.result(30) == 7
        assert published == [0]
    finally:
        queue.shutdown()


def test_cancelled_queued_write_releases_admission_slot():
    import threading

    queue = WriteQueue(1)
    gate = threading.Event()
    first = queue.submit(lambda: gate.wait(30))
    try:
        cancelled = queue.submit(lambda: None)
        assert cancelled.cancel()
        assert queue.outstanding == 1
        next_write = queue.submit(lambda: 9)
        gate.set()
        first.result(30)
        assert next_write.result(30) == 9
        assert queue.outstanding == 0
    finally:
        gate.set()
        queue.shutdown()
