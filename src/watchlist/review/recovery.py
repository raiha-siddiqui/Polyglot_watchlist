"""Pure case assessment and recovery planning for the v2 review workflow.

``assess_case`` interprets one case's raw ledger rows and checkpoint facts. It
never writes, never repairs and never calls a retriever. It returns a status:

=================  =============================================================
preparing          request saved; retrieval/packet/interrupt not finished
awaiting_review    packet saved and the human-review interrupt is pending
finalizing         a review was accepted but the final audit is not complete
completed          final audit event and final checkpoint receipt agree
cancelled          explicitly cancelled before any review was accepted
incompatible       unfinished, but saved under another workflow identity
inconsistent       saved records, audit chain or checkpoint disagree
=================  =============================================================

``plan_recovery`` chooses the single legitimate continuation. Crash windows
are listed in docs/adr/0007-review-service-v2.md. After evidence exists no
plan can re-run retrieval: the retrieve node itself reuses ledger evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Literal

from watchlist.review.contracts import (
    AcceptedReview,
    AuditReceipt,
    Cancellation,
    CaseHeader,
    Explanation,
    InvalidStateError,
    ReviewError,
    RetrievalResult,
    ReviewPacket,
    StorageIntegrityError,
    VersionMismatchError,
    WorkflowIdentity,
    digest,
)
from watchlist.review.storage import LedgerSnapshot, verify_chain

RecoveryAction = Literal["none", "start", "continue", "resume_review"]
REVIEW_NODE = "human_review"


@dataclass(frozen=True)
class CheckpointFacts:
    """What the latest durable checkpoint says, read without running nodes."""

    exists: bool
    checkpoint_id: str | None = None
    next: tuple[str, ...] = ()
    interrupted: bool = False
    has_task_results: bool = False
    values: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Assessment:
    """Interpreted case state; parsed records are None when absent or unreadable."""

    case_id: str
    status: str
    next_action: str
    recovery_required: bool
    compatible: bool
    compatibility_issues: tuple[str, ...]
    issues: tuple[str, ...]
    header: CaseHeader | None
    evidence: RetrievalResult | None
    packet: ReviewPacket | None
    packet_sha256: str | None
    review: AcceptedReview | None
    cancellation: Cancellation | None
    explanation: Explanation | None
    final_receipt: AuditReceipt | None


def final_payload(
    packet_sha256: str, review: dict[str, Any], explanation: dict[str, Any]
) -> dict[str, Any]:
    """The final event binds packet, accepted review and explanation together."""
    return {
        "packet_sha256": packet_sha256,
        "review_sha256": digest(review),
        "explanation": explanation,
    }


def _parse(row: dict[str, Any] | None, label: str, parser, issues: list[str]) -> Any:
    if row is None:
        return None
    try:
        value = json.loads(row["payload_json"])
        if digest(value) != row["sha256"]:
            issues.append(f"{label}: checksum mismatch")
            return None
        return parser(value)
    except (ValueError, ReviewError) as exc:
        issues.append(f"{label}: unreadable ({exc})")
        return None


def assess_case(
    snapshot: LedgerSnapshot, facts: CheckpointFacts, identity: WorkflowIdentity
) -> Assessment:
    """Check ledger/audit/checkpoint agreement and derive status and next action."""
    case_id = snapshot.case_row["case_id"]
    issues: list[str] = []
    header = None
    try:
        header_value = json.loads(snapshot.case_row["header_json"])
        if digest(header_value) != snapshot.case_row["header_sha256"]:
            issues.append("case header: checksum mismatch")
        header = CaseHeader.from_dict(header_value)
        row = snapshot.case_row
        if (header.case_id, header.thread_id, header.request_id, header.origin) != (
            case_id,
            row["thread_id"],
            row["request_id"],
            row["origin"],
        ):
            issues.append("case header: identity differs from catalogue row")
    except (ValueError, ReviewError) as exc:
        issues.append(f"case header: unreadable ({exc})")

    evidence = _parse(
        snapshot.evidence_row, "evidence", RetrievalResult.from_dict, issues
    )
    packet = _parse(snapshot.packet_row, "packet", ReviewPacket.from_dict, issues)
    review = _parse(snapshot.submission_row, "review", AcceptedReview.from_dict, issues)
    cancellation = _parse(
        snapshot.cancellation_row, "cancellation", Cancellation.from_dict, issues
    )
    packet_sha = snapshot.packet_row["sha256"] if snapshot.packet_row else None

    # Audit chain and the one-to-one correspondence of events and records.
    rows = list(snapshot.events)
    _, _, chain_issues = verify_chain(rows, case_id)
    issues.extend(chain_issues)
    events: dict[str, Any] = {}
    for row in rows:
        try:
            events[row["event_type"]] = (row, json.loads(row["payload_json"]))
        except ValueError:
            pass
    if not rows or rows[0]["event_type"] != "case_created":
        issues.append("audit: first event must be case_created")
    expected_events = {
        "case_created": header.to_dict() if header else None,
        "evidence_recorded": {"evidence_sha256": snapshot.evidence_row["sha256"]}
        if snapshot.evidence_row
        else None,
        "review_ready": {"packet_sha256": packet_sha} if packet_sha else None,
        "review_submitted": {"review": review.to_dict()} if review else None,
        "case_cancelled": {"cancellation": cancellation.to_dict()}
        if cancellation
        else None,
    }
    for event_type, expected in expected_events.items():
        saved = events.get(event_type)
        if (saved is None) != (expected is None):
            issues.append(f"audit: {event_type} event and saved record disagree")
        elif saved is not None and saved[1] != expected:
            issues.append(f"audit: {event_type} payload differs from saved record")
    known = set(expected_events) | {"case_finalized"}
    if set(events) - known:
        issues.append("audit: unknown event type")

    # Record-level relationships.
    if header and evidence and evidence.retriever != header.retriever:
        issues.append("evidence: retriever differs from case header")
    if packet:
        if evidence is None or packet.retrieval != evidence:
            issues.append("packet: retrieval differs from saved evidence")
        if header and (packet.case_id, packet.workflow, packet.name, packet.origin) != (
            header.case_id,
            header.workflow,
            header.name,
            header.origin,
        ):
            issues.append("packet: header fields differ from case")
    if review:
        if (
            packet is None
            or review.case_id != case_id
            or review.submission.packet_sha256 != packet_sha
        ):
            issues.append("review: not bound to the saved packet")
        elif review.submission.origin != packet.origin:
            issues.append("review: origin differs from the case origin")
        elif (
            review.submission.outcome == "candidate_for_follow_up"
            and review.submission.candidate_entity_id
            not in {c.entity_id for c in packet.retrieval.candidates}
        ):
            issues.append("review: candidate is not in the saved shortlist")
    if cancellation and (review or "case_finalized" in events):
        issues.append("cancellation: conflicts with an accepted review or final event")
    final = events.get("case_finalized")
    if final and review is None:
        issues.append("final event: no accepted review")

    # Checkpoint agreement. The ledger is the authority; a checkpoint may lag
    # one durable step behind it, but may never contradict it.
    values = facts.values
    if (evidence or packet or review or final) and not facts.exists:
        issues.append("checkpoint: durable progress exists without checkpoint history")
    if values.get("case_id") not in (None, case_id):
        issues.append("checkpoint: belongs to another case")
    if values.get("evidence_sha256") is not None and values["evidence_sha256"] != (
        snapshot.evidence_row["sha256"] if snapshot.evidence_row else None
    ):
        issues.append("checkpoint: evidence digest differs from ledger")
    if (
        values.get("packet_sha256") is not None
        and packet_sha is not None
        and values["packet_sha256"] != packet_sha
    ):
        issues.append("checkpoint: packet digest differs from ledger")
    if values.get("review_status") in ("pending", "recorded") and packet is None:
        issues.append("checkpoint: review requested without a saved packet")
    if values.get("review") is not None and (
        review is None or values["review"] != review.to_dict()
    ):
        issues.append("checkpoint: review differs from accepted submission")
    explanation = None
    final_receipt = None
    if final:
        final_row, final_value = final
        final_receipt = AuditReceipt(
            case_id, final_row["sequence"], final_row["event_sha256"]
        )
        try:
            explanation = Explanation.from_dict(final_value.get("explanation"))
            if review is None or final_value != final_payload(
                packet_sha, review.to_dict(), explanation.to_dict()
            ):
                issues.append("final event: payload differs from packet/review")
        except (ReviewError, AttributeError) as exc:
            issues.append(f"final event: unreadable ({exc})")
        if values.get("explanation") is not None and values[
            "explanation"
        ] != final_value.get("explanation"):
            issues.append("checkpoint: explanation differs from final event")
    elif values.get("explanation") is not None:
        try:
            explanation = Explanation.from_dict(values["explanation"])
        except ReviewError as exc:
            issues.append(f"checkpoint: unreadable explanation ({exc})")
    graph_done = facts.exists and not facts.next
    if values.get("audit_status") == "committed":
        if final_receipt is None or (
            values.get("final_event_sha256"),
            values.get("final_event_sequence"),
        ) != (final_receipt.event_sha256, final_receipt.sequence):
            issues.append("checkpoint: final receipt differs from audit")
    if graph_done and values.get("audit_status") != "committed":
        issues.append("checkpoint: graph ended without a committed audit")

    compatibility: list[str] = []
    if header and header.workflow != identity:
        compatibility.append(
            "workflow identity differs: "
            f"case {header.workflow.version}/{header.workflow.config_sha256[:12]}, "
            f"service {identity.version}/{identity.config_sha256[:12]}"
        )
    compatible = not compatibility

    if issues:
        status, action, recovery = "inconsistent", "none", False
    elif cancellation:
        status, action, recovery = "cancelled", "none", False
    elif final and graph_done:
        status, action, recovery = "completed", "none", False
    elif review:
        status, action, recovery = "finalizing", "recover", True
    elif packet and facts.interrupted and facts.next == (REVIEW_NODE,):
        status, action, recovery = "awaiting_review", "submit_review", False
    else:
        status, action, recovery = "preparing", "recover", True
    if not compatible and status in ("preparing", "awaiting_review", "finalizing"):
        status, action = "incompatible", "none"
    return Assessment(
        case_id,
        status,
        action,
        recovery,
        compatible,
        tuple(compatibility),
        tuple(issues),
        header,
        evidence,
        packet,
        packet_sha,
        review,
        cancellation,
        explanation,
        final_receipt,
    )


def require_consistent(assessment: Assessment) -> None:
    if assessment.issues:
        raise StorageIntegrityError(
            "case storage is inconsistent; preserve the workspace and investigate",
            details={"case_id": assessment.case_id, "issues": list(assessment.issues)},
        )


def require_compatible(assessment: Assessment) -> None:
    if not assessment.compatible:
        raise VersionMismatchError(
            "case was created under another workflow identity; use the original "
            "workflow version and config, or cancel the case",
            details={
                "case_id": assessment.case_id,
                "issues": list(assessment.compatibility_issues),
            },
        )


def plan_recovery(assessment: Assessment, facts: CheckpointFacts) -> RecoveryAction:
    """Choose the one legitimate continuation for a consistent, compatible case.

    * no checkpoint: start from the saved request (retrieval reuses any evidence);
    * a saved task result (including a consumed human resume): continue with None;
    * pending human interrupt: resume with the accepted review, else nothing;
    * any other unfinished checkpoint: continue with None.
    """
    require_consistent(assessment)
    if assessment.status in ("completed", "cancelled"):
        return "none"
    require_compatible(assessment)
    if not facts.exists:
        return "start"
    if facts.has_task_results:
        return "continue"
    if facts.interrupted:
        return "resume_review" if assessment.review is not None else "none"
    if facts.next:
        return "continue"
    raise InvalidStateError(
        "case has no recoverable work", details={"case_id": assessment.case_id}
    )
