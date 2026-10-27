"""Access policy of one local MCP server process (stdio transport).

What this is: the fixed, operator-chosen rules that bound what an MCP client
may do through this server. Why: an MCP client is usually an AI assistant
acting on text it was given, so it must not choose the case origin, claim a
human reviewer identity, or read case partitions it was not given.

Over stdio there is no network listener and no bearer token. The trust boundary
is the local operating-system user who launched the process; whoever can
start the process can already read the workspace files. This module therefore
does **not** authenticate anyone. It fixes three things at launch time:

* ``origin``: every case ``screen_name`` creates gets this origin. ``human`` is
  refused, so a tool call can never produce a case that later counts as
  human-originated review evidence.
* ``actor``: the attribution label written into created cases is always
  ``mcp:<label>``; the client cannot supply or change it. It is attribution,
  not authentication (see ``watchlist.review.contracts.LIMITATION``).
* ``read_scope``: by default the client may read only cases of the server's
  own origin; other cases are reported as not found so their existence and
  query names do not leak.

Authenticated streamable HTTP (needed for the Microsoft 365 requirement) is not
implemented; it depends on the shared Step 24 API authentication contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from watchlist.review.contracts import ValidationError, WorkflowPolicy
from watchlist.review.validation import validate_actor, validate_origin

MCP_ORIGINS = ("test", "simulated_demo", "unknown")
"""Origins an MCP server may stamp on new cases. ``human`` is deliberately absent."""

READ_SCOPES = ("own_origin", "all_origins")
ACTOR_PREFIX = "mcp:"
DEFAULT_ACTOR_LABEL = "local-stdio-client"
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

ReadScope = Literal["own_origin", "all_origins"]


@dataclass(frozen=True)
class AccessPolicy:
    """Immutable per-process rules applied before any review-service call."""

    origin: str
    actor: str
    read_only: bool
    read_scope: str

    def can_read(self, origin: str) -> bool:
        """Whether a case with ``origin`` is visible to this server's clients."""
        return self.read_scope == "all_origins" or origin == self.origin

    def to_dict(self) -> dict[str, object]:
        return {
            "origin": self.origin,
            "actor": self.actor,
            "read_only": self.read_only,
            "read_scope": self.read_scope,
            "transport": "stdio",
            "authentication": "none (local process boundary only)",
        }


def build_policy(
    *,
    origin: str,
    actor_label: str = DEFAULT_ACTOR_LABEL,
    read_only: bool = False,
    read_scope: str = "own_origin",
    workflow: WorkflowPolicy,
) -> AccessPolicy:
    """Validate launch options into an ``AccessPolicy``.

    Raises ``ValidationError`` (code ``invalid_input``) for a ``human`` or
    unknown origin, an origin the review config does not allow, a label that is
    not a short ASCII identifier, or an unknown read scope.
    """
    if origin not in MCP_ORIGINS:
        raise ValidationError(
            f"MCP origin must be one of {', '.join(MCP_ORIGINS)}; human-origin cases "
            "need an authenticated reviewer interface",
            details={"origin": str(origin)[:40]},
        )
    origin = validate_origin(origin, workflow)
    if type(actor_label) is not str or not _LABEL.fullmatch(actor_label):
        raise ValidationError("actor label must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    actor = validate_actor(ACTOR_PREFIX + actor_label, workflow)
    if read_scope not in READ_SCOPES:
        raise ValidationError(f"read scope must be one of {', '.join(READ_SCOPES)}")
    return AccessPolicy(origin, actor, bool(read_only), read_scope)
