"""Bearer API-key authentication for the research API.

Keys are configured as ``actor:role:sha256hex`` entries: the server stores only
the SHA-256 of each key, never the key itself. A presented bearer token is
hashed and compared with **every** configured hash using
``hmac.compare_digest`` (no early exit), so timing does not reveal which entry
matched or how much of a hash agreed.

The ``actor`` label is the identity the API passes to the review service as
the case creator, reviewer or canceller. It is attribution derived from a key
the operator issued, not proof of who is typing: anyone holding the key acts as
that label. Request bodies can never set or override it.

Roles:

* ``viewer``: read cases, audits, audit verification and reference records.
* ``reviewer``: everything a viewer can do, plus start, review, recover and
  cancel cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import re
import secrets
from typing import Iterable, Literal

__all__ = [
    "ROLES",
    "ApiKey",
    "KeyStore",
    "Principal",
    "AuthConfigError",
    "hash_key",
    "new_key",
    "parse_key_entries",
    "parse_key_entry",
]

ROLES = ("viewer", "reviewer")
Role = Literal["viewer", "reviewer"]

# Actor labels are short and plain so they display safely everywhere and pass
# the review service's visible-text rule (no controls or format characters).
_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Longest bearer token we hash; longer headers are rejected without hashing.
MAX_TOKEN_CHARS = 512
MIN_TOKEN_CHARS = 16


class AuthConfigError(ValueError):
    """An API-key entry or key file is malformed; the message never echoes a hash."""


@dataclass(frozen=True)
class ApiKey:
    """One configured key: actor label, role and the key's SHA-256 (hidden in repr)."""

    actor: str
    role: Role
    sha256: str = field(repr=False)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: an issued actor label and its role."""

    actor: str
    role: Role

    def can(self, required: Role) -> bool:
        """Reviewer includes every viewer permission."""
        return required == "viewer" or self.role == "reviewer"


def hash_key(token: str) -> str:
    """SHA-256 hex digest of a key's UTF-8 bytes (what the server stores)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_key(actor: str, role: str) -> tuple[str, str]:
    """Generate a random key; return ``(token, 'actor:role:sha256')``.

    The token is shown once to the operator; only the entry is configured.
    """
    entry = parse_key_entry(f"{actor}:{role}:{'0' * 64}")
    token = secrets.token_urlsafe(32)
    return token, f"{entry.actor}:{entry.role}:{hash_key(token)}"


def parse_key_entry(text: str) -> ApiKey:
    """Parse ``actor:role:sha256hex``; raise ``AuthConfigError`` without echoing it."""
    parts = text.strip().split(":")
    if len(parts) != 3:
        raise AuthConfigError("API key entry must be actor:role:sha256hex")
    actor, role, digest = parts
    if not _ACTOR.fullmatch(actor):
        raise AuthConfigError(
            "API key actor must match [A-Za-z0-9][A-Za-z0-9_.@-]{0,63}"
        )
    if role not in ROLES:
        raise AuthConfigError(f"API key role must be one of: {', '.join(ROLES)}")
    if not _SHA256.fullmatch(digest):
        raise AuthConfigError(
            f"API key hash for actor {actor} must be 64 lowercase hex characters"
        )
    return ApiKey(actor, role, digest)  # type: ignore[arg-type]


def parse_key_entries(lines: Iterable[str]) -> tuple[ApiKey, ...]:
    """Parse entries, skipping blank lines and ``#`` comments; hashes must be unique."""
    keys: list[ApiKey] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key = parse_key_entry(stripped)
        if key.sha256 in seen:
            raise AuthConfigError(f"duplicate API key hash (actor {key.actor})")
        seen.add(key.sha256)
        keys.append(key)
    return tuple(keys)


class KeyStore:
    """Authenticate bearer tokens against configured hashes in constant time."""

    def __init__(self, keys: Iterable[ApiKey]):
        self._keys = tuple(keys)
        if len({key.sha256 for key in self._keys}) != len(self._keys):
            raise AuthConfigError("duplicate API key hash")

    def __len__(self) -> int:
        return len(self._keys)

    def authenticate(self, token: str | None) -> Principal | None:
        """Return the principal for a valid token, else ``None``.

        Every configured hash is compared, whether or not an earlier one
        matched. Tokens outside the accepted length range are refused before
        hashing; that leaks only the (public) length bounds.
        """
        if (
            token is None
            or not MIN_TOKEN_CHARS <= len(token) <= MAX_TOKEN_CHARS
            or not token.isascii()
        ):
            return None
        presented = hash_key(token).encode("ascii")
        found: ApiKey | None = None
        for key in self._keys:
            if hmac.compare_digest(presented, key.sha256.encode("ascii")):
                found = key
        return None if found is None else Principal(found.actor, found.role)
