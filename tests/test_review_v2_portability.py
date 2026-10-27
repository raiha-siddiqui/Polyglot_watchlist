"""Windows/Linux portability and import-weight guarantees of the v2 service."""

import ast
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from review_v2_support import FakeRetriever, ROOT, make_config, submission, worker_env
from watchlist.review import contracts as c
from watchlist.review.service import ReviewService

V2_MODULES = (
    "contracts",
    "validation",
    "storage",
    "workflow",
    "nodes",
    "service",
    "recovery",
    "locking",
)
SOURCE = ROOT / "src" / "watchlist" / "review"


def test_v2_sources_avoid_posix_only_calls_and_implicit_encodings():
    posix_only = {"fcntl", "getpass", "pwd", "grp", "termios", "resource"}
    for name in V2_MODULES:
        text = (SOURCE / f"{name}.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & posix_only, (name, imported & posix_only)
        for forbidden in (
            "os.getuid(",
            "os.geteuid(",
            "os.chmod(",
            ".chmod(",
            "os.fork(",
        ):
            assert forbidden not in text, (name, forbidden)
        for call in re.findall(r"read_text\([^)]*\)", text):
            assert "encoding=" in call, (name, call)
        assert "watchlist.agent" not in text, name


def test_workspace_path_with_spaces_and_non_ascii_characters(tmp_path):
    root = tmp_path / "räume mit Leerzeichen" / "名前 v2"
    service = ReviewService.initialize(root, make_config(), retriever=FakeRetriever())
    view = service.start_case("Ẹlo Nivara", origin="test", actor="fictional-analyst")
    assert service.submit_review(view.case_id, submission(view)).status == "completed"
    reader = ReviewService.open(root, make_config(), read_only=True)
    assert reader.get_case(view.case_id).header.name == "Ẹlo Nivara"


def test_right_to_left_and_mixed_script_names_round_trip(tmp_path):
    service = ReviewService.initialize(
        tmp_path / "rtl", make_config(), retriever=FakeRetriever()
    )
    for name in ("لومى فسبر", "Luma ويسبر", "Лума Веспер"):
        view = service.start_case(name, origin="test", actor="fictional-analyst")
        assert (
            c.CaseView.from_dict(json.loads(json.dumps(view.to_dict()))).header.name
            == name
        )


@pytest.mark.parametrize("mark", ["\u061c", "\u200e", "\u200f"])
def test_direction_marks_are_rejected_before_persistence(tmp_path, mark):
    service = ReviewService.initialize(
        tmp_path / "rtl", make_config(), retriever=FakeRetriever()
    )
    with pytest.raises(c.ValidationError, match="format"):
        service.start_case(
            f"Luma {mark}ويسبر", origin="test", actor="fictional-analyst"
        )
    assert service.list_cases() == []


IMPORT_PROBE = r"""
import json, sys
import watchlist.review.contracts, watchlist.review.validation, watchlist.review.service
import watchlist.review.storage, watchlist.review.recovery, watchlist.review.locking
from watchlist.review.nodes import E5Retriever
loaded_light = sorted(m for m in ('langgraph', 'torch', 'sentence_transformers', 'portalocker', 'watchlist.agent')
                      if m in sys.modules)
retriever = E5Retriever('missing-embeddings-config.json')
try:
    retriever.descriptor()
    outcome = 'unexpected'
except Exception as exc:
    outcome = type(exc).__name__
heavy = sorted(m for m in ('torch', 'sentence_transformers', 'transformers') if m in sys.modules)
print(json.dumps({'light': loaded_light, 'e5_error': outcome, 'heavy': heavy}))
"""


def test_core_service_imports_are_light_and_e5_is_lazy():
    result = subprocess.run(
        [sys.executable, "-B", "-c", IMPORT_PROBE],
        capture_output=True,
        text=True,
        timeout=300,
        env=worker_env(),
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    probe = json.loads(result.stdout)
    # nodes.py imports langgraph (for interrupt); everything else stays light.
    assert probe["light"] == ["langgraph"]
    assert probe["e5_error"] == "RetrievalError"
    assert probe["heavy"] == []


def test_service_module_import_does_not_load_langgraph():
    probe = (
        "import sys, watchlist.review.service, watchlist.review.contracts; "
        "print(sorted(m for m in ('langgraph', 'portalocker', 'torch') if m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        text=True,
        timeout=300,
        env=worker_env(),
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_config_file_is_utf8_lf_json():
    raw = (ROOT / "config" / "review-v2.json").read_bytes()
    assert b"\r\n" not in raw
    assert json.loads(raw.decode("utf-8"))["config_version"] == c.CONFIG_VERSION


def test_schema_file_is_packaged_next_to_storage():
    assert (Path(c.__file__).with_name("schema.sql")).is_file()
