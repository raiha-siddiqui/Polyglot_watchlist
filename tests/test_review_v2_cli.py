"""The v2 command group and the unchanged archive command surface."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from review_v2_support import CONFIG_PATH, ROOT, tree, worker_env
from watchlist.review.cli import main


def run(capsys, *args):
    code = main(["v2", *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_lifecycle_with_default_retriever(tmp_path, capsys):
    workspace = str(tmp_path / "cli workspace")
    common = ["--workspace", workspace, "--config", str(CONFIG_PATH)]
    code, out, _ = run(capsys, "init", *common)
    assert code == 0 and json.loads(out)["status"] == "initialized"
    code, out, _ = run(
        capsys,
        "start",
        *common,
        "--name",
        "Mira Velune",
        "--origin",
        "simulated_demo",
        "--actor",
        "fictional-cli-reviewer",
    )
    view = json.loads(out)
    assert code == 0 and view["status"] == "awaiting_review"
    assert view["packet"]["retrieval"]["candidates"][0]["entity_id"] == "syn-005"
    form = {
        "submission_id": "9b0f0c55-7a9e-4f0e-9d7b-2f1e1d8f3c11",
        "packet_sha256": view["packet_sha256"],
        "outcome": "unresolved",
        "candidate_entity_id": None,
        "reason": "Fictional CLI walkthrough.",
        "actor": "fictional-cli-reviewer",
        "origin": "simulated_demo",
    }
    path = tmp_path / "form.json"
    path.write_text(json.dumps(form), encoding="utf-8", newline="\n")
    code, out, _ = run(
        capsys,
        "review",
        *common,
        "--case-id",
        view["case_id"],
        "--submission",
        str(path),
    )
    assert code == 0 and json.loads(out)["status"] == "completed"
    before = tree(tmp_path / "cli workspace")
    code, out, _ = run(capsys, "status", *common, "--case-id", view["case_id"])
    assert (
        code == 0 and json.loads(out)["review"]["submission"]["outcome"] == "unresolved"
    )
    code, out, _ = run(capsys, "list", *common)
    assert [c["case_id"] for c in json.loads(out)["cases"]] == [view["case_id"]]
    assert "Mira Velune" not in out
    code, out, _ = run(capsys, "audit", *common, "--case-id", view["case_id"])
    audit = json.loads(out)
    assert audit["count"] == 5
    anchor = tmp_path / "anchor.json"
    anchor.write_text(
        json.dumps(
            {
                "case_id": view["case_id"],
                "count": 5,
                "head_sha256": audit["head_sha256"],
            }
        ),
        encoding="utf-8",
    )
    code, out, _ = run(
        capsys, "verify", *common, "--case-id", view["case_id"], "--anchor", str(anchor)
    )
    assert (
        code == 0
        and json.loads(out)["ok"] is True
        and json.loads(out)["anchor_checked"] is True
    )
    assert tree(tmp_path / "cli workspace") == before


def test_cli_errors_are_structured_json(tmp_path, capsys):
    workspace = str(tmp_path / "w")
    common = ["--workspace", workspace, "--config", str(CONFIG_PATH)]
    code, out, err = run(
        capsys, "status", *common, "--case-id", "00000000-0000-4000-8000-000000000000"
    )
    assert code == 1 and out == "" and json.loads(err)["error"] == "workspace_invalid"
    assert not (tmp_path / "w").exists()
    run(capsys, "init", *common)
    code, _, err = run(capsys, "init", *common)
    assert code == 1 and json.loads(err)["error"] == "workspace_invalid"
    code, _, err = run(
        capsys, "start", *common, "--name", "   ", "--origin", "test", "--actor", "a"
    )
    assert code == 1 and json.loads(err)["error"] == "invalid_input"
    bad = tmp_path / "bad.json"
    bad.write_text('{"submission_id": "a", "submission_id": "b"}', encoding="utf-8")
    code, _, err = run(
        capsys,
        "review",
        *common,
        "--case-id",
        "00000000-0000-4000-8000-000000000000",
        "--submission",
        str(bad),
    )
    assert code == 1 and json.loads(err)["error"] == "invalid_input"
    code, _, err = run(
        capsys,
        "cancel",
        *common,
        "--case-id",
        "00000000-0000-4000-8000-000000000000",
        "--actor",
        "a",
        "--reason",
        "r",
    )
    assert code == 1 and json.loads(err)["error"] == "case_not_found"


def test_cli_rejects_unknown_origin_at_parse_time(tmp_path, capsys):
    with pytest.raises(SystemExit) as caught:
        main(
            [
                "v2",
                "start",
                "--workspace",
                str(tmp_path),
                "--name",
                "x",
                "--origin",
                "robot",
                "--actor",
                "a",
            ]
        )
    assert caught.value.code == 2


def test_archive_commands_are_still_available(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["archive", "verify", "--help"])
    assert caught.value.code == 0
    assert "--archive" in capsys.readouterr().out
    with pytest.raises(SystemExit) as caught:
        main(["archive", "inspect", "--archive", "x"])
    assert caught.value.code == 2


def test_module_entry_point_runs_v2_in_a_subprocess(tmp_path):
    workspace = tmp_path / "module"
    base = [sys.executable, "-B", "-m", "watchlist.review", "v2"]
    common = ["--workspace", str(workspace), "--config", str(CONFIG_PATH)]
    init = subprocess.run(
        [*base, "init", *common],
        capture_output=True,
        text=True,
        timeout=180,
        env=worker_env(),
        cwd=ROOT,
    )
    assert init.returncode == 0, init.stderr
    listed = subprocess.run(
        [*base, "list", *common],
        capture_output=True,
        text=True,
        timeout=180,
        env=worker_env(),
        cwd=ROOT,
    )
    assert listed.returncode == 0 and json.loads(listed.stdout) == {"cases": []}


def test_cli_default_config_and_relative_parent_workspace_work_outside_the_repo(
    tmp_path, capsys, monkeypatch
):
    # Regressions: the default config used to be relative to the current
    # directory, and '../x' workspaces were rejected as parent traversal.
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    code, out, err = run(capsys, "init", "--workspace", "../relative-ws")
    assert code == 0, err
    assert (
        Path(json.loads(out)["workspace"]).resolve()
        == (tmp_path / "relative-ws").resolve()
    )
    code, out, _ = run(
        capsys,
        "start",
        "--workspace",
        "../relative-ws",
        "--name",
        "Mira Velune",
        "--origin",
        "test",
        "--actor",
        "fictional-cli",
        "--request-id",
        "0d9b1d2e-5a51-4c39-9b8e-1f0a4c7a9e01",
    )
    first = json.loads(out)
    code, out, _ = run(
        capsys,
        "start",
        "--workspace",
        "../relative-ws",
        "--name",
        "Mira Velune",
        "--origin",
        "test",
        "--actor",
        "fictional-cli",
        "--request-id",
        "0d9b1d2e-5a51-4c39-9b8e-1f0a4c7a9e01",
    )
    assert code == 0 and json.loads(out)["case_id"] == first["case_id"]
    code, out, _ = run(
        capsys, "list", "--workspace", "../relative-ws", "--origin", "human"
    )
    assert code == 0 and json.loads(out) == {"cases": []}
    code, out, _ = run(
        capsys, "list", "--workspace", "../relative-ws", "--origin", "test"
    )
    assert [c["case_id"] for c in json.loads(out)["cases"]] == [first["case_id"]]


def test_cli_verify_returns_failure_for_a_wrong_anchor(tmp_path, capsys):
    workspace = str(tmp_path / "ws")
    common = ["--workspace", workspace, "--config", str(CONFIG_PATH)]
    assert run(capsys, "init", *common)[0] == 0
    code, out, _ = run(
        capsys,
        "start",
        *common,
        "--name",
        "Luma Vesper",
        "--origin",
        "test",
        "--actor",
        "fixture",
    )
    assert code == 0
    case = json.loads(out)
    anchor = tmp_path / "anchor.json"
    anchor.write_text(
        json.dumps({"case_id": case["case_id"], "count": 1, "head_sha256": "0" * 64}),
        encoding="utf-8",
    )
    code, out, err = run(
        capsys, "verify", *common, "--case-id", case["case_id"], "--anchor", str(anchor)
    )
    assert code == 1 and err == ""
    assert json.loads(out)["ok"] is False
