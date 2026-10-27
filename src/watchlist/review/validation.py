"""Semantic validation for v2 review inputs and evidence, before any storage effect.

Structural JSON checks belong to ``contracts``. These functions add rules that
depend on the workflow policy or on saved evidence: name normalization, allowed
origins, bounded reasons, candidate/evidence linkage and binding a submission
to the exact packet the reviewer saw. Every failure is a ``ValidationError``
(or ``StaleSubmissionError`` for an outdated packet), never a partial write.
"""

from __future__ import annotations

from typing import Any, Mapping
import unicodedata

from watchlist.normalize import normalize_name
from watchlist.review.contracts import (
    ORIGINS,
    ContextProvider,
    OperationsSettings,
    RetrievalResult,
    RetrieverDescriptor,
    ReviewPacket,
    ReviewSubmission,
    StaleSubmissionError,
    ValidationError,
    WorkflowPolicy,
    canonical,
    choice,
    json_value,
    provider_name,
    text,
    uuid_text,
)


# Bidirectional embedding/override/isolate controls can make stored text display
# differently from its characters; real names and labels do not need them.
_BIDI_CONTROLS = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")
_LINE_CONTROLS = frozenset("\n\r\t")


def visible_text(
    value: str, label: str, *, allow_format: bool, allow_lines: bool = False
) -> str:
    """Reject control characters and text without any visible character.

    Format characters (Unicode ``Cf``) are allowed only where scripts need
    them (``allow_format``, e.g. ZWJ/ZWNJ in names); bidi controls never are.
    """
    for char in value:
        category = unicodedata.category(char)
        if category == "Cc" and not (allow_lines and char in _LINE_CONTROLS):
            raise ValidationError(f"{label} must not contain control characters")
        if char in _BIDI_CONTROLS or (category == "Cf" and not allow_format):
            raise ValidationError(
                f"{label} must not contain invisible format characters"
            )
    if not any(unicodedata.category(char)[0] in "LNPS" for char in value):
        raise ValidationError(f"{label} must contain at least one visible character")
    return value


def validate_name(name: object, policy: WorkflowPolicy) -> tuple[str, str]:
    """Return the original and normalized name; blank/oversized names are rejected.

    Unlike v1, invalid names are rejected before a case exists, so interfaces
    can return a plain input error and no audit record describes a non-request.
    """
    visible_text(
        text(name, "name", limit=policy.name_max_chars), "name", allow_format=True
    )
    try:
        normalized = normalize_name(name)
    except ValueError as exc:
        raise ValidationError(f"name: {exc}") from None
    return name, normalized


def validate_origin(origin: object, policy: WorkflowPolicy) -> str:
    """Origin is mandatory and explicit; there is no silent default."""
    choice(origin, "origin", ORIGINS)
    if origin not in policy.allowed_origins:
        raise ValidationError(
            "origin is not allowed by this workflow policy",
            details={"allowed": list(policy.allowed_origins)},
        )
    return origin


def validate_actor(actor: object, policy: WorkflowPolicy) -> str:
    """Actor is an attribution label from the interface, not authentication."""
    return visible_text(
        text(actor, "actor", limit=policy.actor_max_chars), "actor", allow_format=False
    )


def validate_reason(reason: object, policy: WorkflowPolicy) -> str:
    """Free-text opinion; line breaks and tabs are allowed, other controls are not."""
    return visible_text(
        text(reason, "reason", limit=policy.reason_max_chars),
        "reason",
        allow_format=True,
        allow_lines=True,
    )


def validate_case_id(case_id: object) -> str:
    return uuid_text(case_id, "case_id")


def validate_request_id(request_id: object) -> str | None:
    """Optional client UUID for idempotent ``start_case`` retries."""
    return None if request_id is None else uuid_text(request_id, "request_id")


DEFAULT_PAGE = 50


def validate_page(
    limit: object, offset: object, operations: OperationsSettings, origin: object = None
) -> tuple[int, int, str | None]:
    """Bounded pagination and an optional origin filter for catalogue listings.

    ``limit=None`` means ``min(50, page_max)``, so the default never exceeds a
    smaller configured page size.
    """
    if limit is None:
        limit = min(DEFAULT_PAGE, operations.page_max)
    if type(limit) is not int or not 1 <= limit <= operations.page_max:
        raise ValidationError(
            f"limit must be an integer from 1 to {operations.page_max}"
        )
    if type(offset) is not int or not 0 <= offset <= 10**9:
        raise ValidationError("offset must be a nonnegative integer")
    if origin is not None:
        choice(origin, "origin", ORIGINS)
    return limit, offset, origin


def coerce_submission(value: object, policy: WorkflowPolicy) -> ReviewSubmission:
    """Accept a ``ReviewSubmission`` or its JSON object; apply policy bounds."""
    submission = (
        value
        if isinstance(value, ReviewSubmission)
        else ReviewSubmission.from_dict(value)
    )
    validate_reason(submission.reason, policy)
    validate_actor(submission.actor, policy)
    validate_origin(submission.origin, policy)
    return submission


def validate_retrieval(
    result: object, *, expected: RetrieverDescriptor, top_k: int
) -> RetrievalResult:
    """Check one retriever result before it becomes durable evidence.

    Requires the frozen retriever identity, at most ``top_k`` consecutive
    ranks, unique entity/evidence IDs, finite scores, evidence linked only to
    its own entity, the winning alias among the linked evidence and no orphan
    evidence. An empty candidate list is a valid result that still needs review.
    """
    if not isinstance(result, RetrievalResult):
        raise ValidationError("retriever must return a RetrievalResult")
    # Round-trip through the strict parser so adapters cannot smuggle non-JSON values.
    result = RetrievalResult.from_dict(result.to_dict())
    if result.retriever != expected:
        raise ValidationError("retrieval descriptor differs from the case retriever")
    if result.top_k != top_k or len(result.candidates) > top_k:
        raise ValidationError("retrieval exceeds the approved top_k")
    by_id = {}
    for evidence in result.evidence:
        if evidence.evidence_id in by_id:
            raise ValidationError("duplicate evidence ID")
        if not evidence.synthetic and evidence.provenance is None:
            raise ValidationError("non-synthetic evidence requires provenance")
        by_id[evidence.evidence_id] = evidence
    seen, used = set(), set()
    for rank, candidate in enumerate(result.candidates, 1):
        if candidate.rank != rank or candidate.entity_id in seen:
            raise ValidationError("duplicate entity or non-consecutive rank")
        seen.add(candidate.entity_id)
        refs = candidate.evidence_ids
        if not refs or len(set(refs)) != len(refs):
            raise ValidationError(
                "candidate evidence links must be unique and nonempty"
            )
        linked = [by_id.get(ref) for ref in refs]
        if any(
            item is None or item.entity_id != candidate.entity_id for item in linked
        ):
            raise ValidationError(
                "candidate links evidence of another or unknown entity"
            )
        if not any(
            item.alias_id == candidate.matched_alias_id
            and item.original_text == candidate.matched_alias
            for item in linked
        ):
            raise ValidationError("winning alias is missing from candidate evidence")
        used.update(refs)
    if used != set(by_id):
        raise ValidationError("retrieval contains orphan evidence")
    return result


def validate_providers(providers: object) -> tuple[ContextProvider, ...]:
    """Require unique, well-named providers that implement the hook protocol."""
    if not isinstance(providers, (list, tuple)):
        raise ValidationError("context_providers must be a list")
    names = set()
    for provider in providers:
        if not isinstance(provider, ContextProvider):
            raise ValidationError(
                "context provider must define name, version and provide()"
            )
        provider_name(provider.name)
        text(provider.version, "context provider version", limit=100)
        if provider.name in names:
            raise ValidationError("duplicate context provider name")
        names.add(provider.name)
    return tuple(providers)


def validate_context(name: str, value: object, max_bytes: int) -> dict[str, Any]:
    """A provider result must be a finite JSON object within the size budget."""
    data = json_value(value, f"context {name}")
    if not isinstance(data, dict):
        raise ValidationError(f"context {name} must be a JSON object")
    if len(canonical(data).encode("utf-8")) > max_bytes:
        raise ValidationError(f"context {name} exceeds {max_bytes} bytes")
    return data


def validate_submission_for_packet(
    submission: ReviewSubmission, packet: ReviewPacket, packet_sha256: str
) -> None:
    """Bind the form to the saved packet, its origin and its shortlist.

    A different packet digest means the reviewer saw other evidence: the form is
    stale and must not be applied. The review origin must equal the case
    origin, so demo or test cases can never be counted as human-reviewed
    evidence (and vice versa). A follow-up candidate must be in the saved
    shortlist; ``none_selected`` and ``unresolved`` require a null candidate.
    """
    if submission.packet_sha256 != packet_sha256:
        raise StaleSubmissionError(
            "submission refers to a different review packet",
            details={"expected_packet_sha256": packet_sha256},
        )
    if submission.origin != packet.origin:
        raise ValidationError(
            "submission origin must equal the case origin",
            details={
                "case_origin": packet.origin,
                "submission_origin": submission.origin,
            },
        )
    if submission.outcome == "candidate_for_follow_up":
        shortlist = {c.entity_id for c in packet.retrieval.candidates}
        if submission.candidate_entity_id not in shortlist:
            raise ValidationError("candidate_entity_id is not in the saved shortlist")


def same_submission(
    left: ReviewSubmission, right: Mapping[str, Any] | ReviewSubmission
) -> bool:
    """Compare caller-owned fields for idempotent replay detection."""
    other = (
        right
        if isinstance(right, ReviewSubmission)
        else ReviewSubmission.from_dict(right)
    )
    return left.to_dict() == other.to_dict()
