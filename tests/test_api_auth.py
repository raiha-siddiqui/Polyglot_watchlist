"""API-key parsing, constant-time checks, settings and the endpoint auth matrix."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import subprocess
import sys
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from review_v2_support import (
    CONFIG_PATH,
    ROOT,
    FakeRetriever,
    make_config,
    worker_env,
)
from watchlist.api import ApiSettings, SettingsError, create_app
from watchlist.api import auth
from watchlist.api.settings import ENV_KEYS as KEYS_VAR, ENV_KEYS_FILE as KEYS_FILE_VAR
from watchlist.api.__main__ import is_loopback, main
from watchlist.api.auth import (
    ApiKey,
    AuthConfigError,
    KeyStore,
    hash_key,
    new_key,
    parse_key_entries,
    parse_key_entry,
)
from watchlist.review.service import ReviewService

REVIEWER = secrets.token_urlsafe(24)
VIEWER = secrets.token_urlsafe(24)
OTHER_REVIEWER = secrets.token_urlsafe(24)
KEYS = (
    ApiKey("fictional-reviewer", "reviewer", hash_key(REVIEWER)),
    ApiKey("fictional-viewer", "viewer", hash_key(VIEWER)),
    ApiKey("fictional-reviewer-2", "reviewer", hash_key(OTHER_REVIEWER)),
)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client(tmp_path):
    service = ReviewService.initialize(
        tmp_path / "ws", make_config(), retriever=FakeRetriever()
    )
    with TestClient(create_app(ApiSettings(api_keys=KEYS), service=service)) as c:
        yield c


# -- key entries ------------------------------------------------------------------


def test_parse_valid_entries_and_comments():
    digest = hash_key("k" * 32)
    keys = parse_key_entries(
        [
            "# comment",
            "",
            f"analyst-1:reviewer:{digest}",
            f"  viewer.a@lab:viewer:{'1' * 64}  ",
        ]
    )
    assert [(k.actor, k.role) for k in keys] == [
        ("analyst-1", "reviewer"),
        ("viewer.a@lab", "viewer"),
    ]
    assert digest not in repr(keys)


@pytest.mark.parametrize(
    "entry",
    [
        "analyst:reviewer",
        f"analyst:admin:{'a' * 64}",
        f"analyst:reviewer:{'A' * 64}",
        f"analyst:reviewer:{'a' * 63}",
        f":reviewer:{'a' * 64}",
        f"bad actor:reviewer:{'a' * 64}",
        f"-lead:reviewer:{'a' * 64}",
        f"a:b:c:{'a' * 64}",
        f"{'a' * 65}:reviewer:{'a' * 64}",
    ],
)
def test_malformed_entries_are_refused_without_echoing_hash(entry):
    with pytest.raises(AuthConfigError) as caught:
        parse_key_entry(entry)
    assert "a" * 64 not in str(caught.value)


def test_duplicate_hash_is_refused():
    digest = "b" * 64
    with pytest.raises(AuthConfigError):
        parse_key_entries([f"a:viewer:{digest}", f"b:reviewer:{digest}"])
    with pytest.raises(SettingsError):
        ApiSettings(
            api_keys=(ApiKey("a", "viewer", digest), ApiKey("b", "viewer", digest))
        )


def test_new_key_round_trip():
    token, entry = new_key("analyst-1", "viewer")
    key = parse_key_entry(entry)
    assert key.sha256 == hash_key(token) and token not in entry
    principal = KeyStore([key]).authenticate(token)
    assert principal is not None and principal.actor == "analyst-1"
    with pytest.raises(AuthConfigError):
        new_key("bad actor", "viewer")


def test_authenticate_compares_every_hash(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", counting)
    store = KeyStore(KEYS)
    assert store.authenticate(REVIEWER).actor == "fictional-reviewer"
    assert len(calls) == len(KEYS)
    calls.clear()
    assert store.authenticate("x" * 40) is None
    assert len(calls) == len(KEYS)
    calls.clear()
    for bad in (None, "", "short", "x" * 513, "é" * 20):
        assert store.authenticate(bad) is None
    assert calls == []


def test_roles():
    store = KeyStore(KEYS)
    viewer = store.authenticate(VIEWER)
    reviewer = store.authenticate(REVIEWER)
    assert viewer.can("viewer") and not viewer.can("reviewer")
    assert reviewer.can("viewer") and reviewer.can("reviewer")


# -- settings from the environment ----------------------------------------------------


def test_settings_from_env_inline_and_file(tmp_path):
    entry_a = f"a:viewer:{hash_key('a' * 20)}"
    entry_b = f"b:reviewer:{hash_key('b' * 20)}"
    settings = ApiSettings.from_env(
        workspace=tmp_path,
        config_path=None,
        environ={
            KEYS_VAR: f"{entry_a}, {entry_b}",
            "WATCHLIST_API_WRITE_QUEUE_MAX": "3",
            "WATCHLIST_API_WRITE_WAIT_SECONDS": "2.5",
        },
    )
    assert [k.actor for k in settings.api_keys] == ["a", "b"]
    assert settings.write_queue_max == 3 and settings.write_wait_seconds == 2.5
    assert settings.max_body_bytes == 64 * 1024
    keys_file = tmp_path / "keys.txt"
    keys_file.write_text(
        f"# fictional test keys\n{entry_a}\n\n{entry_b}\n", encoding="utf-8"
    )
    settings = ApiSettings.from_env(
        workspace=None,
        config_path=None,
        environ={KEYS_FILE_VAR: str(keys_file)},
    )
    assert len(settings.api_keys) == 2
    defaults = ApiSettings.from_env(
        workspace=None, config_path=None, environ={}, require_keys=False
    )
    assert defaults.api_keys == () and defaults.write_queue_max == 8


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {KEYS_VAR: "nonsense"},
        {
            KEYS_VAR: f"a:viewer:{'a' * 64}",
            KEYS_FILE_VAR: "x",
        },
        {KEYS_FILE_VAR: "definitely-missing-keys-file.txt"},
        {
            KEYS_VAR: f"a:viewer:{'a' * 64}",
            "WATCHLIST_API_WRITE_QUEUE_MAX": "-1",
        },
        {
            KEYS_VAR: f"a:viewer:{'a' * 64}",
            "WATCHLIST_API_WRITE_QUEUE_MAX": "1001",
        },
        {
            KEYS_VAR: f"a:viewer:{'a' * 64}",
            "WATCHLIST_API_WRITE_WAIT_SECONDS": "nan",
        },
        {
            KEYS_VAR: f"a:viewer:{'a' * 64}",
            "WATCHLIST_API_WRITE_WAIT_SECONDS": "0",
        },
    ],
)
def test_bad_settings_are_refused(environ, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SettingsError) as caught:
        ApiSettings.from_env(workspace=None, config_path=None, environ=environ)
    assert "a" * 64 not in str(caught.value)


def test_settings_repr_hides_hashes():
    assert hash_key(REVIEWER) not in repr(ApiSettings(api_keys=KEYS))


# -- endpoint matrix ---------------------------------------------------------------------

READS = [
    ("get", "/v2/cases", None),
    ("get", "/v2/cases/{case}", None),
    ("get", "/v2/cases/{case}/audit", None),
    ("get", "/v2/cases/{case}/audit/verify", None),
    ("get", "/v2/entities/syn-001", None),
]
WRITES = [
    ("post", "/v2/cases", {"name": "Luma Vesper", "origin": "test"}),
    ("post", "/v2/cases/{case}/recover", None),
    ("post", "/v2/cases/{case}/cancel", {"reason": "Fictional matrix check."}),
    ("post", "/v2/cases/{case}/review", "form"),
]
BAD_HEADERS = [
    {},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer " + "x" * 40},
    {"Authorization": "Basic " + "x" * 40},
    {"Authorization": "Token " + REVIEWER},
    {"Authorization": "Bearer " + REVIEWER + "x"},
    {"Authorization": "Bearer " + REVIEWER[:-1]},
    {"X-Api-Key": REVIEWER},
]


def _case(client):
    return client.post(
        "/v2/cases",
        json={"name": "Luma Vesper", "origin": "test"},
        headers=bearer(REVIEWER),
    ).json()


def _call(client, method, path, body, case, headers):
    url = path.replace("{case}", case["case"]["case_id"])
    if body == "form":
        body = {
            "submission_id": str(uuid4()),
            "revision": case["revision"],
            "packet_sha256": case["case"]["packet_sha256"],
            "outcome": "unresolved",
            "candidate_entity_id": None,
            "reason": "Fictional matrix check.",
        }
    return client.request(method.upper(), url, json=body, headers=headers)


@pytest.mark.parametrize("method,path,body", READS + WRITES)
def test_unauthenticated_requests_are_401(client, method, path, body):
    case = _case(client)
    for headers in BAD_HEADERS:
        response = _call(client, method, path, body, case, headers)
        assert response.status_code == 401, (headers, response.text)
        assert response.json()["code"] == "unauthenticated"
        assert response.headers["www-authenticate"] == "Bearer"
        assert REVIEWER not in response.text
    after = client.get(
        f"/v2/cases/{case['case']['case_id']}", headers=bearer(VIEWER)
    ).json()
    assert after["case"]["status"] == "awaiting_review"
    assert len(client.get("/v2/cases", headers=bearer(VIEWER)).json()["cases"]) == 1


@pytest.mark.parametrize("method,path,body", READS)
def test_viewer_and_reviewer_can_read(client, method, path, body):
    case = _case(client)
    for token in (VIEWER, REVIEWER):
        assert _call(client, method, path, body, case, bearer(token)).status_code == 200


@pytest.mark.parametrize("method,path,body", WRITES)
def test_viewer_cannot_write(client, method, path, body):
    case = _case(client)
    response = _call(client, method, path, body, case, bearer(VIEWER))
    assert response.status_code == 403 and response.json()["code"] == "forbidden"
    after = client.get(
        f"/v2/cases/{case['case']['case_id']}", headers=bearer(VIEWER)
    ).json()
    assert after["case"]["status"] == "awaiting_review"
    assert len(client.get("/v2/cases", headers=bearer(VIEWER)).json()["cases"]) == 1


@pytest.mark.parametrize("method,path,body", WRITES)
def test_reviewer_can_write(client, method, path, body):
    case = _case(client)
    assert _call(client, method, path, body, case, bearer(REVIEWER)).status_code == 200


def test_health_is_public(client):
    assert client.get("/health").status_code == 200


def test_bearer_scheme_is_case_insensitive(client):
    assert (
        client.get(
            "/v2/cases", headers={"Authorization": f"bearer {VIEWER}"}
        ).status_code
        == 200
    )


def test_auth_precedes_body_validation(client):
    response = client.post("/v2/cases", json={"name": 1}, headers={})
    assert response.status_code == 401
    response = client.post("/v2/cases", json={"name": 1}, headers=bearer(VIEWER))
    assert response.status_code == 403


# -- identity comes from the key -----------------------------------------------------------


def test_actor_is_the_key_label_for_every_write(client):
    case = _case(client)
    assert case["case"]["header"]["actor"] == "fictional-reviewer"
    case_id = case["case"]["case_id"]
    form = {
        "submission_id": str(uuid4()),
        "revision": case["revision"],
        "packet_sha256": case["case"]["packet_sha256"],
        "outcome": "none_selected",
        "candidate_entity_id": None,
        "reason": "Fictional.",
    }
    for field, value in (
        ("actor", "fictional-reviewer"),
        ("reviewer", "x"),
        ("origin", "test"),
    ):
        response = client.post(
            f"/v2/cases/{case_id}/review",
            json={**form, field: value},
            headers=bearer(OTHER_REVIEWER),
        )
        assert response.status_code == 422
    done = client.post(
        f"/v2/cases/{case_id}/review", json=form, headers=bearer(OTHER_REVIEWER)
    )
    assert (
        done.json()["case"]["review"]["submission"]["actor"] == "fictional-reviewer-2"
    )
    second = _case(client)
    cancelled = client.post(
        f"/v2/cases/{second['case']['case_id']}/cancel",
        json={"reason": "Fictional."},
        headers=bearer(OTHER_REVIEWER),
    )
    assert cancelled.json()["case"]["cancellation"]["actor"] == "fictional-reviewer-2"


def test_same_request_id_from_another_key_conflicts(client):
    request_id = str(uuid4())
    body = {"name": "Luma Vesper", "origin": "test", "request_id": request_id}
    first = client.post("/v2/cases", json=body, headers=bearer(REVIEWER))
    assert first.status_code == 200
    other = client.post("/v2/cases", json=body, headers=bearer(OTHER_REVIEWER))
    assert other.status_code == 409 and other.json()["code"] == "request_conflict"


def test_tokens_never_reach_logs_or_errors(client, caplog):
    caplog.set_level(logging.DEBUG)
    wrong = secrets.token_urlsafe(24)
    client.get("/v2/cases", headers=bearer(wrong))
    client.post(
        "/v2/cases",
        json={"name": "Luma Vesper", "origin": "test"},
        headers=bearer(VIEWER),
    )
    _case(client)
    text = caplog.text
    for token in (wrong, VIEWER, REVIEWER):
        assert token not in text and hash_key(token) not in text


# -- command line -------------------------------------------------------------------------


def test_cli_new_key_and_hash_key(capsys, monkeypatch):
    assert main(["new-key", "--actor", "analyst-1", "--role", "reviewer"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert parse_key_entry(out["entry"]).sha256 == hash_key(out["token"])
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(out["token"] + "\n"))
    assert main(["hash-key"]) == 0
    assert capsys.readouterr().out.strip() == hash_key(out["token"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("short\n"))
    assert main(["hash-key"]) == 1


def test_serve_refuses_non_loopback_and_missing_keys(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(KEYS_VAR, raising=False)
    monkeypatch.delenv(KEYS_FILE_VAR, raising=False)
    config = ["--config", str(CONFIG_PATH)]
    assert (
        main(["serve", "--workspace", str(tmp_path), *config, "--host", "0.0.0.0"]) == 1
    )
    assert "loopback" in capsys.readouterr().err
    assert main(["serve", "--workspace", str(tmp_path), *config]) == 1
    assert "no API keys" in capsys.readouterr().err
    monkeypatch.setenv(KEYS_VAR, f"a:viewer:{'a' * 64}")
    assert main(["serve", "--workspace", str(tmp_path / "missing"), *config]) == 1
    assert "workspace_invalid" in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    "host,ok",
    [
        ("127.0.0.1", True),
        ("localhost", True),
        ("::1", True),
        ("[::1]", True),
        ("127.0.0.2", True),
        ("0.0.0.0", False),
        ("192.168.1.5", False),
        ("example.org", False),
    ],
)
def test_loopback_detection(host, ok):
    assert is_loopback(host) is ok


def test_openapi_command_runs_in_a_fresh_process():
    result = subprocess.run(
        [sys.executable, "-B", "-m", "watchlist.api", "openapi"],
        capture_output=True,
        env=worker_env(),
        cwd=ROOT,
        timeout=120,
        check=True,
    )
    committed = (ROOT / "docs" / "openapi.json").read_bytes().replace(b"\r\n", b"\n")
    assert result.stdout.replace(b"\r\n", b"\n") == committed


def test_serve_requires_explicit_config(tmp_path, capsys):
    # No default config path is valid in both a checkout and an installed copy.
    with pytest.raises(SystemExit) as caught:
        main(["serve", "--workspace", str(tmp_path)])
    assert caught.value.code == 2
    assert "--config" in capsys.readouterr().err


def test_keys_file_with_utf8_byte_order_mark_loads(tmp_path):
    # Windows PowerShell 5 ``Set-Content -Encoding UTF8`` writes EF BB BF first.
    entry = f"fictional-reviewer:reviewer:{hash_key(REVIEWER)}"
    keys_file = tmp_path / "keys.txt"
    keys_file.write_bytes(b"\xef\xbb\xbf" + f"{entry}\r\n".encode("utf-8"))
    settings = ApiSettings.from_env(
        workspace=None, config_path=None, environ={KEYS_FILE_VAR: str(keys_file)}
    )
    assert [k.actor for k in settings.api_keys] == ["fictional-reviewer"]


@pytest.mark.parametrize("route", ["/ready", "/metrics"])
def test_operational_routes_require_viewer_auth(client, route):
    probe = client.app.state.readiness
    before = probe.runs
    assert client.get(route).status_code == 401
    assert (
        client.get(route, headers=bearer("invalid-fictional-token")).status_code == 401
    )
    assert probe.runs == before
    for token in (VIEWER, REVIEWER):
        assert client.get(route, headers=bearer(token)).status_code == 200
