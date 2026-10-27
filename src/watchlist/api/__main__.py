"""Command line for the research API.

::

    python -B -m watchlist.api serve --workspace PATH --config PATH \
        [--host 127.0.0.1] [--port 8000] \
        [--memory-store PATH --memory-config PATH]
    python -B -m watchlist.api openapi > docs/openapi.json
    python -B -m watchlist.api new-key --actor analyst-1 --role reviewer
    python -B -m watchlist.api hash-key < key.txt

``serve`` binds loopback addresses only: external hosting needs TLS,
deployment review and Step 28 approval first. Keys come from
``WATCHLIST_API_KEYS`` or ``WATCHLIST_API_KEYS_FILE`` (see ``settings``).
Reviewer memory is opt-in: with ``--memory-store``/``--memory-config`` the
service is opened with the read-only memory context provider
(``watchlist.stack``); without them no memory context is attached.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import ipaddress
import json
from pathlib import Path
import sys

LOOPBACK_NAMES = frozenset({"localhost"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -B -m watchlist.api",
        description="Research-only review API (fictional data; human review only).",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve", help="serve the API on a loopback address", allow_abbrev=False
    )
    serve.add_argument("--workspace", type=Path, required=True, metavar="PATH")
    # Required: no default path is valid both in a source checkout and in an
    # installed package (e.g. config/review-v2.json in the checkout).
    serve.add_argument("--config", type=Path, required=True, metavar="PATH")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    from watchlist.stack import add_memory_arguments

    add_memory_arguments(serve)
    commands.add_parser(
        "openapi", help="print the OpenAPI document as JSON", allow_abbrev=False
    )
    key = commands.add_parser(
        "new-key",
        help="generate a key; prints the token once and its configuration entry",
        allow_abbrev=False,
    )
    key.add_argument("--actor", required=True)
    key.add_argument("--role", required=True, choices=("viewer", "reviewer"))
    commands.add_parser(
        "hash-key",
        help="read one key from stdin and print its SHA-256 for an entry",
        allow_abbrev=False,
    )
    return parser


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IP literals only."""
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _fail(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=True), file=sys.stderr)
    return 1


def _openapi_text() -> str:
    from watchlist.api.app import openapi_document

    return (
        json.dumps(openapi_document(), indent=2, sort_keys=True, ensure_ascii=True)
        + "\n"
    )


def _serve(args: argparse.Namespace) -> int:
    from watchlist.api.app import create_app
    from watchlist.api.settings import ApiSettings, SettingsError
    from watchlist.review.contracts import ReviewError
    from watchlist.stack import memory_providers_from_args

    if not is_loopback(args.host):
        return _fail(
            "only loopback hosts are allowed; external hosting is not approved"
        )
    if not 1 <= args.port <= 65535:
        return _fail("port must be from 1 to 65535")
    try:
        settings = ApiSettings.from_env(
            workspace=args.workspace, config_path=args.config
        )
        app = create_app(settings, context_providers=memory_providers_from_args(args))
    except SettingsError as exc:
        return _fail(str(exc))
    except ReviewError as exc:
        return _fail(f"{exc.code}: {exc.message}")
    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        server_header=False,
        date_header=True,
        proxy_headers=False,
        log_level="info",
        access_log=False,
        timeout_keep_alive=5,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "openapi":
        sys.stdout.write(_openapi_text())
        return 0
    if args.command == "new-key":
        from watchlist.api.auth import AuthConfigError, new_key

        try:
            token, entry = new_key(args.actor, args.role)
        except AuthConfigError as exc:
            return _fail(str(exc))
        print(json.dumps({"entry": entry, "token": token}, ensure_ascii=True))
        return 0
    if args.command == "hash-key":
        from watchlist.api.auth import MIN_TOKEN_CHARS, hash_key

        token = sys.stdin.readline().strip()
        if len(token) < MIN_TOKEN_CHARS:
            return _fail(f"key must have at least {MIN_TOKEN_CHARS} characters")
        print(hash_key(token))
        return 0
    return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
