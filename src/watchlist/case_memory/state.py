"""Memory configuration, structured errors and frozen contracts (Steps 20-21).

Reviewer memory stores **dated prior annotations** about exact reference-record
IDs: which earlier case displayed a record, what outcome a reviewer recorded,
when, under which workflow version and with which origin. A card is context
for a human reviewer. It is never an identity fact, a finding of wrongdoing, a
sanctions status or a clearance, and it never changes candidate ranking.

This module is standard-library only (plus the v2 review contracts). Every
error is a ``ReviewError`` subclass with a stable ``code`` so that a failure
inside the context provider surfaces as ``context_provider_failed`` with the
memory code in ``details['cause']``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from watchlist.review.contracts import (
    ORIGINS,
    OUTCOMES,
    ReviewError,
    _Contract,
    _object,
    boolean,
    choice,
    digest,
    integer,
    loads_strict,
    number,
    sha256_text,
    text,
    uuid_text,
)

MEMORY_CONFIG_VERSION = "case-memory-config-v1"
MEMORY_SCHEMA = "case-memory-store-v1"
ANNOTATION_SCHEMA = "case-memory-annotation-v1"
CONTEXT_SCHEMA = "case-memory-context-v1"
PROVIDER_NAME = "case_memory"
PROVIDER_VERSION = "case-memory-context-v1"

RELATIONS = (
    "selected_for_follow_up",
    "considered_without_selection",
    "unresolved_context",
)
CORRECTION_KINDS = ("withdrawal", "supersession")

MEMORY_NOTICE = (
    "Earlier dated annotations about the same exact reference-record ID in the same "
    "registered source snapshot. They are context for human review only: an earlier "
    "annotation is a reviewer opinion about an earlier query, not an identity fact, "
    "not evidence of wrongdoing or sanctions status, and not a clearance. Records are "
    "linked by exact ID only, never by name similarity. Memory does not change "
    "candidates, ranking or scores."
)
CONTINUITY_NOTICE = (
    "Identifier continuity across snapshots is not established; this history is shown "
    "for explicit inspection only and never enters the default review panel."
)


# ---------------------------------------------------------------------------
# Structured errors (stable codes).
# ---------------------------------------------------------------------------


class CaseMemoryError(ReviewError):
    """Base class for case-memory failures."""

    code = "memory_error"


class MemoryInputError(CaseMemoryError):
    """Caller input or configuration has the wrong shape or value."""

    code = "memory_invalid_input"


class MemoryUnavailableError(CaseMemoryError):
    """The memory store is missing, busy or unreadable; never an empty history."""

    code = "memory_unavailable"
    retryable = True


class MemoryIntegrityError(CaseMemoryError):
    """Saved memory rows, hashes or the event chain disagree."""

    code = "memory_inconsistent"


class MemoryVersionError(CaseMemoryError):
    """The store or config declares an unsupported version."""

    code = "memory_incompatible_version"


class UnregisteredIdentifierError(CaseMemoryError):
    """A namespace, source or snapshot is not in the registered allowlist."""

    code = "memory_unregistered_identifier"


class PublicationConflictError(CaseMemoryError):
    """The same source case was published before with different content."""

    code = "publication_conflict"


class CorrectionError(CaseMemoryError):
    """A withdrawal or supersession is invalid or conflicts with an earlier one."""

    code = "correction_invalid"


class RetentionError(CaseMemoryError):
    """A retention plan is stale, invalid or cannot be applied."""

    code = "retention_invalid"


class NotFoundMemoryError(CaseMemoryError):
    """The requested annotation does not exist (or was deleted by retention)."""

    code = "memory_not_found"


# ---------------------------------------------------------------------------
# Time helpers. All stored times are timezone-aware ISO-8601 UTC strings.
# ---------------------------------------------------------------------------


def parse_time(value: object, label: str) -> datetime:
    """Parse an aware ISO-8601 timestamp; naive times are rejected, never guessed."""
    if type(value) is not str:
        raise MemoryInputError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise MemoryInputError(f"{label} must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise MemoryInputError(f"{label} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def utc_now() -> str:
    return iso(datetime.now(timezone.utc))


def days(value: int) -> timedelta:
    return timedelta(days=value)


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------


def _id_text(value: object, label: str) -> str:
    from watchlist.case_memory.identifiers import validate_identifier

    return validate_identifier(value, label)


@dataclass(frozen=True)
class NamespaceConfig(_Contract):
    """One registered source lineage; ``sources`` holds its evidence ``source`` label.

    Exactly one label is allowed: ``record_key`` does not include the source,
    so two sources sharing a namespace could reuse an entity ID and collide.
    Each source needs its own namespace.
    """

    namespace_id: str
    sources: tuple[str, ...]
    fictional: bool
    description: str

    @classmethod
    def from_dict(cls, data: object) -> NamespaceConfig:
        data = _object(data, "namespace", tuple(cls.__dataclass_fields__))
        sources = data["sources"]
        if not isinstance(sources, list) or len(sources) != 1:
            raise MemoryInputError(
                "namespace.sources must list exactly one evidence source label; "
                "register each source as its own namespace"
            )
        return cls(
            _id_text(data["namespace_id"], "namespace.namespace_id"),
            tuple(_id_text(s, "namespace.sources") for s in sources),
            boolean(data["fictional"], "namespace.fictional"),
            text(data["description"], "namespace.description", limit=500),
        )


@dataclass(frozen=True)
class SnapshotConfig(_Contract):
    """A registered reference snapshot fingerprint and the namespace it belongs to."""

    snapshot_sha256: str
    namespace_id: str
    label: str

    @classmethod
    def from_dict(cls, data: object) -> SnapshotConfig:
        data = _object(data, "snapshot", tuple(cls.__dataclass_fields__))
        return cls(
            sha256_text(data["snapshot_sha256"], "snapshot.snapshot_sha256"),
            _id_text(data["namespace_id"], "snapshot.namespace_id"),
            text(data["label"], "snapshot.label", limit=500),
        )


@dataclass(frozen=True)
class MemoryPolicy(_Contract):
    """Display policy; its digest is frozen into every memory context.

    ``visibility_days`` is a pilot freshness window (suppression from the
    default panel), not deletion and not a legal retention period.
    ``display_origins`` maps the new case's origin to the annotation origins it
    may display, so demo or test history never appears in a human case.
    """

    cards_per_candidate: int
    visibility_days: int
    page_max: int
    display_origins: dict[str, tuple[str, ...]]

    @classmethod
    def from_dict(cls, data: object) -> MemoryPolicy:
        data = _object(data, "policy", tuple(cls.__dataclass_fields__))
        raw = data["display_origins"]
        if not isinstance(raw, Mapping) or set(raw) != set(ORIGINS):
            raise MemoryInputError(
                "policy.display_origins must map every case origin",
                details={"origins": list(ORIGINS)},
            )
        mapping: dict[str, tuple[str, ...]] = {}
        for origin in ORIGINS:
            values = raw[origin]
            if not isinstance(values, list) or any(v not in ORIGINS for v in values):
                raise MemoryInputError(
                    f"policy.display_origins.{origin} must list known origins"
                )
            if "unknown" in values:
                raise MemoryInputError(
                    "unknown-origin annotations are never displayed automatically"
                )
            mapping[origin] = tuple(o for o in ORIGINS if o in values)
        return cls(
            integer(
                data["cards_per_candidate"],
                "policy.cards_per_candidate",
                minimum=1,
                maximum=10,
            ),
            integer(
                data["visibility_days"],
                "policy.visibility_days",
                minimum=1,
                maximum=3650,
            ),
            integer(data["page_max"], "policy.page_max", minimum=1, maximum=500),
            mapping,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cards_per_candidate": self.cards_per_candidate,
            "visibility_days": self.visibility_days,
            "page_max": self.page_max,
            "display_origins": {k: list(v) for k, v in self.display_origins.items()},
        }

    @property
    def sha256(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True)
class RetentionPolicy(_Contract):
    """How long memory annotations and backup sets are kept before deletion drills."""

    annotation_retention_days: int
    backup_retention_days: int

    @classmethod
    def from_dict(cls, data: object) -> RetentionPolicy:
        data = _object(data, "retention", tuple(cls.__dataclass_fields__))
        return cls(
            integer(
                data["annotation_retention_days"],
                "retention.annotation_retention_days",
                minimum=1,
                maximum=36500,
            ),
            integer(
                data["backup_retention_days"],
                "retention.backup_retention_days",
                minimum=1,
                maximum=36500,
            ),
        )


@dataclass(frozen=True)
class MemoryConfig(_Contract):
    """Validated ``config/memory.json``."""

    config_version: str
    policy: MemoryPolicy
    retention: RetentionPolicy
    namespaces: tuple[NamespaceConfig, ...]
    snapshots: tuple[SnapshotConfig, ...]
    lock_timeout_seconds: float

    @classmethod
    def from_dict(cls, data: object) -> MemoryConfig:
        keys = tuple(cls.__dataclass_fields__)
        data = _object(data, "memory config", keys)
        if data["config_version"] != MEMORY_CONFIG_VERSION:
            raise MemoryVersionError(
                f"config_version must be {MEMORY_CONFIG_VERSION}",
                details={"found": str(data["config_version"])[:100]},
            )
        for key in ("namespaces", "snapshots"):
            if not isinstance(data[key], list):
                raise MemoryInputError(f"{key} must be a list")
        timeout = number(data["lock_timeout_seconds"], "lock_timeout_seconds")
        if not 0 <= timeout <= 300:
            raise MemoryInputError("lock_timeout_seconds must be from 0 to 300")
        config = cls(
            MEMORY_CONFIG_VERSION,
            MemoryPolicy.from_dict(data["policy"]),
            RetentionPolicy.from_dict(data["retention"]),
            tuple(NamespaceConfig.from_dict(n) for n in data["namespaces"]),
            tuple(SnapshotConfig.from_dict(s) for s in data["snapshots"]),
            timeout,
        )
        config.registry()  # rejects ambiguous registrations at load time
        return config

    @classmethod
    def load(cls, path: Path | str) -> MemoryConfig:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise MemoryInputError(
                f"cannot read memory config: {exc.strerror or exc}"
            ) from None
        try:
            return cls.from_dict(loads_strict(raw))
        except CaseMemoryError:
            raise
        except ReviewError as exc:
            raise MemoryInputError(exc.message, details=exc.details) from None

    def registry(self):
        from watchlist.case_memory.identifiers import Registry

        return Registry(self.namespaces, self.snapshots)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "policy": self.policy.to_dict(),
            "retention": self.retention.to_dict(),
            "namespaces": [n.to_dict() for n in self.namespaces],
            "snapshots": [s.to_dict() for s in self.snapshots],
            "lock_timeout_seconds": self.lock_timeout_seconds,
        }


# ---------------------------------------------------------------------------
# Stored annotation contracts.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnnotationRecord(_Contract):
    """One exact reference record displayed in the earlier case, with its relation.

    ``relation`` describes the earlier reviewer's form, not identity truth:
    a displayed-but-unselected record is ``considered_without_selection``,
    never a verified negative.
    """

    record_key: str
    namespace_id: str
    source_entity_id: str
    snapshot_sha256: str
    relation: str
    rank: int
    score: float
    score_kind: str
    matched_alias_id: str
    alias_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: object) -> AnnotationRecord:
        data = _object(data, "record", tuple(cls.__dataclass_fields__))
        alias_ids = data["alias_ids"]
        if not isinstance(alias_ids, list):
            raise MemoryInputError("record.alias_ids must be a list")
        return cls(
            sha256_text(data["record_key"], "record.record_key"),
            _id_text(data["namespace_id"], "record.namespace_id"),
            _id_text(data["source_entity_id"], "record.source_entity_id"),
            sha256_text(data["snapshot_sha256"], "record.snapshot_sha256"),
            choice(data["relation"], "record.relation", RELATIONS),
            integer(data["rank"], "record.rank", minimum=1, maximum=50),
            number(data["score"], "record.score"),
            text(data["score_kind"], "record.score_kind", limit=100),
            _id_text(data["matched_alias_id"], "record.matched_alias_id"),
            tuple(_id_text(a, "record.alias_ids") for a in alias_ids),
        )


@dataclass(frozen=True)
class Annotation(_Contract):
    """A completed v2 review projected into memory (no raw query text).

    ``reason`` and ``actor`` are the reviewer's bounded opinion and the
    interface-supplied label; the actor label is not authentication.
    """

    annotation_schema: str
    annotation_id: str
    source_workspace_id: str
    case_id: str
    submission_id: str
    origin: str
    outcome: str
    selected_entity_id: str | None
    reason: str
    actor: str
    reviewed_at: str
    case_created_at: str
    workflow_version: str
    workflow_config_sha256: str
    packet_sha256: str
    final_event_sequence: int
    final_event_sha256: str
    retriever_kind: str
    retriever_name: str
    retriever_version: str
    namespace_id: str
    snapshot_sha256: str
    records: tuple[AnnotationRecord, ...]

    @property
    def sha256(self) -> str:
        return digest(self.to_dict())

    @classmethod
    def from_dict(cls, data: object) -> Annotation:
        data = _object(data, "annotation", tuple(cls.__dataclass_fields__))
        if data["annotation_schema"] != ANNOTATION_SCHEMA:
            raise MemoryVersionError(f"annotation_schema must be {ANNOTATION_SCHEMA}")
        records = data["records"]
        if not isinstance(records, list):
            raise MemoryInputError("annotation.records must be a list")
        selected = data["selected_entity_id"]
        return cls(
            ANNOTATION_SCHEMA,
            sha256_text(data["annotation_id"], "annotation.annotation_id"),
            _id_text(data["source_workspace_id"], "annotation.source_workspace_id"),
            uuid_text(data["case_id"], "annotation.case_id"),
            uuid_text(data["submission_id"], "annotation.submission_id"),
            choice(data["origin"], "annotation.origin", ORIGINS),
            choice(data["outcome"], "annotation.outcome", OUTCOMES),
            None if selected is None else _id_text(selected, "annotation.selected"),
            text(data["reason"], "annotation.reason", limit=4000),
            text(data["actor"], "annotation.actor", limit=200),
            iso(parse_time(data["reviewed_at"], "annotation.reviewed_at")),
            iso(parse_time(data["case_created_at"], "annotation.case_created_at")),
            text(data["workflow_version"], "annotation.workflow_version", limit=100),
            sha256_text(data["workflow_config_sha256"], "annotation.workflow_sha"),
            sha256_text(data["packet_sha256"], "annotation.packet_sha256"),
            integer(
                data["final_event_sequence"],
                "annotation.final_event_sequence",
                minimum=1,
                maximum=10**9,
            ),
            sha256_text(data["final_event_sha256"], "annotation.final_event_sha256"),
            text(data["retriever_kind"], "annotation.retriever_kind", limit=100),
            text(data["retriever_name"], "annotation.retriever_name", limit=200),
            text(data["retriever_version"], "annotation.retriever_version", limit=200),
            _id_text(data["namespace_id"], "annotation.namespace_id"),
            sha256_text(data["snapshot_sha256"], "annotation.snapshot_sha256"),
            tuple(AnnotationRecord.from_dict(r) for r in records),
        )


def annotation_id_for(source_workspace_id: str, case_id: str) -> str:
    """Deterministic annotation ID: replays of one source case reuse the same ID."""
    return digest(["case-memory-annotation", source_workspace_id, case_id])
