"""Fictional fakes, helpers and a subprocess worker for the v2 review tests.

Everything here is test-only. Names are authored fictional fixtures; no real
person, list or model is involved. Run as a script, this module is the crash
and race worker used by the subprocess tests.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "review-v2.json"
WORKER = Path(__file__).resolve()
CRASH_CODE = 77

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from watchlist.review.contracts import (  # noqa: E402
    AliasEvidence,
    Candidate,
    RetrievalResult,
    RetrieverDescriptor,
    ReviewConfig,
    digest,
)


def config_dict() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


OPERATIONS_KEYS = ("max_cases", "page_max")


def make_config(*, lock_timeout: float | None = None, **overrides: Any) -> ReviewConfig:
    """The project config with optional workflow-policy or operations overrides."""
    data = copy.deepcopy(config_dict())
    for key, value in overrides.items():
        data["operations" if key in OPERATIONS_KEYS else "workflow"][key] = value
    if lock_timeout is not None:
        data["operations"]["lock_timeout_seconds"] = lock_timeout
    return ReviewConfig.from_dict(data)


class FakeRetriever:
    """Deterministic authored candidates; counts calls and can refuse to run."""

    def __init__(
        self,
        *,
        fail: bool = False,
        empty: bool = False,
        tag: str = "fixture-v1",
        alias: str = "Luma Vesper",
    ):
        self.calls = 0
        self.fail, self.empty, self.alias = fail, empty, alias
        self._descriptor = RetrieverDescriptor(
            "fake",
            "authored-fixture-retriever",
            tag,
            digest(["fixture", tag]),
            {"fixture": tag},
        )

    def descriptor(self) -> RetrieverDescriptor:
        return self._descriptor

    def search(self, normalized_name: str, *, top_k: int) -> RetrievalResult:
        self.calls += 1
        if self.fail:
            raise RuntimeError("retrieval must not run")
        candidates, evidence = [], []
        for rank, entity in enumerate([] if self.empty else ["syn-001", "syn-012"], 1):
            evidence_id = f"{entity}/{entity}-a1"
            evidence.append(
                AliasEvidence(
                    evidence_id,
                    entity,
                    f"{entity}-a1",
                    self.alias,
                    "fictional-synthetic-v1",
                    True,
                    "Authored fictional fixture",
                    None,
                )
            )
            candidates.append(
                Candidate(
                    rank,
                    entity,
                    self.alias,
                    "person",
                    f"{entity}-a1",
                    self.alias,
                    100.0,
                    "fixture_score",
                    (evidence_id,),
                )
            )
        return RetrievalResult(
            self._descriptor, top_k, tuple(candidates), tuple(evidence)
        )


class StaticContext:
    """A context provider returning fixed fictional display-only data."""

    def __init__(
        self,
        name: str = "fictional_notes",
        data: Any = None,
        *,
        version: str = "v1",
        fail: bool = False,
    ):
        self.name, self.version = name, version
        self.data = (
            {"notes": ["Fictional prior annotation placeholder."]}
            if data is None
            else data
        )
        self.fail = fail
        self.requests = []

    def provide(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("context store unavailable")
        return self.data


def submission(view, **changes: Any) -> dict[str, Any]:
    """A valid review form for an awaiting case; override fields with ``changes``."""
    form = {
        "submission_id": str(uuid4()),
        "packet_sha256": view.packet_sha256,
        "outcome": "candidate_for_follow_up",
        "candidate_entity_id": "syn-001",
        "reason": "Fictional test; follow up on the possible record match.",
        "actor": "fictional-reviewer",
        "origin": "test",
    }
    form.update(changes)
    return form


def tree(root: Path) -> dict[str, tuple[bytes, int]]:
    """Names, bytes and modification times of every file under ``root``."""
    return {
        str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(Path(root).rglob("*"))
        if path.is_file()
    }


def worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def worker_command(*args: str) -> list[str]:
    return [sys.executable, "-B", str(WORKER), *args]


def _crash_at(stage: str):
    def failpoint(point: str) -> None:
        if point == stage:
            sys.stdout.flush()
            os._exit(CRASH_CODE)

    return failpoint


def worker() -> None:
    """``action workspace stage [case_id submission_file]``; prints one JSON line."""
    from watchlist.review.contracts import ReviewError
    from watchlist.review.locking import workspace_lock
    from watchlist.review.service import ReviewService

    action, workspace, stage = sys.argv[1:4]
    if action == "hold-lock":
        with workspace_lock(Path(workspace), timeout=10):
            print("locked", flush=True)
            sys.stdin.readline()
        return
    service = ReviewService.open(
        Path(workspace),
        make_config(lock_timeout=60.0),
        retriever=FakeRetriever(),
        failpoint=_crash_at(stage),
    )
    try:
        if action == "start":
            result = service.start_case(
                "Luma Vesper", origin="test", actor="fictional-worker"
            ).to_dict()
        elif action == "review":
            form = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
            result = service.submit_review(sys.argv[4], form).to_dict()
        else:
            raise SystemExit(f"unknown action {action}")
        print(json.dumps({"ok": True, "status": result["status"]}))
    except ReviewError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))


if __name__ == "__main__":
    worker()
