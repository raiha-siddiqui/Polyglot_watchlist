"""Pydantic request and response models for the v2 review API.

Response models mirror the field names of the v2 contracts'
``to_dict()`` output one-to-one (a test compares them with the dataclass
fields, so drift fails loudly). They exist to document and check the wire
format; the service's frozen dataclasses remain the source of truth, and
endpoints build responses from ``to_dict()`` only.

Request models are strict and reject unknown fields. In particular no request
accepts an ``actor``: the actor label always comes from the API key.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "NAME_MAX_CHARS",
    "REASON_MAX_CHARS",
    "StartCaseRequest",
    "ReviewRequest",
    "CancelRequest",
    "CaseResponse",
    "CaseListResponse",
    "AuditResponse",
    "VerificationResponse",
    "EntityResponse",
    "HealthResponse",
    "ErrorResponse",
    "CONTRACT_MODELS",
]

# Python ``len`` counts code points, so these are code-point limits.
NAME_MAX_CHARS = 200
REASON_MAX_CHARS = 1000

Origin = Literal["human", "simulated_demo", "test", "unknown"]
Outcome = Literal["candidate_for_follow_up", "none_selected", "unresolved"]
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_SHA256 = r"^[0-9a-f]{64}$"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- requests ------------------------------------------------------------------


class StartCaseRequest(_Request):
    """Start a screening case for one name. Send ``request_id`` so retries are safe."""

    name: str = Field(min_length=1, max_length=NAME_MAX_CHARS)
    origin: Origin
    request_id: str | None = Field(
        default=None,
        pattern=_UUID,
        description="Client UUID (lowercase, hyphenated); a retry with the same "
        "request returns the same case instead of creating a duplicate.",
    )


class ReviewRequest(_Request):
    """One human review form, bound to the exact case revision and packet seen."""

    submission_id: str = Field(
        pattern=_UUID, description="Client UUID that makes retries idempotent."
    )
    revision: str = Field(
        pattern=_SHA256,
        description="The `revision` of the case response the reviewer looked at.",
    )
    packet_sha256: str = Field(pattern=_SHA256)
    outcome: Outcome
    candidate_entity_id: str | None = Field(
        max_length=200,
        description="Required for candidate_for_follow_up; null otherwise.",
    )
    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)


class CancelRequest(_Request):
    """Cancel a case before any review is accepted (not a review outcome)."""

    reason: str = Field(min_length=1, max_length=REASON_MAX_CHARS)


# -- contract mirrors -------------------------------------------------------------


class WorkflowIdentityModel(_Wire):
    version: str
    config_sha256: str


class RetrieverDescriptorModel(_Wire):
    kind: str
    name: str
    version: str
    reference_sha256: str
    settings: dict[str, Any]


class AliasEvidenceModel(_Wire):
    evidence_id: str
    entity_id: str
    alias_id: str
    original_text: str
    source: str
    synthetic: bool
    attribution: str
    provenance: dict[str, Any] | None


class CandidateModel(_Wire):
    """A possible record match; ``score`` is raw similarity, not a probability."""

    rank: int
    entity_id: str
    display_name: str
    entity_type: str
    matched_alias_id: str
    matched_alias: str
    score: float
    score_kind: str
    evidence_ids: list[str]


class RetrievalResultModel(_Wire):
    retriever: RetrieverDescriptorModel
    top_k: int
    candidates: list[CandidateModel]
    evidence: list[AliasEvidenceModel]


class ReviewPacketModel(_Wire):
    packet_schema: str
    case_id: str
    workflow: WorkflowIdentityModel
    origin: Origin
    name: str
    normalized_name: str
    retrieval: RetrievalResultModel
    context: dict[str, Any]
    limitation: str


class ReviewSubmissionModel(_Wire):
    submission_id: str
    packet_sha256: str
    outcome: Outcome
    candidate_entity_id: str | None
    reason: str
    actor: str
    origin: Origin


class AcceptedReviewModel(_Wire):
    submission_schema: str
    case_id: str
    submitted_at: str
    submission: ReviewSubmissionModel


class CancellationModel(_Wire):
    case_id: str
    actor: str
    reason: str
    cancelled_at: str


class ExplanationModel(_Wire):
    template_version: str
    text: str
    evidence_ids: list[str]
    limitation: str


class CaseHeaderModel(_Wire):
    case_schema: str
    case_id: str
    thread_id: str
    request_id: str | None
    created_at: str
    origin: Origin
    actor: str
    name: str
    normalized_name: str
    workflow: WorkflowIdentityModel
    packet_schema: str
    retriever: RetrieverDescriptorModel
    context_providers: list[str]


class AuditReceiptModel(_Wire):
    case_id: str
    sequence: int
    event_sha256: str


class CaseViewModel(_Wire):
    case_id: str
    status: Literal[
        "preparing",
        "awaiting_review",
        "finalizing",
        "completed",
        "cancelled",
        "incompatible",
        "inconsistent",
    ]
    next_action: Literal["submit_review", "recover", "none"]
    recovery_required: bool
    compatible: bool
    compatibility_issues: list[str]
    issues: list[str]
    header: CaseHeaderModel
    packet: ReviewPacketModel | None
    packet_sha256: str | None
    review: AcceptedReviewModel | None
    explanation: ExplanationModel | None
    final_receipt: AuditReceiptModel | None
    cancellation: CancellationModel | None
    limitation: str


class CaseSummaryModel(_Wire):
    case_id: str
    created_at: str
    origin: Origin
    status: str
    next_action: str
    recovery_required: bool
    compatible: bool
    workflow_version: str


class AuditEventModel(_Wire):
    case_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str
    previous_sha256: str
    event_sha256: str


class AuditViewModel(_Wire):
    audit_schema: str
    case_id: str
    count: int
    head_sha256: str
    events: list[AuditEventModel]


class AuditVerificationModel(_Wire):
    case_id: str
    ok: bool
    count: int
    head_sha256: str
    anchor_checked: bool
    issues: list[str]


# Pairs checked by the drift test: (pydantic model, contract class name).
CONTRACT_MODELS: tuple[tuple[type[BaseModel], str], ...] = (
    (WorkflowIdentityModel, "WorkflowIdentity"),
    (RetrieverDescriptorModel, "RetrieverDescriptor"),
    (AliasEvidenceModel, "AliasEvidence"),
    (CandidateModel, "Candidate"),
    (RetrievalResultModel, "RetrievalResult"),
    (ReviewPacketModel, "ReviewPacket"),
    (ReviewSubmissionModel, "ReviewSubmission"),
    (AcceptedReviewModel, "AcceptedReview"),
    (CancellationModel, "Cancellation"),
    (ExplanationModel, "Explanation"),
    (CaseHeaderModel, "CaseHeader"),
    (AuditReceiptModel, "AuditReceipt"),
    (CaseViewModel, "CaseView"),
    (CaseSummaryModel, "CaseSummary"),
    (AuditEventModel, "AuditEvent"),
    (AuditViewModel, "AuditView"),
    (AuditVerificationModel, "AuditVerification"),
)


# -- API envelopes ------------------------------------------------------------------


class CaseResponse(_Wire):
    """One case. ``revision`` is an opaque token (SHA-256 of the case view)."""

    case: CaseViewModel
    revision: str
    limitation: str


class CaseListResponse(_Wire):
    """A catalogue page without names, evidence or notes."""

    cases: list[CaseSummaryModel]
    limit: int
    offset: int
    next_offset: int | None
    limitation: str


class AuditResponse(_Wire):
    audit: AuditViewModel
    limitation: str


class VerificationResponse(_Wire):
    """Local tamper-evidence check; ``ok`` is not authentication."""

    verification: AuditVerificationModel
    limitation: str


class EntityAliasModel(_Wire):
    alias_id: str
    name: str
    evidence_id: str


class EntityModel(_Wire):
    """A fictional reference record; a shared name does not establish identity."""

    entity_id: str
    name: str
    entity_type: str
    synthetic: bool
    source: str
    attribution: str
    aliases: list[EntityAliasModel]
    provenance: dict[str, Any] | None
    dataset: str
    reference_sha256: str


class EntityResponse(_Wire):
    entity: EntityModel
    limitation: str


class HealthResponse(_Wire):
    status: Literal["ok"]
    api_version: str
    research_only: bool
    limitation: str


class ErrorResponse(_Wire):
    """Every error: a stable ``code``, readable ``message``, ``retryable`` and ``details``."""

    code: str
    message: str
    retryable: bool
    details: dict[str, Any]
