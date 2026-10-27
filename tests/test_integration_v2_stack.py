"""End-to-end wiring of the v2 stack on fictional data (Step 29).

One workspace, one memory store, the packaged fictional entities and the real
RapidFuzz retriever: start a case through the API -> human review through the
API -> publish to memory -> a new case for the same exact entity shows the
earlier annotation as display-only context -> ranking unchanged; the MCP tool
layer and the UI helpers read the same case. Also checks the opt-in memory
options of the API, MCP and ``watchlist.stack`` command lines. No network
socket is opened (the API runs in process) and no model is loaded.
"""

from __future__ import annotations

import json
from pathlib import Path
import secrets
from uuid import uuid4

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("portalocker")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from review_v2_support import CONFIG_PATH, ROOT
from watchlist.api import ApiSettings, create_app
from watchlist.api.auth import ApiKey, hash_key
from watchlist.case_memory.provider import MemoryContextProvider
from watchlist.case_memory.publisher import Publisher
from watchlist.case_memory.store import MemoryStore
from watchlist.review.contracts import LIMITATION, ReviewError
from watchlist.review.service import ReviewService
from watchlist import stack

MEMORY_CONFIG = ROOT / "config" / "memory.json"
NAME = "Luma Vesper"
REVIEWER = secrets.token_urlsafe(24)
KEYS = (ApiKey("fictional-reviewer", "reviewer", hash_key(REVIEWER)),)
AUTH = {"Authorization": f"Bearer {REVIEWER}"}


def ranking(case: dict) -> list[tuple[int, str, float]]:
    return [
        (c["rank"], c["entity_id"], c["score"])
        for c in case["packet"]["retrieval"]["candidates"]
    ]


def memory_data(case: dict) -> dict | None:
    entry = (case["packet"].get("context") or {}).get("case_memory")
    return None if entry is None else entry["data"]


def key_for(memory: dict, entity_id: str) -> dict:
    (key,) = [k for k in memory["keys"] if k["source_entity_id"] == entity_id]
    return key


@pytest.fixture
def stack_dirs(tmp_path):
    workspace, memory = tmp_path / "workspace", tmp_path / "memory"
    ReviewService.initialize(workspace, CONFIG_PATH)
    MemoryStore.initialize(memory, MEMORY_CONFIG)
    return workspace, memory


def start(client, name: str = NAME) -> dict:
    response = client.post(
        "/v2/cases",
        json={"name": name, "origin": "test", "request_id": str(uuid4())},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_api_review_publish_then_memory_context_without_ranking_change(stack_dirs):
    workspace, memory_dir = stack_dirs
    providers = stack.memory_context_providers(memory_dir, MEMORY_CONFIG)
    assert [type(p) for p in providers] == [MemoryContextProvider]
    settings = ApiSettings(workspace=workspace, config_path=CONFIG_PATH, api_keys=KEYS)
    app = create_app(settings, context_providers=providers)
    with TestClient(app) as client:
        first = start(client)
        case1 = first["case"]
        assert first["limitation"] == LIMITATION
        assert case1["status"] == "awaiting_review"
        memory1 = memory_data(case1)
        assert memory1["status"] == "ok"
        assert [k["source_entity_id"] for k in memory1["keys"]] == [
            e for _, e, _ in ranking(case1)
        ]
        assert all(k["cards"] == [] for k in memory1["keys"])
        assert ranking(case1)[0][1] == "syn-001"

        reviewed = client.post(
            f"/v2/cases/{case1['case_id']}/review",
            json={
                "submission_id": str(uuid4()),
                "revision": first["revision"],
                "packet_sha256": case1["packet_sha256"],
                "outcome": "candidate_for_follow_up",
                "candidate_entity_id": "syn-001",
                "reason": "Fictional integration test: possible record match.",
            },
            headers=AUTH,
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["case"]["status"] == "completed"

        # Publication is an explicit operator step with a writable handle.
        report = Publisher(
            MemoryStore.open(memory_dir, MEMORY_CONFIG),
            ReviewService.open(workspace, CONFIG_PATH, read_only=True),
            "integration-ws",
        ).publish()
        assert (report.published, report.pending, report.blocked) == (1, 0, 0)
        assert report.quarantined == ()

        case2 = start(client)["case"]
        memory2 = memory_data(case2)
        assert memory2["status"] == "ok"
        selected = key_for(memory2, "syn-001")
        assert selected["shown_count"] == 1
        (card,) = selected["cards"]
        assert card["case_id"] == case1["case_id"]
        assert card["outcome"] == "candidate_for_follow_up"
        assert card["relation"] == "selected_for_follow_up"
        assert card["origin"] == "test"
        other = key_for(memory2, "syn-012")
        assert [c["relation"] for c in other["cards"]] == [
            "considered_without_selection"
        ]
        # Memory is display-only: identical candidates, ranks and scores.
        assert ranking(case2) == ranking(case1)
        assert case2["packet_sha256"] != case1["packet_sha256"]

        verify = client.get(f"/v2/cases/{case1['case_id']}/audit/verify", headers=AUTH)
        assert verify.status_code == 200 and verify.json()["verification"]["ok"]

    # Same backend without memory: same ranking, no memory context at all.
    plain = ReviewService.open(workspace, CONFIG_PATH).start_case(
        NAME, origin="test", actor="fictional-cli", request_id=str(uuid4())
    )
    assert "case_memory" not in plain.packet.context
    assert [
        (c.rank, c.entity_id, c.score) for c in plain.packet.retrieval.candidates
    ] == ranking(case1)

    # The UI helpers render the frozen memory panel of the second case.
    components = pytest.importorskip("watchlist.ui.components")
    panel = components.memory_panel(case2)
    assert panel.status == "ok"
    shown = {k.entity_id: k for k in panel.keys}
    assert [c.case_id for c in shown["syn-001"].cards] == [case1["case_id"]]
    html = components.memory_html(panel)
    assert case1["case_id"] in html and "selected_for_follow_up" in html

    # MCP reads the same case through the same backend, with memory wired in.
    pytest.importorskip("mcp")
    from watchlist.mcp_server import tools
    from watchlist.mcp_server.server import ServerSettings, open_runtime

    runtime = open_runtime(
        ServerSettings(
            workspace=workspace,
            origin="test",
            config=CONFIG_PATH,
            memory_store=memory_dir,
            memory_config=MEMORY_CONFIG,
        )
    )
    seen = tools.get_case(runtime, case1["case_id"])
    assert seen["status"] == "completed"
    assert seen["packet_sha256"] == case1["packet_sha256"]
    assert seen["review"]["outcome"] == "candidate_for_follow_up"
    assert seen["review"]["candidate_entity_id"] == "syn-001"
    assert seen["limitation"]
    screened = tools.screen_name(runtime, NAME, str(uuid4()))
    assert [(c["rank"], c["entity_id"]) for c in screened["candidates"]] == [
        (r, e) for r, e, _ in ranking(case1)
    ]
    mcp_case = runtime.service.get_case(screened["case_id"])
    mcp_memory = mcp_case.packet.context["case_memory"]["data"]
    assert [c["case_id"] for c in key_for(mcp_memory, "syn-001")["cards"]] == [
        case1["case_id"]
    ]


def test_memory_options_must_come_together(tmp_path):
    assert stack.memory_context_providers(None, None) == ()
    with pytest.raises(stack.MemoryOptionError) as caught:
        stack.memory_context_providers(tmp_path / "memory", None)
    assert caught.value.code == "invalid_input"
    with pytest.raises(stack.MemoryOptionError):
        stack.validate_memory_options(None, MEMORY_CONFIG)
    with pytest.raises(stack.MemoryOptionError) as caught:
        stack.validate_memory_options(
            Path("relative-store"), MEMORY_CONFIG, require_absolute=True
        )
    assert caught.value.details["options"] == ["--memory-store"]


def test_missing_memory_store_is_an_error_not_an_empty_history(tmp_path):
    with pytest.raises(ReviewError) as caught:
        stack.memory_context_providers(tmp_path / "absent", MEMORY_CONFIG)
    assert caught.value.code == "memory_unavailable"
    assert not (tmp_path / "absent").exists()


def test_api_serve_wires_memory_only_when_asked(stack_dirs, monkeypatch, capsys):
    import uvicorn

    from watchlist.api import __main__ as api_cli
    from watchlist.api import app as api_app

    workspace, memory_dir = stack_dirs
    monkeypatch.setenv(
        "WATCHLIST_API_KEYS", f"fictional-reviewer:reviewer:{KEYS[0].sha256}"
    )
    monkeypatch.delenv("WATCHLIST_API_KEYS_FILE", raising=False)
    captured = []
    real_create_app = api_app.create_app

    def recording_create_app(settings, **kwargs):
        captured.append(kwargs.get("context_providers", ()))
        return real_create_app(settings, **kwargs)

    monkeypatch.setattr(api_app, "create_app", recording_create_app)
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: None)
    base = ["serve", "--workspace", str(workspace), "--config", str(CONFIG_PATH)]

    assert api_cli.main(base) == 0
    assert tuple(captured.pop()) == ()
    memory = ["--memory-store", str(memory_dir), "--memory-config", str(MEMORY_CONFIG)]
    assert api_cli.main(base + memory) == 0
    assert [type(p) for p in captured.pop()] == [MemoryContextProvider]

    capsys.readouterr()
    assert api_cli.main(base + memory[:2]) == 1
    assert "invalid_input" in capsys.readouterr().err
    assert not captured


def test_mcp_launcher_refuses_incomplete_or_relative_memory_options(stack_dirs, capsys):
    pytest.importorskip("mcp")
    from watchlist.mcp_server.__main__ import main

    workspace, memory_dir = stack_dirs
    base = [
        "--workspace",
        str(workspace),
        "--config",
        str(CONFIG_PATH),
        "--origin",
        "test",
    ]
    capsys.readouterr()
    assert main(base + ["--memory-store", str(memory_dir)]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "invalid_input"
    assert (
        main(base + ["--memory-store", "memory", "--memory-config", str(MEMORY_CONFIG)])
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert error["details"]["options"] == ["--memory-store"]


def test_stack_cli_memory_init_and_publish(tmp_path, capsys):
    workspace, store = tmp_path / "ws", tmp_path / "memory"
    service = ReviewService.initialize(workspace, CONFIG_PATH)
    view = service.start_case(
        NAME, origin="test", actor="fictional-cli", request_id=str(uuid4())
    )
    service.submit_review(
        view.case_id,
        {
            "submission_id": str(uuid4()),
            "packet_sha256": view.packet_sha256,
            "outcome": "none_selected",
            "candidate_entity_id": None,
            "reason": "Fictional integration test: no candidate selected.",
            "actor": "fictional-cli",
            "origin": "test",
        },
    )
    capsys.readouterr()
    memory = ["--store", str(store), "--memory-config", str(MEMORY_CONFIG)]
    assert stack.main(["memory-init", *memory]) == 0
    assert json.loads(capsys.readouterr().out)["memory_head"]["sequence"] >= 1
    assert stack.main(["memory-init", *memory]) == 1
    assert json.loads(capsys.readouterr().err)["error"] == "memory_invalid_input"
    publish = [
        "memory-publish",
        *memory,
        "--workspace",
        str(workspace),
        "--config",
        str(CONFIG_PATH),
        "--workspace-id",
        "integration-ws",
    ]
    assert stack.main(publish) == 0
    first = json.loads(capsys.readouterr().out)["publication"]
    assert (first["scanned"], first["published"], first["pending"]) == (1, 1, 0)
    assert stack.main(publish) == 0
    again = json.loads(capsys.readouterr().out)["publication"]
    assert (again["already_enqueued"], again["published"]) == (1, 0)


def test_demo_walkthrough_runs_in_a_new_directory(tmp_path):
    lines: list[str] = []
    summary = stack.run_demo(tmp_path / "demo", log=lines.append)
    assert lines[0] == LIMITATION
    assert summary["ranking_unchanged"] is True
    assert summary["case_1_audit_ok"] is True
    assert summary["case_1"]["status"] == "completed"
    assert summary["case_1"]["memory_status"] == "ok"
    assert summary["publication"]["published"] == 1
    prior = summary["case_2"]["prior_annotations"]
    assert {
        "entity_id": "syn-001",
        "case_id": summary["case_1"]["case_id"],
        "outcome": "candidate_for_follow_up",
        "relation": "selected_for_follow_up",
        "origin": "simulated_demo",
    } in prior
    saved = json.loads((tmp_path / "demo" / "demo-summary.json").read_text("utf-8"))
    assert saved["case_2"]["prior_annotations"] == prior
    with pytest.raises(ReviewError) as caught:
        stack.run_demo(tmp_path / "demo", log=lines.append)
    assert caught.value.code == "invalid_input"


def test_default_demo_output_is_bound_to_checkout_not_current_directory(
    tmp_path, monkeypatch, capsys
):
    recorded = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(stack, "run_demo", lambda path: recorded.append(path) or {})
    assert stack.main(["demo"]) == 0
    assert recorded[0].parent == ROOT / "data" / "demo"
    assert recorded[0].name.startswith("run-")
    assert not (tmp_path / "data").exists()
