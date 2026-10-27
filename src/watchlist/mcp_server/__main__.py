"""Command line launcher: ``python -B -m watchlist.mcp_server --workspace PATH``.

Stdout carries the MCP protocol stream, so this module writes diagnostics to
stderr only. Exit status 2 means invalid options or an unusable workspace.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
from pathlib import Path
import sys


def _parser() -> argparse.ArgumentParser:
    from watchlist.mcp_server import DEFAULT_CONFIG
    from watchlist.mcp_server.auth import DEFAULT_ACTOR_LABEL, MCP_ORIGINS, READ_SCOPES

    parser = argparse.ArgumentParser(
        prog="python -B -m watchlist.mcp_server",
        description=(
            "Research-only MCP server over the v2 review service (fictional data). "
            "It suggests possible record matches for human review and cannot "
            "submit review decisions."
        ),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--transport",
        choices=("stdio",),
        default="stdio",
        help="only stdio is implemented; authenticated HTTP is not (see docs/mcp.md)",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        metavar="PATH",
        help="absolute path of the v2 workspace directory",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        metavar="PATH",
        help="absolute path of the review config (default: the repository "
        "config/review-v2.json)",
    )
    parser.add_argument(
        "--origin",
        choices=MCP_ORIGINS,
        required=True,
        help="origin stamped on every case this server creates ('human' is refused)",
    )
    parser.add_argument(
        "--actor-label",
        default=DEFAULT_ACTOR_LABEL,
        metavar="LABEL",
        help="attribution label; cases record 'mcp:LABEL' (not authentication)",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="open the workspace read-only and do not offer screen_name",
    )
    parser.add_argument(
        "--read-scope",
        choices=READ_SCOPES,
        default="own_origin",
        help="which case origins clients may read (default: own_origin)",
    )
    parser.add_argument(
        "--init-workspace",
        action="store_true",
        help="create the v2 workspace if the directory is missing or empty",
    )
    from watchlist.stack import add_memory_arguments

    add_memory_arguments(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse options, open the workspace, then serve MCP over stdio until EOF."""
    args = _parser().parse_args(argv)
    from watchlist.mcp_server.server import ServerSettings, build_server, open_runtime
    from watchlist.review.contracts import ReviewError, ValidationError

    # An MCP client chooses the server's working directory, so a relative path
    # would silently point at a different workspace per client. Refuse it.
    relative = [
        option
        for option, path in (("--workspace", args.workspace), ("--config", args.config))
        if not path.is_absolute()
    ]
    if relative:
        error = ValidationError(
            "paths must be absolute because the MCP client chooses the working "
            "directory",
            details={"options": relative},
        )
        print(json.dumps(error.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    from watchlist.stack import validate_memory_options

    try:
        # Both or neither, absolute; open_runtime opens the store itself.
        validate_memory_options(
            args.memory_store, args.memory_config, require_absolute=True
        )
    except ReviewError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    settings = ServerSettings(
        workspace=args.workspace,
        origin=args.origin,
        config=args.config,
        actor_label=args.actor_label,
        read_only=args.read_only,
        read_scope=args.read_scope,
        init_workspace=args.init_workspace,
        memory_store=args.memory_store,
        memory_config=args.memory_config,
    )
    try:
        runtime = open_runtime(settings)
    except ReviewError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    build_server(runtime).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
