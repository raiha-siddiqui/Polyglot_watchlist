"""Verify or inspect archived review evidence without running its workflow.

The separate ``v2`` command group drives the versioned v2 review service in an
explicit v2 workspace. Archive commands never import or run the v2 workflow.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys

# Resolved from this source file, not the current directory, so commands work
# from anywhere in a source checkout. An installed copy without the repository
# config gets a clear "cannot read review config" error; pass --config then.
DEFAULT_V2_CONFIG = Path(__file__).resolve().parents[3] / "config" / "review-v2.json"


def _parser() -> argparse.ArgumentParser:
    """Define only the approved archive operations and their required inputs."""
    parser = argparse.ArgumentParser(
        prog="python -B -m watchlist.review",
        description=__doc__,
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    archive = commands.add_parser(
        "archive",
        help="read preserved evidence without permitting execution",
        allow_abbrev=False,
    )
    actions = archive.add_subparsers(dest="archive_action", required=True)
    verify = actions.add_parser(
        "verify",
        help="verify an archive and list its case identifiers",
        allow_abbrev=False,
    )
    verify.add_argument("--archive", type=Path, required=True, metavar="PATH")
    inspect = actions.add_parser(
        "inspect",
        help="verify an archive and display one saved case",
        allow_abbrev=False,
    )
    inspect.add_argument("--archive", type=Path, required=True, metavar="PATH")
    inspect.add_argument("--case-id", required=True, metavar="AUDIT_ID")
    _add_v2(commands)
    return parser


def _add_v2(commands) -> None:
    """The v2 service group: explicit init, writes and read-only inspection."""
    group = commands.add_parser(
        "v2",
        help="versioned v2 review service (separate v2 workspace)",
        allow_abbrev=False,
    )
    actions = group.add_subparsers(dest="v2_action", required=True)

    def action(
        name: str, help_text: str, *, case: bool = False
    ) -> argparse.ArgumentParser:
        sub = actions.add_parser(name, help=help_text, allow_abbrev=False)
        sub.add_argument("--workspace", type=Path, required=True, metavar="PATH")
        sub.add_argument(
            "--config",
            type=Path,
            default=DEFAULT_V2_CONFIG,
            metavar="PATH",
            help="review config (default: the repository config/review-v2.json)",
        )
        if case:
            sub.add_argument("--case-id", required=True, metavar="CASE_ID")
        return sub

    action("init", "create a new v2 workspace in a missing or empty directory")
    start = action("start", "create a case and stop at the human-review interrupt")
    start.add_argument("--name", required=True)
    start.add_argument(
        "--origin",
        required=True,
        choices=("human", "simulated_demo", "test", "unknown"),
    )
    start.add_argument(
        "--actor", required=True, help="attribution label; not authentication"
    )
    start.add_argument(
        "--request-id", metavar="UUID", help="client UUID that makes a retry idempotent"
    )
    action("status", "read one case (read-only)", case=True)
    listing = action("list", "list case summaries (read-only)")
    listing.add_argument(
        "--limit", type=int, default=None, help="default: min(50, page_max)"
    )
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument(
        "--origin", choices=("human", "simulated_demo", "test", "unknown")
    )
    review = action("review", "submit one review form from a JSON file", case=True)
    review.add_argument("--submission", type=Path, required=True, metavar="FILE")
    action("recover", "finish interrupted work from durable state", case=True)
    cancel = action("cancel", "cancel a case before any review is accepted", case=True)
    cancel.add_argument("--actor", required=True)
    cancel.add_argument("--reason", required=True)
    action("audit", "show the case audit events (read-only)", case=True)
    verify = action(
        "verify", "verify the audit chain and records (read-only)", case=True
    )
    verify.add_argument(
        "--anchor", type=Path, metavar="FILE", help="JSON anchor kept elsewhere"
    )


def _error(message: str) -> int:
    """Keep expected failures on stderr, with terminal controls escaped."""
    print(json.dumps({"error": message}, ensure_ascii=True), file=sys.stderr)
    return 1


def _print(result: object) -> int:
    print(
        json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)
    )
    return 0


def _read_json(path: Path) -> object:
    from watchlist.review.contracts import ValidationError, loads_strict

    try:
        return loads_strict(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValidationError(
            f"cannot read {path.name}: {exc.strerror or exc}"
        ) from None


def _run_v2(args: argparse.Namespace) -> int:
    """Dispatch one v2 command; domain errors become JSON on stderr, exit 1."""
    from watchlist.review.contracts import ReviewError
    from watchlist.review.service import ReviewService

    try:
        if args.v2_action == "init":
            service = ReviewService.initialize(args.workspace, args.config)
            return _print(
                {
                    "status": "initialized",
                    "workspace": str(service.workspace),
                    "workflow": service.workflow_identity.to_dict(),
                }
            )
        read_only = args.v2_action in ("status", "list", "audit", "verify")
        service = ReviewService.open(args.workspace, args.config, read_only=read_only)
        if args.v2_action == "start":
            result = service.start_case(
                args.name,
                origin=args.origin,
                actor=args.actor,
                request_id=args.request_id,
            ).to_dict()
        elif args.v2_action == "status":
            result = service.get_case(args.case_id).to_dict()
        elif args.v2_action == "list":
            cases = service.list_cases(args.limit, args.offset, origin=args.origin)
            result = {"cases": [item.to_dict() for item in cases]}
        elif args.v2_action == "review":
            result = service.submit_review(
                args.case_id, _read_json(args.submission)
            ).to_dict()
        elif args.v2_action == "recover":
            result = service.recover(args.case_id).to_dict()
        elif args.v2_action == "cancel":
            result = service.cancel_case(
                args.case_id, actor=args.actor, reason=args.reason
            ).to_dict()
        elif args.v2_action == "audit":
            result = service.audit(args.case_id).to_dict()
        else:
            anchor = _read_json(args.anchor) if args.anchor else None
            result = service.verify_audit(args.case_id, anchor).to_dict()
            if not result["ok"]:
                print(json.dumps(result, indent=2, ensure_ascii=True))
                return 1
    except ReviewError as exc:
        print(
            json.dumps(exc.to_dict(), sort_keys=True, ensure_ascii=True),
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        return _error(f"{type(exc).__name__}: {exc.strerror or exc}")
    return _print(result)


def main(argv: Sequence[str] | None = None) -> int:
    """Print a finite JSON result; use exit 1 for expected archive failures.

    Argument parsing happens before importing the reader. Argparse retains its
    standard exit 2 for invalid syntax and exit 0 for help. Neither path opens
    an archive. Reader failures do not print a partial success result.
    """
    args = _parser().parse_args(argv)
    if args.command == "v2":
        return _run_v2(args)

    from .archive import inspect_case, verify_archive
    from .compat_v1 import ArchiveError

    try:
        if args.archive_action == "verify":
            result = verify_archive(args.archive)
        else:
            result = inspect_case(args.archive, args.case_id)
    except (ArchiveError, OSError) as exc:
        return _error(str(exc))

    if not isinstance(result, dict):
        return _error("archive reader did not return a JSON object")
    try:
        output = json.dumps(
            result, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError):
        return _error(
            "archive reader returned a value outside the finite JSON contract"
        )
    print(output)
    return 0
