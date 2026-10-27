"""Exact, registered reference-record identifiers for reviewer memory.

A memory link exists only when three structured values are **equal**:

* a registered ``namespace_id`` (a source lineage, e.g. the fictional fixture);
* the exact ``source_entity_id`` taken from candidate evidence;
* the exact ``snapshot_sha256`` of the reference data the retriever used.

``record_key = SHA-256(canonical_json(["case-memory-record", namespace, id]))``
is a compact deterministic key, not secrecy and not identity proof. Names,
alias text, query text and name hashes never create or select a key, so two
records called "Luma Vesper" (``syn-001`` and ``syn-012``) stay separate, and
the same ID reused in another namespace or snapshot never inherits history.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Sequence

from watchlist.case_memory.state import (
    MemoryInputError,
    NamespaceConfig,
    SnapshotConfig,
    UnregisteredIdentifierError,
)
from watchlist.review.contracts import digest

# Identifiers are opaque tokens: no whitespace, so a free-text name such as
# "Luma Vesper" can never be passed where a record ID is required.
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def validate_identifier(value: object, label: str) -> str:
    """Require an opaque bounded ASCII token (no whitespace or name-like text)."""
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise MemoryInputError(
            f"{label} must be an exact identifier token "
            "([A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}); names are never identifiers"
        )
    return value


def validate_snapshot(value: object, label: str = "snapshot_sha256") -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise MemoryInputError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def record_key(namespace_id: str, source_entity_id: str) -> str:
    """Deterministic key for one namespaced source record ID."""
    return digest(
        [
            "case-memory-record",
            validate_identifier(namespace_id, "namespace_id"),
            validate_identifier(source_entity_id, "source_entity_id"),
        ]
    )


@dataclass(frozen=True)
class RecordRef:
    """One exact lookup key: namespace + source entity ID + snapshot fingerprint."""

    namespace_id: str
    source_entity_id: str
    snapshot_sha256: str

    def __post_init__(self) -> None:
        validate_identifier(self.namespace_id, "namespace_id")
        validate_identifier(self.source_entity_id, "source_entity_id")
        validate_snapshot(self.snapshot_sha256)

    @property
    def record_key(self) -> str:
        return record_key(self.namespace_id, self.source_entity_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "namespace_id": self.namespace_id,
            "source_entity_id": self.source_entity_id,
            "snapshot_sha256": self.snapshot_sha256,
            "record_key": self.record_key,
        }


class Registry:
    """Human-maintained allowlist of namespaces and snapshot fingerprints.

    Registration rejects ambiguity: a snapshot belongs to exactly one namespace
    and an evidence ``source`` label to exactly one namespace. A snapshot that
    is not registered yields no lookup key (reported as ``unregistered``,
    never as an empty history).
    """

    def __init__(
        self,
        namespaces: Iterable[NamespaceConfig],
        snapshots: Iterable[SnapshotConfig],
    ):
        self._namespaces: dict[str, NamespaceConfig] = {}
        self._source_owner: dict[str, str] = {}
        for namespace in namespaces:
            if namespace.namespace_id in self._namespaces:
                raise MemoryInputError(
                    "namespace registered twice",
                    details={"namespace_id": namespace.namespace_id},
                )
            self._namespaces[namespace.namespace_id] = namespace
            for source in namespace.sources:
                owner = self._source_owner.setdefault(source, namespace.namespace_id)
                if owner != namespace.namespace_id:
                    raise MemoryInputError(
                        "evidence source label is registered to two namespaces",
                        details={"source": source},
                    )
        self._snapshots: dict[str, SnapshotConfig] = {}
        for snapshot in snapshots:
            if snapshot.namespace_id not in self._namespaces:
                raise MemoryInputError(
                    "snapshot refers to an unregistered namespace",
                    details={"namespace_id": snapshot.namespace_id},
                )
            if snapshot.snapshot_sha256 in self._snapshots:
                raise MemoryInputError(
                    "snapshot fingerprint registered twice",
                    details={"snapshot_sha256": snapshot.snapshot_sha256},
                )
            self._snapshots[snapshot.snapshot_sha256] = snapshot

    @property
    def namespaces(self) -> tuple[NamespaceConfig, ...]:
        return tuple(self._namespaces.values())

    @property
    def snapshots(self) -> tuple[SnapshotConfig, ...]:
        return tuple(self._snapshots.values())

    def namespace_for_snapshot(self, snapshot_sha256: str) -> NamespaceConfig | None:
        snapshot = self._snapshots.get(validate_snapshot(snapshot_sha256))
        return None if snapshot is None else self._namespaces[snapshot.namespace_id]

    def require_namespace(self, snapshot_sha256: str) -> NamespaceConfig:
        namespace = self.namespace_for_snapshot(snapshot_sha256)
        if namespace is None:
            raise UnregisteredIdentifierError(
                "reference snapshot is not registered for memory",
                details={"snapshot_sha256": snapshot_sha256},
            )
        return namespace

    def refs(
        self, snapshot_sha256: str, source_entity_ids: Sequence[str]
    ) -> tuple[RecordRef, ...]:
        """Exact lookup keys for candidate IDs in a registered snapshot."""
        namespace = self.require_namespace(snapshot_sha256)
        return tuple(
            RecordRef(namespace.namespace_id, entity_id, snapshot_sha256)
            for entity_id in source_entity_ids
        )

    def check_source(self, namespace: NamespaceConfig, source: str) -> None:
        """Candidate evidence must come from a source label of the snapshot's namespace."""
        if source not in namespace.sources:
            raise UnregisteredIdentifierError(
                "evidence source does not belong to the registered namespace",
                details={"namespace_id": namespace.namespace_id, "source": source},
            )
