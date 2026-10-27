"""Versioned v2 review contracts shared by the CLI, API, UI and MCP layers.

Every contract is a frozen dataclass with ``to_dict()`` and a strict
``from_dict()``. ``to_dict()`` returns finite JSON values only (lists, never
tuples), so interface layers can serialize results without custom encoders.
``from_dict()`` rejects missing or extra keys, wrong types and nonfinite numbers
with ``ValidationError``. Structural checks live here; checks that need saved
state (for example a candidate belonging to the saved shortlist) live in
``watchlist.review.validation``.

This module is standard-library only. Importing it loads no workflow, lock
library or model. Outputs are possible record matches for human review; no
contract represents an identity finding, wrongdoing, sanctions status or
clearance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID

# Explicit contract versions. A change to any stored shape requires a new
# version string; the workflow identity is a declared version plus the digest
# of the workflow policy, never a hash of source bytes.
WORKSPACE_FORMAT = "review-workspace-v2"
CASE_SCHEMA = "review-case-v2"
PACKET_SCHEMA = "review-packet-v2"
SUBMISSION_SCHEMA = "review-submission-v2"
AUDIT_SCHEMA = "review-audit-v2"
WORKFLOW_VERSION = "review-workflow-v2.0"
CONFIG_VERSION = "review-config-v2"
EXPLANATION_TEMPLATE = "evidence-v2"

ORIGINS = ("human", "simulated_demo", "test", "unknown")
OUTCOMES = ("candidate_for_follow_up", "none_selected", "unresolved")
CASE_STATUSES = (
    "preparing",
    "awaiting_review",
    "finalizing",
    "completed",
    "cancelled",
    "incompatible",
    "inconsistent",
)
NEXT_ACTIONS = ("submit_review", "recover", "none")
AUDIT_EVENT_TYPES = (
    "case_created",
    "evidence_recorded",
    "review_ready",
    "review_submitted",
    "case_finalized",
    "case_cancelled",
)

Origin = Literal["human", "simulated_demo", "test", "unknown"]
Outcome = Literal["candidate_for_follow_up", "none_selected", "unresolved"]
CaseStatus = Literal[
    "preparing",
    "awaiting_review",
    "finalizing",
    "completed",
    "cancelled",
    "incompatible",
    "inconsistent",
]

LIMITATION = (
    "Possible record matches for research and human review only. Similarity scores are "
    "not identity probabilities. Names and this reference snapshot cannot establish "
    "identity, wrongdoing or sanctions status; selecting none is not a clearance. "
    "The actor label is supplied by the calling interface and is not authentication."
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
_PROVIDER_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_MAX_DEPTH = 32


# ---------------------------------------------------------------------------
# Structured domain errors. ``code`` is stable; interfaces map it to transport
# statuses (see docs/adr/0007-review-service-v2.md) instead of parsing text.
# ---------------------------------------------------------------------------


class ReviewError(Exception):
    """Base class for expected review-service failures."""

    code = "review_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})
        if retryable is not None:
            # Per-instance override, e.g. a retryable cause after a case already exists.
            self.retryable = bool(retryable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": json.loads(canonical(self.details)),
        }


class ValidationError(ReviewError):
    """Caller input has the wrong shape, type, bounds or value."""

    code = "invalid_input"


class NotFoundError(ReviewError):
    """The requested case does not exist in this workspace."""

    code = "case_not_found"


class WorkspaceError(ReviewError):
    """A workspace is missing, incomplete, not empty or not an owned v2 store."""

    code = "workspace_invalid"


class WorkspaceBusyError(ReviewError):
    """The cooperative workspace lock was not acquired before its deadline."""

    code = "workspace_busy"
    retryable = True


class ReentrantCallError(ReviewError):
    """This thread already holds the workspace lock (e.g. a provider called back)."""

    code = "reentrant_call"


class ReadOnlyError(ReviewError):
    """A mutating operation was requested from a read-only service."""

    code = "read_only"


class VersionMismatchError(ReviewError):
    """Stored contract versions or workflow identity differ from this service."""

    code = "incompatible_version"


class InvalidStateError(ReviewError):
    """The case is not in a state that permits the requested operation."""

    code = "invalid_state"


class StaleSubmissionError(ReviewError):
    """The submission refers to old evidence or the case was already reviewed."""

    code = "stale_submission"


class SubmissionConflictError(ReviewError):
    """A submission ID was reused with different content or for another case."""

    code = "submission_conflict"


class RequestConflictError(ReviewError):
    """A start-case ``request_id`` was reused with a different request."""

    code = "request_conflict"


class RecoveryRequiredError(ReviewError):
    """Interrupted work must be finished with ``recover`` before this operation."""

    code = "recovery_required"


class StorageIntegrityError(ReviewError):
    """Saved records, audit chain or checkpoint history disagree."""

    code = "storage_inconsistent"


class RetrieverMismatchError(ReviewError):
    """Retrieval would run with a different retriever than the case froze."""

    code = "retriever_mismatch"


class RetrievalError(ReviewError):
    """The retriever failed or returned an invalid result; nothing is inferred."""

    code = "retrieval_failed"
    retryable = True


class ContextProviderError(ReviewError):
    """A packet context provider failed or returned invalid data.

    ``details['cause']`` names the provider's error code or exception type.
    """

    code = "context_provider_failed"
    retryable = True


# ---------------------------------------------------------------------------
# Canonical JSON and small structural helpers.
# ---------------------------------------------------------------------------


def canonical(value: object) -> str:
    """One finite JSON text for hashing and storage (sorted keys, UTF-8 safe)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: object) -> str:
    """SHA-256 of the canonical JSON text; a consistency check, not authentication."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValidationError(f"nonfinite JSON number: {value}")


def loads_strict(text: str) -> Any:
    """Parse JSON text rejecting duplicate keys and NaN/Infinity."""
    try:
        return json.loads(
            text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON: {exc.msg}") from None


def json_value(value: object, label: str, *, depth: int = 0) -> Any:
    """Return a detached copy of a finite JSON value or raise ValidationError."""
    if depth > _MAX_DEPTH:
        raise ValidationError(f"{label} is nested too deeply")
    if value is None or type(value) in (str, bool, int):
        if type(value) is str:
            _unicode(value, label)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValidationError(f"{label} must contain finite numbers only")
        return value
    if isinstance(value, (list, tuple)):
        return [json_value(item, label, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise ValidationError(f"{label} object keys must be strings")
            result[key] = json_value(item, label, depth=depth + 1)
        return result
    raise ValidationError(f"{label} must contain only JSON values")


def _unicode(value: str, label: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(f"{label} must contain valid Unicode text") from None


def _object(data: object, label: str, keys: Sequence[str]) -> Mapping[str, Any]:
    """Require a JSON object with exactly the given keys."""
    if not isinstance(data, Mapping):
        raise ValidationError(f"{label} must be a JSON object")
    actual = set(data)
    expected = set(keys)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(str(key) for key in actual - expected)
        raise ValidationError(
            f"{label} fields mismatch",
            details={"missing": missing, "unexpected": extra},
        )
    return data


def text(
    value: object, label: str, *, limit: int = 1000, allow_blank: bool = False
) -> str:
    """Require bounded Unicode text; nonblank unless explicitly allowed."""
    if type(value) is not str:
        raise ValidationError(f"{label} must be text")
    if len(value) > limit or (not allow_blank and not value.strip()):
        raise ValidationError(
            f"{label} must be {'' if allow_blank else 'nonblank '}text of at most {limit} characters"
        )
    _unicode(value, label)
    return value


def optional_text(value: object, label: str, *, limit: int = 1000) -> str | None:
    return None if value is None else text(value, label, limit=limit)


def integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"{label} must be an integer from {minimum} to {maximum}")
    return value


def number(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValidationError(f"{label} must be a finite number")
    return float(value)


def boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{label} must be a boolean")
    return value


def sha256_text(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValidationError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def uuid_text(value: object, label: str) -> str:
    """Accept only the canonical lowercase hyphenated UUID spelling."""
    if type(value) is not str:
        raise ValidationError(f"{label} must be a UUID string")
    try:
        parsed = UUID(value)
    except ValueError:
        raise ValidationError(f"{label} must be a UUID string") from None
    if str(parsed) != value:
        raise ValidationError(f"{label} must use canonical lowercase UUID form")
    return value


def choice(value: object, label: str, options: Sequence[str]) -> str:
    if type(value) is not str or value not in options:
        raise ValidationError(f"{label} must be one of: {', '.join(options)}")
    return value


def text_list(value: object, label: str, *, limit: int = 1000) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{label} must be a list")
    return tuple(text(item, label, limit=limit) for item in value)


def _object_list(value: object, label: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{label} must be a list")
    return list(value)


def _plain(value: object) -> Any:
    """Convert nested contracts/tuples to plain JSON containers."""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    return value


class _Contract:
    """Shared ``to_dict``; subclasses implement strict ``from_dict``."""

    def to_dict(self) -> dict[str, Any]:
        return {name: _plain(getattr(self, name)) for name in self.__dataclass_fields__}


# ---------------------------------------------------------------------------
# Configuration and workflow identity.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowIdentity(_Contract):
    """Declared workflow version plus the digest of its behavioral policy."""

    version: str
    config_sha256: str

    @classmethod
    def from_dict(cls, data: object) -> WorkflowIdentity:
        data = _object(data, "workflow", ("version", "config_sha256"))
        return cls(
            text(data["version"], "workflow.version", limit=100),
            sha256_text(data["config_sha256"], "workflow.config_sha256"),
        )


@dataclass(frozen=True)
class WorkflowPolicy(_Contract):
    """Settings that change review behavior; their digest is part of the identity.

    ``allowed_origins`` is kept in the canonical ``ORIGINS`` order, so reordering
    the config list does not change the identity. Capacity and paging limits
    live in ``OperationsSettings`` because they never change how a case is
    reviewed.
    """

    top_k: int
    name_max_chars: int
    reason_max_chars: int
    actor_max_chars: int
    allowed_origins: tuple[str, ...]
    explanation_template: str
    context_max_bytes: int

    @classmethod
    def from_dict(cls, data: object) -> WorkflowPolicy:
        data = _object(data, "workflow", tuple(cls.__dataclass_fields__))
        origins = text_list(
            data["allowed_origins"], "workflow.allowed_origins", limit=40
        )
        if (
            not origins
            or len(set(origins)) != len(origins)
            or any(o not in ORIGINS for o in origins)
        ):
            raise ValidationError(
                "workflow.allowed_origins must be unique known origins"
            )
        if data["explanation_template"] != EXPLANATION_TEMPLATE:
            raise ValidationError(
                f"workflow.explanation_template must be {EXPLANATION_TEMPLATE}"
            )
        return cls(
            top_k=integer(data["top_k"], "workflow.top_k", minimum=1, maximum=50),
            name_max_chars=integer(
                data["name_max_chars"],
                "workflow.name_max_chars",
                minimum=1,
                maximum=200,
            ),
            reason_max_chars=integer(
                data["reason_max_chars"],
                "workflow.reason_max_chars",
                minimum=1,
                maximum=4000,
            ),
            actor_max_chars=integer(
                data["actor_max_chars"],
                "workflow.actor_max_chars",
                minimum=1,
                maximum=200,
            ),
            allowed_origins=tuple(origin for origin in ORIGINS if origin in origins),
            explanation_template=EXPLANATION_TEMPLATE,
            context_max_bytes=integer(
                data["context_max_bytes"],
                "workflow.context_max_bytes",
                minimum=0,
                maximum=1_048_576,
            ),
        )


@dataclass(frozen=True)
class RetrieverSettings(_Contract):
    """Which adapter the default service builds; frozen per case as a descriptor."""

    kind: Literal["rapidfuzz", "e5"]
    dataset: Literal["packaged-fictional"]
    embedding_config: str | None

    @classmethod
    def from_dict(cls, data: object) -> RetrieverSettings:
        data = _object(data, "retriever", ("kind", "dataset", "embedding_config"))
        kind = choice(data["kind"], "retriever.kind", ("rapidfuzz", "e5"))
        dataset = choice(data["dataset"], "retriever.dataset", ("packaged-fictional",))
        embedding = optional_text(
            data["embedding_config"], "retriever.embedding_config", limit=1000
        )
        if (kind == "e5") != (embedding is not None):
            raise ValidationError(
                "retriever.embedding_config is required for e5 and must be null otherwise"
            )
        return cls(kind, dataset, embedding)


@dataclass(frozen=True)
class OperationsSettings(_Contract):
    """Operational settings that do not change review semantics (not in the identity)."""

    lock_timeout_seconds: float
    max_cases: int
    page_max: int

    @classmethod
    def from_dict(cls, data: object) -> OperationsSettings:
        data = _object(data, "operations", tuple(cls.__dataclass_fields__))
        timeout = number(
            data["lock_timeout_seconds"], "operations.lock_timeout_seconds"
        )
        if not 0 <= timeout <= 300:
            raise ValidationError(
                "operations.lock_timeout_seconds must be from 0 to 300"
            )
        return cls(
            timeout,
            integer(
                data["max_cases"], "operations.max_cases", minimum=1, maximum=1_000_000
            ),
            integer(data["page_max"], "operations.page_max", minimum=1, maximum=1000),
        )


@dataclass(frozen=True)
class ReviewConfig(_Contract):
    """Validated ``config/review-v2.json``."""

    config_version: str
    workflow: WorkflowPolicy
    retriever: RetrieverSettings
    operations: OperationsSettings

    @classmethod
    def from_dict(cls, data: object) -> ReviewConfig:
        data = _object(
            data, "config", ("config_version", "workflow", "retriever", "operations")
        )
        if data["config_version"] != CONFIG_VERSION:
            raise VersionMismatchError(
                f"config_version must be {CONFIG_VERSION}",
                details={"found": str(data["config_version"])[:100]},
            )
        return cls(
            CONFIG_VERSION,
            WorkflowPolicy.from_dict(data["workflow"]),
            RetrieverSettings.from_dict(data["retriever"]),
            OperationsSettings.from_dict(data["operations"]),
        )

    @classmethod
    def load(cls, path: Path | str) -> ReviewConfig:
        """Read a UTF-8 JSON config with duplicate-key and NaN rejection."""
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(
                f"cannot read review config: {exc.strerror or exc}"
            ) from None
        return cls.from_dict(loads_strict(raw))

    def workflow_identity(self) -> WorkflowIdentity:
        """Bind cases to behavior, not to source bytes: comments never lock cases out."""
        return WorkflowIdentity(WORKFLOW_VERSION, digest(self.workflow.to_dict()))


# ---------------------------------------------------------------------------
# Retrieval evidence and the immutable review packet.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrieverDescriptor(_Contract):
    """Frozen identity of a retriever and its reference data, stored per case."""

    kind: str
    name: str
    version: str
    reference_sha256: str
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> RetrieverDescriptor:
        data = _object(
            data,
            "retriever",
            ("kind", "name", "version", "reference_sha256", "settings"),
        )
        settings = json_value(data["settings"], "retriever.settings")
        if not isinstance(settings, dict):
            raise ValidationError("retriever.settings must be an object")
        return cls(
            text(data["kind"], "retriever.kind", limit=100),
            text(data["name"], "retriever.name", limit=200),
            text(data["version"], "retriever.version", limit=200),
            sha256_text(data["reference_sha256"], "retriever.reference_sha256"),
            settings,
        )


@dataclass(frozen=True)
class AliasEvidence(_Contract):
    """One stored alias spelling of a reference record, with its provenance."""

    evidence_id: str
    entity_id: str
    alias_id: str
    original_text: str
    source: str
    synthetic: bool
    attribution: str
    provenance: dict[str, Any] | None

    @classmethod
    def from_dict(cls, data: object) -> AliasEvidence:
        data = _object(data, "evidence", tuple(cls.__dataclass_fields__))
        provenance = data["provenance"]
        if provenance is not None:
            provenance = json_value(provenance, "evidence.provenance")
            if not isinstance(provenance, dict):
                raise ValidationError("evidence.provenance must be an object or null")
        return cls(
            *(
                text(data[k], f"evidence.{k}", limit=10000)
                for k in (
                    "evidence_id",
                    "entity_id",
                    "alias_id",
                    "original_text",
                    "source",
                )
            ),
            boolean(data["synthetic"], "evidence.synthetic"),
            text(data["attribution"], "evidence.attribution", limit=10000),
            provenance,
        )


@dataclass(frozen=True)
class Candidate(_Contract):
    """A ranked possible record match; ``score`` is raw similarity, not probability."""

    rank: int
    entity_id: str
    display_name: str
    entity_type: str
    matched_alias_id: str
    matched_alias: str
    score: float
    score_kind: str
    evidence_ids: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: object) -> Candidate:
        data = _object(data, "candidate", tuple(cls.__dataclass_fields__))
        return cls(
            integer(data["rank"], "candidate.rank", minimum=1, maximum=50),
            *(
                text(data[k], f"candidate.{k}", limit=10000)
                for k in (
                    "entity_id",
                    "display_name",
                    "entity_type",
                    "matched_alias_id",
                    "matched_alias",
                )
            ),
            number(data["score"], "candidate.score"),
            text(data["score_kind"], "candidate.score_kind", limit=100),
            text_list(data["evidence_ids"], "candidate.evidence_ids", limit=10000),
        )


@dataclass(frozen=True)
class RetrievalResult(_Contract):
    """Candidates and all alias evidence returned by one retriever call."""

    retriever: RetrieverDescriptor
    top_k: int
    candidates: tuple[Candidate, ...]
    evidence: tuple[AliasEvidence, ...]

    @classmethod
    def from_dict(cls, data: object) -> RetrievalResult:
        data = _object(
            data, "retrieval", ("retriever", "top_k", "candidates", "evidence")
        )
        return cls(
            RetrieverDescriptor.from_dict(data["retriever"]),
            integer(data["top_k"], "retrieval.top_k", minimum=1, maximum=50),
            tuple(
                Candidate.from_dict(c)
                for c in _object_list(data["candidates"], "retrieval.candidates")
            ),
            tuple(
                AliasEvidence.from_dict(e)
                for e in _object_list(data["evidence"], "retrieval.evidence")
            ),
        )


@dataclass(frozen=True)
class ContextRequest(_Contract):
    """What a context provider may see when adding display-only packet context."""

    case_id: str
    origin: str
    name: str
    normalized_name: str
    candidate_entity_ids: tuple[str, ...]
    retriever: RetrieverDescriptor
    # Distinct labels from the frozen alias evidence. None means no binding
    # was supplied (older callers); () is valid only for an empty shortlist.
    candidate_sources: tuple[str, ...] | None = None


@runtime_checkable
class ContextProvider(Protocol):
    """Hook for display-only packet context, e.g. reviewer memory (Step 20).

    ``name`` must match ``[a-z0-9][a-z0-9_.-]{0,63}``. ``provide`` returns a
    JSON object that is frozen into the packet and its digest; it must not
    change candidates, scores or outcomes. Every exception, a ``ReviewError``
    included, becomes ``ContextProviderError`` (original code or exception
    type in ``details['cause']``) and leaves the case recoverable.

    ``provide`` runs while the service holds the workspace lock, so it must
    not call back into a ``ReviewService`` on the same workspace. A call from
    the same thread fails at once with ``reentrant_call`` (not retryable); one
    from another thread would wait for the lock timeout. Read what the
    provider needs from its own store or from ``ContextRequest``.
    """

    name: str
    version: str

    def provide(self, request: ContextRequest) -> Mapping[str, Any]: ...


@runtime_checkable
class Retriever(Protocol):
    """Adapter boundary for candidate retrieval (RapidFuzz default, optional E5)."""

    def descriptor(self) -> RetrieverDescriptor:
        """Return the frozen identity; it must be cheap and must not load a model."""
        ...

    def search(self, normalized_name: str, *, top_k: int) -> RetrievalResult:
        """Return up to ``top_k`` candidates and all alias evidence for them."""
        ...


def provider_name(value: object) -> str:
    if type(value) is not str or not _PROVIDER_NAME.fullmatch(value):
        raise ValidationError(
            "context provider name must match [a-z0-9][a-z0-9_.-]{0,63}"
        )
    return value


@dataclass(frozen=True)
class ReviewPacket(_Contract):
    """Immutable evidence shown to the reviewer; its digest binds submissions."""

    packet_schema: str
    case_id: str
    workflow: WorkflowIdentity
    origin: str
    name: str
    normalized_name: str
    retrieval: RetrievalResult
    context: dict[str, Any]
    limitation: str

    @property
    def sha256(self) -> str:
        return digest(self.to_dict())

    @classmethod
    def from_dict(cls, data: object) -> ReviewPacket:
        data = _object(data, "packet", tuple(cls.__dataclass_fields__))
        if data["packet_schema"] != PACKET_SCHEMA:
            raise VersionMismatchError(f"packet_schema must be {PACKET_SCHEMA}")
        context = json_value(data["context"], "packet.context")
        if not isinstance(context, dict) or any(provider_name(k) != k for k in context):
            raise ValidationError("packet.context must map provider names to values")
        return cls(
            PACKET_SCHEMA,
            uuid_text(data["case_id"], "packet.case_id"),
            WorkflowIdentity.from_dict(data["workflow"]),
            choice(data["origin"], "packet.origin", ORIGINS),
            text(data["name"], "packet.name", limit=200),
            text(data["normalized_name"], "packet.normalized_name", limit=200),
            RetrievalResult.from_dict(data["retrieval"]),
            context,
            text(data["limitation"], "packet.limitation", limit=4000),
        )


# ---------------------------------------------------------------------------
# Human review, cancellation and explanation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewSubmission(_Contract):
    """Caller-owned review form. ``actor`` comes from the interface's identity layer.

    ``submission_id`` is a client-generated UUID that makes retries idempotent.
    ``packet_sha256`` must equal the packet the reviewer actually saw.
    """

    submission_id: str
    packet_sha256: str
    outcome: str
    candidate_entity_id: str | None
    reason: str
    actor: str
    origin: str

    def __post_init__(self) -> None:
        uuid_text(self.submission_id, "submission_id")
        sha256_text(self.packet_sha256, "packet_sha256")
        choice(self.outcome, "outcome", OUTCOMES)
        choice(self.origin, "origin", ORIGINS)
        text(self.reason, "reason", limit=4000)
        text(self.actor, "actor", limit=200)
        if self.outcome == "candidate_for_follow_up":
            text(self.candidate_entity_id, "candidate_entity_id", limit=10000)
        elif self.candidate_entity_id is not None:
            raise ValidationError(
                "candidate_entity_id must be null unless outcome is candidate_for_follow_up"
            )

    @classmethod
    def from_dict(cls, data: object) -> ReviewSubmission:
        data = _object(data, "submission", tuple(cls.__dataclass_fields__))
        return cls(**{key: data[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class AcceptedReview(_Contract):
    """A durably accepted submission with the service-assigned time."""

    submission_schema: str
    case_id: str
    submitted_at: str
    submission: ReviewSubmission

    @classmethod
    def from_dict(cls, data: object) -> AcceptedReview:
        data = _object(data, "review", tuple(cls.__dataclass_fields__))
        if data["submission_schema"] != SUBMISSION_SCHEMA:
            raise VersionMismatchError(f"submission_schema must be {SUBMISSION_SCHEMA}")
        return cls(
            SUBMISSION_SCHEMA,
            uuid_text(data["case_id"], "review.case_id"),
            text(data["submitted_at"], "review.submitted_at", limit=64),
            ReviewSubmission.from_dict(data["submission"]),
        )


@dataclass(frozen=True)
class Cancellation(_Contract):
    """An explicit stop before any review was accepted; not a review outcome."""

    case_id: str
    actor: str
    reason: str
    cancelled_at: str

    @classmethod
    def from_dict(cls, data: object) -> Cancellation:
        data = _object(data, "cancellation", tuple(cls.__dataclass_fields__))
        return cls(
            uuid_text(data["case_id"], "cancellation.case_id"),
            text(data["actor"], "cancellation.actor", limit=200),
            text(data["reason"], "cancellation.reason", limit=4000),
            text(data["cancelled_at"], "cancellation.cancelled_at", limit=64),
        )


@dataclass(frozen=True)
class Explanation(_Contract):
    """Deterministic template text built only from the packet and the review."""

    template_version: str
    text: str
    evidence_ids: tuple[str, ...]
    limitation: str

    @classmethod
    def from_dict(cls, data: object) -> Explanation:
        data = _object(data, "explanation", tuple(cls.__dataclass_fields__))
        return cls(
            text(data["template_version"], "explanation.template_version", limit=100),
            text(data["text"], "explanation.text", limit=20000),
            text_list(data["evidence_ids"], "explanation.evidence_ids", limit=10000),
            text(data["limitation"], "explanation.limitation", limit=4000),
        )


# ---------------------------------------------------------------------------
# Case and audit views returned by the service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseHeader(_Contract):
    """Immutable request envelope recorded when a case is created.

    ``request_id`` is the optional client UUID that makes ``start_case``
    retries idempotent; it is unique within a workspace.
    """

    case_schema: str
    case_id: str
    thread_id: str
    request_id: str | None
    created_at: str
    origin: str
    actor: str
    name: str
    normalized_name: str
    workflow: WorkflowIdentity
    packet_schema: str
    retriever: RetrieverDescriptor
    context_providers: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: object) -> CaseHeader:
        data = _object(data, "case", tuple(cls.__dataclass_fields__))
        if data["case_schema"] != CASE_SCHEMA:
            raise VersionMismatchError(f"case_schema must be {CASE_SCHEMA}")
        providers = text_list(
            data["context_providers"], "case.context_providers", limit=64
        )
        for name in providers:
            provider_name(name)
        request_id = (
            None
            if data["request_id"] is None
            else uuid_text(data["request_id"], "case.request_id")
        )
        return cls(
            CASE_SCHEMA,
            uuid_text(data["case_id"], "case.case_id"),
            uuid_text(data["thread_id"], "case.thread_id"),
            request_id,
            text(data["created_at"], "case.created_at", limit=64),
            choice(data["origin"], "case.origin", ORIGINS),
            text(data["actor"], "case.actor", limit=200),
            text(data["name"], "case.name", limit=200),
            text(data["normalized_name"], "case.normalized_name", limit=200),
            WorkflowIdentity.from_dict(data["workflow"]),
            text(data["packet_schema"], "case.packet_schema", limit=100),
            RetrieverDescriptor.from_dict(data["retriever"]),
            providers,
        )


@dataclass(frozen=True)
class AuditReceipt(_Contract):
    """Identifies the final audit event of a completed case."""

    case_id: str
    sequence: int
    event_sha256: str

    @classmethod
    def from_dict(cls, data: object) -> AuditReceipt:
        data = _object(data, "receipt", tuple(cls.__dataclass_fields__))
        return cls(
            uuid_text(data["case_id"], "receipt.case_id"),
            integer(data["sequence"], "receipt.sequence", minimum=1, maximum=10**9),
            sha256_text(data["event_sha256"], "receipt.event_sha256"),
        )


@dataclass(frozen=True)
class CaseView(_Contract):
    """Complete read model of one case.

    ``status`` distinguishes preparing, awaiting_review, finalizing (accepted
    but not finalized), completed, cancelled, incompatible and inconsistent.
    ``next_action`` tells an interface what is possible: ``submit_review``,
    ``recover`` or ``none``. ``issues`` lists storage inconsistencies and
    ``compatibility_issues`` lists version differences, without repairs.
    """

    case_id: str
    status: str
    next_action: str
    recovery_required: bool
    compatible: bool
    compatibility_issues: tuple[str, ...]
    issues: tuple[str, ...]
    header: CaseHeader
    packet: ReviewPacket | None
    packet_sha256: str | None
    review: AcceptedReview | None
    explanation: Explanation | None
    final_receipt: AuditReceipt | None
    cancellation: Cancellation | None
    limitation: str

    @classmethod
    def from_dict(cls, data: object) -> CaseView:
        data = _object(data, "case_view", tuple(cls.__dataclass_fields__))
        packet = (
            None if data["packet"] is None else ReviewPacket.from_dict(data["packet"])
        )
        return cls(
            uuid_text(data["case_id"], "case_view.case_id"),
            choice(data["status"], "status", CASE_STATUSES),
            choice(data["next_action"], "next_action", NEXT_ACTIONS),
            boolean(data["recovery_required"], "recovery_required"),
            boolean(data["compatible"], "compatible"),
            text_list(data["compatibility_issues"], "compatibility_issues"),
            text_list(data["issues"], "issues"),
            CaseHeader.from_dict(data["header"]),
            packet,
            None
            if data["packet_sha256"] is None
            else sha256_text(data["packet_sha256"], "packet_sha256"),
            None
            if data["review"] is None
            else AcceptedReview.from_dict(data["review"]),
            None
            if data["explanation"] is None
            else Explanation.from_dict(data["explanation"]),
            None
            if data["final_receipt"] is None
            else AuditReceipt.from_dict(data["final_receipt"]),
            None
            if data["cancellation"] is None
            else Cancellation.from_dict(data["cancellation"]),
            text(data["limitation"], "limitation", limit=4000),
        )


@dataclass(frozen=True)
class CaseSummary(_Contract):
    """Catalogue row without query text, evidence or reviewer notes."""

    case_id: str
    created_at: str
    origin: str
    status: str
    next_action: str
    recovery_required: bool
    compatible: bool
    workflow_version: str

    @classmethod
    def from_dict(cls, data: object) -> CaseSummary:
        data = _object(data, "case_summary", tuple(cls.__dataclass_fields__))
        return cls(
            uuid_text(data["case_id"], "case_id"),
            text(data["created_at"], "created_at", limit=64),
            choice(data["origin"], "origin", ORIGINS),
            choice(data["status"], "status", CASE_STATUSES),
            choice(data["next_action"], "next_action", NEXT_ACTIONS),
            boolean(data["recovery_required"], "recovery_required"),
            boolean(data["compatible"], "compatible"),
            text(data["workflow_version"], "workflow_version", limit=100),
        )


@dataclass(frozen=True)
class AuditEvent(_Contract):
    """One hash-chained audit record; ``payload`` is the canonical stored JSON."""

    case_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str
    previous_sha256: str
    event_sha256: str

    @classmethod
    def from_dict(cls, data: object) -> AuditEvent:
        data = _object(data, "audit_event", tuple(cls.__dataclass_fields__))
        payload = json_value(data["payload"], "audit_event.payload")
        if not isinstance(payload, dict):
            raise ValidationError("audit_event.payload must be an object")
        previous = data["previous_sha256"]
        if previous != "":
            sha256_text(previous, "audit_event.previous_sha256")
        return cls(
            uuid_text(data["case_id"], "audit_event.case_id"),
            integer(data["sequence"], "audit_event.sequence", minimum=1, maximum=10**9),
            choice(data["event_type"], "audit_event.event_type", AUDIT_EVENT_TYPES),
            payload,
            text(data["created_at"], "audit_event.created_at", limit=64),
            previous,
            sha256_text(data["event_sha256"], "audit_event.event_sha256"),
        )


@dataclass(frozen=True)
class AuditView(_Contract):
    """All audit events of one case in sequence order."""

    audit_schema: str
    case_id: str
    count: int
    head_sha256: str
    events: tuple[AuditEvent, ...]

    @classmethod
    def from_dict(cls, data: object) -> AuditView:
        data = _object(data, "audit", tuple(cls.__dataclass_fields__))
        if data["audit_schema"] != AUDIT_SCHEMA:
            raise VersionMismatchError(f"audit_schema must be {AUDIT_SCHEMA}")
        head = data["head_sha256"]
        if head != "":
            sha256_text(head, "audit.head_sha256")
        return cls(
            AUDIT_SCHEMA,
            uuid_text(data["case_id"], "audit.case_id"),
            integer(data["count"], "audit.count", minimum=0, maximum=10**9),
            head,
            tuple(
                AuditEvent.from_dict(e)
                for e in _object_list(data["events"], "audit.events")
            ),
        )


@dataclass(frozen=True)
class AuditAnchor(_Contract):
    """A separately retained (count, head) pair; detects a truncated chain tail."""

    case_id: str
    count: int
    head_sha256: str

    def __post_init__(self) -> None:
        # Checked on direct construction too: verify_audit indexes by count.
        uuid_text(self.case_id, "anchor.case_id")
        integer(self.count, "anchor.count", minimum=1, maximum=10**9)
        sha256_text(self.head_sha256, "anchor.head_sha256")

    @classmethod
    def from_dict(cls, data: object) -> AuditAnchor:
        data = _object(data, "anchor", tuple(cls.__dataclass_fields__))
        return cls(data["case_id"], data["count"], data["head_sha256"])


@dataclass(frozen=True)
class AuditVerification(_Contract):
    """Result of local tamper-evidence checks; ``ok`` is not authentication."""

    case_id: str
    ok: bool
    count: int
    head_sha256: str
    anchor_checked: bool
    issues: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: object) -> AuditVerification:
        data = _object(data, "verification", tuple(cls.__dataclass_fields__))
        return cls(
            uuid_text(data["case_id"], "verification.case_id"),
            boolean(data["ok"], "verification.ok"),
            integer(data["count"], "verification.count", minimum=0, maximum=10**9),
            text(
                data["head_sha256"],
                "verification.head_sha256",
                limit=64,
                allow_blank=True,
            ),
            boolean(data["anchor_checked"], "verification.anchor_checked"),
            text_list(data["issues"], "verification.issues"),
        )
