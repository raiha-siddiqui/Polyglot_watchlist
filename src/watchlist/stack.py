"""Opt-in wiring of the v2 interface stack: memory provider, operator commands, demo.

Research-only. Every interface (CLI, API, MCP) uses the one v2
``ReviewService``; this module only decides which display-only context
providers that service is opened with, and offers the small operator commands
needed to run the fictional stack end to end::

    python -B -m watchlist.stack memory-init --store PATH --memory-config PATH
    python -B -m watchlist.stack memory-publish --store PATH --memory-config PATH \
        --workspace PATH --config PATH --workspace-id LABEL
    python -B -m watchlist.stack demo [--output-root NEW_DIR]

Reviewer memory is **opt-in**: an interface attaches the memory provider only
when both ``--memory-store`` and ``--memory-config`` are given. The store is
opened read-only for lookups; publishing completed cases is a separate,
explicit operator step (``memory-publish``). Memory shows dated earlier
annotations about exact reference-record IDs; it never changes candidates,
ranks or scores and never states identity, wrongdoing, sanctions status or
clearance.

Nothing here downloads anything, opens a network socket or reads an API key
from the repository. The demo runs the API in process (no listening socket)
with a key generated for that run only.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
from typing import Any
from uuid import uuid4

from watchlist.review.contracts import (
    LIMITATION,
    ContextProvider,
    ReviewError,
    ValidationError,
)

__all__ = [
    "DEFAULT_DEMO_ROOT",
    "DEMO_NAME",
    "MemoryOptionError",
    "add_memory_arguments",
    "main",
    "memory_context_providers",
    "memory_providers_from_args",
    "run_demo",
    "validate_memory_options",
]

DEMO_NAME = "Luma Vesper"
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEMO_ROOT = _REPO_ROOT / "data" / "demo"


class MemoryOptionError(ValidationError):
    """The memory options were incomplete or inconsistent (``invalid_input``)."""


def add_memory_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the opt-in ``--memory-store`` / ``--memory-config`` pair to a parser."""
    group = parser.add_argument_group(
        "reviewer memory (opt-in, display-only prior annotations)"
    )
    group.add_argument(
        "--memory-store",
        type=Path,
        metavar="PATH",
        help="existing memory store directory (create it with "
        "'python -m watchlist.stack memory-init'); opened read-only",
    )
    group.add_argument(
        "--memory-config",
        type=Path,
        metavar="PATH",
        help="memory config JSON (for example config/memory.json); required "
        "with --memory-store",
    )


def validate_memory_options(
    store: Path | str | None,
    config: Path | str | None,
    *,
    require_absolute: bool = False,
) -> bool:
    """Check the memory option pair without opening anything; True when enabled.

    Both or neither must be given (``MemoryOptionError`` otherwise).
    ``require_absolute`` refuses relative paths; the MCP launcher uses it
    because an MCP client chooses the server's working directory.
    """
    if store is None and config is None:
        return False
    if store is None or config is None:
        raise MemoryOptionError(
            "--memory-store and --memory-config must be given together",
            details={"memory_store_set": store is not None},
        )
    if require_absolute:
        relative = [
            option
            for option, path in (("--memory-store", store), ("--memory-config", config))
            if not Path(path).is_absolute()
        ]
        if relative:
            raise MemoryOptionError(
                "memory paths must be absolute", details={"options": relative}
            )
    return True


def memory_context_providers(
    store: Path | str | None, config: Path | str | None
) -> tuple[ContextProvider, ...]:
    """Open an existing memory store read-only and return its context provider.

    Returns ``()`` when neither path is given (memory disabled). Giving only one
    of the two raises ``MemoryOptionError``. A missing, foreign or unreadable
    store raises the memory layer's own structured ``ReviewError`` subclass; it
    is never silently treated as an empty history.
    """
    if not validate_memory_options(store, config):
        return ()
    from watchlist.case_memory.provider import MemoryContextProvider
    from watchlist.case_memory.store import MemoryStore

    handle = MemoryStore.open(Path(store), Path(config), read_only=True)
    return (MemoryContextProvider(handle),)


def memory_providers_from_args(
    args: argparse.Namespace, *, require_absolute: bool = False
) -> tuple[ContextProvider, ...]:
    """``memory_context_providers`` for parsed ``add_memory_arguments`` options.

    ``require_absolute`` refuses relative paths (the MCP launcher uses it,
    because an MCP client chooses the server's working directory).
    """
    store, config = args.memory_store, args.memory_config
    validate_memory_options(store, config, require_absolute=require_absolute)
    return memory_context_providers(store, config)


# -- operator commands -------------------------------------------------------------


def _memory_init(args: argparse.Namespace) -> dict[str, Any]:
    from watchlist.case_memory.store import MemoryStore

    store = MemoryStore.initialize(args.store, args.memory_config)
    sequence, head = store.head()
    return {
        "store": str(store.root),
        "memory_head": {"sequence": sequence, "sha256": head},
        "limitation": LIMITATION,
    }


def _memory_publish(args: argparse.Namespace) -> dict[str, Any]:
    from watchlist.case_memory.publisher import Publisher
    from watchlist.case_memory.store import MemoryStore
    from watchlist.review.service import ReviewService

    store = MemoryStore.open(args.store, args.memory_config)
    service = ReviewService.open(args.workspace, args.config, read_only=True)
    report = Publisher(store, service, args.workspace_id).publish()
    return {"publication": report.to_dict(), "limitation": LIMITATION}


# -- fictional walkthrough ---------------------------------------------------------


def _ranking(case: dict[str, Any]) -> list[tuple[int, str, float]]:
    """(rank, entity ID, score) of the frozen candidates, for comparison only."""
    return [
        (c["rank"], c["entity_id"], c["score"])
        for c in case["packet"]["retrieval"]["candidates"]
    ]


def _memory_data(case: dict[str, Any]) -> dict[str, Any] | None:
    entry = (case["packet"].get("context") or {}).get("case_memory")
    return None if entry is None else entry["data"]


def run_demo(
    output_root: Path,
    *,
    review_config: Path | None = None,
    memory_config: Path | None = None,
    name: str = DEMO_NAME,
    log: Any = None,
) -> dict[str, Any]:
    """Run the documented fictional walkthrough in a NEW directory.

    Steps: create a v2 workspace and a memory store; serve the API in process
    with the memory provider attached; start a case for ``name``; submit one
    fictional human-review annotation through the API; publish the completed
    case to memory; start a second case for the same name and show the earlier
    annotation as display-only context; check the ranking is unchanged and the
    audit chain verifies; read the case through the MCP tool layer when the
    ``mcp`` extra is installed. Returns a JSON-ready summary (also saved as
    ``demo-summary.json``). Requires the ``agent`` and ``api`` extras.
    """
    from fastapi.testclient import TestClient

    from watchlist.api import ApiSettings, create_app
    from watchlist.api.auth import ApiKey, hash_key
    from watchlist.case_memory.publisher import Publisher
    from watchlist.case_memory.store import MemoryStore
    from watchlist.review.service import ReviewService

    say = log or (lambda text: print(text, file=sys.stderr))
    review_config = review_config or _REPO_ROOT / "config" / "review-v2.json"
    memory_config = memory_config or _REPO_ROOT / "config" / "memory.json"
    root = Path(output_root).absolute()
    if root.exists() or root.is_symlink():
        raise ValidationError(
            "demo output root must not exist yet; choose a new directory",
            details={"output_root": str(root)},
        )
    root.mkdir(parents=True)
    workspace, memory_dir = root / "workspace", root / "memory"
    say(LIMITATION)
    ReviewService.initialize(workspace, review_config)
    MemoryStore.initialize(memory_dir, memory_config)
    say(f"Created fictional workspace {workspace} and memory store {memory_dir}.")

    # A key for this run only; it is never written to disk or printed.
    token = secrets.token_urlsafe(32)
    settings = ApiSettings(
        workspace=workspace,
        config_path=review_config,
        api_keys=(ApiKey("demo-reviewer", "reviewer", hash_key(token)),),
    )
    providers = memory_context_providers(memory_dir, memory_config)
    auth = {"Authorization": f"Bearer {token}"}
    summary: dict[str, Any] = {"limitation": LIMITATION, "name": name}
    with TestClient(create_app(settings, context_providers=providers)) as client:

        def call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
            response = client.request(method, path, headers=auth, **kwargs)
            if response.status_code != 200:
                raise ReviewError(
                    f"demo API call {method} {path} failed",
                    details={"status": response.status_code, "body": response.json()},
                )
            return response.json()

        first = call(
            "POST",
            "/v2/cases",
            json={"name": name, "origin": "simulated_demo", "request_id": str(uuid4())},
        )
        case1 = first["case"]
        say(
            f"Case 1 {case1['case_id']}: possible record matches for review "
            f"{_ranking(case1)}; memory status {_memory_data(case1)['status']}."
        )
        reviewed = call(
            "POST",
            f"/v2/cases/{case1['case_id']}/review",
            json={
                "submission_id": str(uuid4()),
                "revision": first["revision"],
                "packet_sha256": case1["packet_sha256"],
                "outcome": "candidate_for_follow_up",
                "candidate_entity_id": case1["packet"]["retrieval"]["candidates"][0][
                    "entity_id"
                ],
                "reason": "Fictional demo annotation: possible record match worth "
                "follow-up research; not an identity finding.",
            },
        )["case"]
        say(f"Case 1 reviewed by a fictional reviewer; status {reviewed['status']}.")

        store = MemoryStore.open(memory_dir, memory_config)
        reader = ReviewService.open(workspace, review_config, read_only=True)
        report = Publisher(store, reader, "demo-workspace").publish()
        say(f"Published completed cases to memory: {report.published} published.")

        second = call(
            "POST",
            "/v2/cases",
            json={"name": name, "origin": "simulated_demo", "request_id": str(uuid4())},
        )["case"]
        memory = _memory_data(second)
        cards = [
            {"entity_id": key["source_entity_id"], **card}
            for key in memory["keys"]
            for card in key["cards"]
        ]
        say(
            f"Case 2 {second['case_id']}: {len(cards)} earlier annotation(s) shown as "
            "display-only context (dated reviewer opinions, not identity facts)."
        )
        verification = call("GET", f"/v2/cases/{case1['case_id']}/audit/verify")[
            "verification"
        ]
    summary.update(
        {
            "case_1": {
                "case_id": case1["case_id"],
                "status": reviewed["status"],
                "ranking": _ranking(case1),
                "memory_status": _memory_data(case1)["status"],
            },
            "publication": report.to_dict(),
            "case_2": {
                "case_id": second["case_id"],
                "status": second["status"],
                "ranking": _ranking(second),
                "memory_status": memory["status"],
                "prior_annotations": [
                    {
                        k: card[k]
                        for k in (
                            "entity_id",
                            "case_id",
                            "outcome",
                            "relation",
                            "origin",
                        )
                    }
                    for card in cards
                ],
            },
            "ranking_unchanged": _ranking(case1) == _ranking(second),
            "case_1_audit_ok": verification["ok"],
            "mcp_get_case": _mcp_view(workspace, review_config, case1["case_id"], say),
        }
    )
    target = root / "demo-summary.json"
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, indent=2, sort_keys=True, ensure_ascii=True)
        stream.write("\n")
    say(f"Saved {target}.")
    return summary


def _mcp_view(
    workspace: Path, review_config: Path, case_id: str, say: Any
) -> dict[str, Any] | None:
    """Read the case through the MCP tool layer, or None without the ``mcp`` extra."""
    try:
        from watchlist.mcp_server import tools
        from watchlist.mcp_server.server import ServerSettings, open_runtime
    except ImportError:
        say("MCP extra not installed; skipped the MCP read.")
        return None
    runtime = open_runtime(
        ServerSettings(
            workspace=workspace,
            origin="simulated_demo",
            config=review_config,
            read_only=True,
        )
    )
    payload = tools.get_case(runtime, case_id)
    say(f"MCP get_case sees case 1 with status {payload['status']}.")
    return {
        "status": payload["status"],
        "packet_sha256": payload["packet_sha256"],
        "review_outcome": payload["review"]["outcome"] if payload["review"] else None,
    }


# -- command line ------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -B -m watchlist.stack",
        description="Research-only v2 stack wiring: reviewer-memory operator "
        "commands and the fictional demo.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "memory-init", help="create a new memory store", allow_abbrev=False
    )
    init.add_argument("--store", type=Path, required=True, metavar="PATH")
    init.add_argument("--memory-config", type=Path, required=True, metavar="PATH")
    publish = commands.add_parser(
        "memory-publish",
        help="publish completed, verified cases of one workspace into memory",
        allow_abbrev=False,
    )
    publish.add_argument("--store", type=Path, required=True, metavar="PATH")
    publish.add_argument("--memory-config", type=Path, required=True, metavar="PATH")
    publish.add_argument("--workspace", type=Path, required=True, metavar="PATH")
    publish.add_argument("--config", type=Path, required=True, metavar="PATH")
    publish.add_argument(
        "--workspace-id",
        required=True,
        metavar="LABEL",
        help="stable operator-chosen label for this workspace",
    )
    demo = commands.add_parser(
        "demo", help="run the fictional end-to-end walkthrough", allow_abbrev=False
    )
    demo.add_argument(
        "--output-root",
        type=Path,
        metavar="NEW_DIR",
        help="new directory (default: data/demo/run-<UTC timestamp>)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one command; structured errors go to stderr as JSON with exit 1."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "memory-init":
            result = _memory_init(args)
        elif args.command == "memory-publish":
            result = _memory_publish(args)
        else:
            root = args.output_root or DEFAULT_DEMO_ROOT / datetime.now(
                timezone.utc
            ).strftime("run-%Y%m%dT%H%M%SZ")
            result = run_demo(root)
    except ReviewError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
