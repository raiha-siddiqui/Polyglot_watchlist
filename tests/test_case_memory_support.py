"""Shared fictional helpers for the case-memory tests (collects no tests itself).

Run as a script, this module is the subprocess crash worker for publication,
retention and backup crash-window tests. All names, IDs, reviewers and notes
are authored fiction.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
WORKER = Path(__file__).resolve()
CRASH_CODE = 77
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from review_v2_support import make_config  # noqa: E402
from watchlist.case_memory.evaluation import (  # noqa: E402
    EpisodeClock,
    ScriptedRetriever,
    authored_snapshot,
    packaged_snapshot,
)
from watchlist.case_memory.provider import MemoryContextProvider  # noqa: E402
from watchlist.case_memory.publisher import Publisher  # noqa: E402
from watchlist.case_memory.state import MemoryConfig  # noqa: E402
from watchlist.case_memory.store import MemoryStore  # noqa: E402
from watchlist.normalize import normalize_name  # noqa: E402

MEMORY_CONFIG = ROOT / "config" / "memory.json"
SNAP_MAIN = packaged_snapshot()
SNAP_REFRESH = authored_snapshot("test-refresh")
SNAP_ALT = authored_snapshot("test-alt")
SNAP_UNREGISTERED = authored_snapshot("test-unregistered")
BASE_TIME = "2026-02-02T10:00:00+00:00"

PACKAGED_NAMES = None


def names(kind: str = "packaged") -> dict[str, str]:
    global PACKAGED_NAMES
    if PACKAGED_NAMES is None:
        from watchlist.entities import load_entities

        PACKAGED_NAMES = {e.entity_id: e.name for e in load_entities()}
    if kind == "packaged":
        return PACKAGED_NAMES
    return {k: f"Alt fictional {k}" for k in PACKAGED_NAMES}


def memory_config_dict(**policy: Any) -> dict[str, Any]:
    data = json.loads(MEMORY_CONFIG.read_text(encoding="utf-8"))
    data["policy"].update(policy)
    data["namespaces"].append(
        {
            "namespace_id": "fictional-alt-v1",
            "sources": ["fictional-alt-v1"],
            "fictional": True,
            "description": "Second authored fictional source reusing syn-* IDs.",
        }
    )
    data["snapshots"] += [
        {
            "snapshot_sha256": SNAP_REFRESH,
            "namespace_id": "fictional-synthetic-v1",
            "label": "refresh",
        },
        {
            "snapshot_sha256": SNAP_ALT,
            "namespace_id": "fictional-alt-v1",
            "label": "alt",
        },
    ]
    return data


def memory_config(**policy: Any) -> MemoryConfig:
    return MemoryConfig.from_dict(memory_config_dict(**policy))


SOURCES = {
    SNAP_MAIN: ("fictional-synthetic-v1", "packaged"),
    SNAP_REFRESH: ("fictional-synthetic-v1", "packaged"),
    SNAP_ALT: ("fictional-alt-v1", "alt"),
    SNAP_UNREGISTERED: ("fictional-synthetic-v1", "packaged"),
}


class Env:
    """A fictional v2 workspace + memory store with scripted retrievers per snapshot."""

    def __init__(self, tmp: Path, *, config: MemoryConfig | None = None, sources=None):
        from watchlist.review.service import ReviewService

        self.tmp = tmp
        self.clock = EpisodeClock(BASE_TIME)
        self.review_config = make_config()
        self.workspace = tmp / "review"
        self.memory_dir = tmp / "memory"
        ReviewService.initialize(self.workspace, self.review_config, clock=self.clock)
        self.store = MemoryStore.initialize(
            self.memory_dir, config or memory_config(), clock=self.clock
        )
        self.provider = MemoryContextProvider(self.store, clock=self.clock)
        self.sources = dict(SOURCES, **(sources or {}))
        self.retrievers: dict[str, ScriptedRetriever] = {}
        self._services: dict[tuple[str, bool], Any] = {}
        self.counter = 0

    def retriever(self, snapshot: str) -> ScriptedRetriever:
        if snapshot not in self.retrievers:
            source, kind = self.sources[snapshot]
            self.retrievers[snapshot] = ScriptedRetriever(
                snapshot, evidence_source=source, names=names(kind), tag=snapshot[:8]
            )
        return self.retrievers[snapshot]

    def service(self, snapshot: str = SNAP_MAIN, *, memory: bool = False):
        from watchlist.review.service import ReviewService

        key = (snapshot, memory)
        if key not in self._services:
            self._services[key] = ReviewService.open(
                self.workspace,
                self.review_config,
                retriever=self.retriever(snapshot),
                context_providers=[self.provider] if memory else [],
                clock=self.clock,
            )
        return self._services[key]

    def reader(self):
        from watchlist.review.service import ReviewService

        return ReviewService.open(self.workspace, self.review_config, read_only=True)

    def start(
        self,
        candidates,
        *,
        day=0,
        origin="test",
        snapshot=SNAP_MAIN,
        memory=False,
        name=None,
    ):
        self.counter += 1
        name = name or f"Fictional query {self.counter}"
        self.retriever(snapshot).script[normalize_name(name)] = list(candidates)
        self.clock.set_day(day)
        return self.service(snapshot, memory=memory).start_case(
            name, origin=origin, actor="fictional-tester"
        )

    def review(
        self,
        candidates,
        *,
        outcome="candidate_for_follow_up",
        selected="__first__",
        reason="Fictional reviewer note.",
        day=0,
        origin="test",
        snapshot=SNAP_MAIN,
        memory=False,
    ):
        view = self.start(
            candidates, day=day, origin=origin, snapshot=snapshot, memory=memory
        )
        if selected == "__first__":
            selected = candidates[0] if outcome == "candidate_for_follow_up" else None
        return self.service(snapshot, memory=memory).submit_review(
            view.case_id,
            {
                "submission_id": str(uuid4()),
                "packet_sha256": view.packet_sha256,
                "outcome": outcome,
                "candidate_entity_id": selected,
                "reason": reason,
                "actor": "fictional-reviewer",
                "origin": origin,
            },
        )

    def publisher(self, workspace_id: str = "ws-test") -> Publisher:
        return Publisher(self.store, self.reader(), workspace_id)

    def publish(self, **kw):
        return self.publisher().publish(**kw)


def context_keys(view) -> dict[str, dict[str, Any]]:
    data = view.packet.context["case_memory"]["data"]
    return {k["source_entity_id"]: k for k in data["keys"]}


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
    """``action workspace memory_dir stage``; prints one JSON line unless it crashes."""
    from watchlist.case_memory.lifecycle import apply_retention, plan_retention
    from watchlist.review.contracts import ReviewError
    from watchlist.review.service import ReviewService

    action, workspace, memory_dir, stage = sys.argv[1:5]
    # Retention drills run with the store clock at the plan time: a plan dated
    # after the store clock is a forecast and is refused.
    clock = EpisodeClock(sys.argv[5]) if action == "retention" else None
    store = MemoryStore.open(
        Path(memory_dir),
        memory_config(),
        failpoint=_crash_at(stage),
        lock_timeout=60.0,
        clock=clock,
    )
    try:
        if action == "publish":
            reader = ReviewService.open(Path(workspace), make_config(), read_only=True)
            result = Publisher(store, reader, "ws-test").publish().to_dict()
        elif action == "retention":
            plan = plan_retention(store, now=sys.argv[5])
            result = apply_retention(
                store, plan, actor="fictional-maintainer", reason="Fictional drill."
            ).to_dict()
        else:
            raise SystemExit(f"unknown action {action}")
        print(json.dumps({"ok": True, "result": result}))
    except ReviewError as exc:
        print(json.dumps({"ok": False, "error": exc.code}))


if __name__ == "__main__":
    worker()
