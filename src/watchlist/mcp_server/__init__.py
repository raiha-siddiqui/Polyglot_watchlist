"""Local MCP server exposing bounded, research-only review capabilities.

The server is a thin interface over ``watchlist.review.service.ReviewService``
(the one v2 review backend). It offers ``screen_name``, ``get_case``,
``list_cases``, ``get_entity``, ``get_audit`` and ``verify_audit`` over stdio,
and deliberately no tool that submits a human review decision. See
``docs/mcp.md``. Importing this package does not import the MCP SDK; use
``watchlist.mcp_server.server`` for that.
"""

from pathlib import Path

__all__ = ["DEFAULT_CONFIG", "main"]

# Resolved from this source file, as in the review CLI, so a source checkout
# works from any directory. An installed copy without the repository config
# must pass ``--config``.
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "review-v2.json"


def main(argv=None) -> int:
    """Console entry point; see ``watchlist.mcp_server.__main__``."""
    from watchlist.mcp_server.__main__ import main as run

    return run(argv)
