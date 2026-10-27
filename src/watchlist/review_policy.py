"""Frozen review-attention policy: it may only order and flag cases for humans.

The policy never emits match, clear, identity, wrongdoing or sanctions-status
decisions, and it never chooses a reviewer outcome (``candidate_for_follow_up``,
``none_selected`` and ``unresolved`` remain human choices). Its three flags
describe how much calibrated support exists for the *selected record*:

* ``priority_review`` - higher calibrated support; review this possible record first.
* ``standard_review`` - intermediate support; ordinary human review.
* ``insufficient_support`` - low support, unsupported domain/score, truncation,
  stale provenance or an upstream failure. This never means "no target exists".

Every output keeps ``human_review_required: true``.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

from watchlist.calibration import CalibrationError, load_calibrator
from watchlist.model_artifacts import canonical_digest

POLICY_VERSION = "review-policy-v2"
FLAGS = ("priority_review", "standard_review", "insufficient_support")
# Words that would turn an attention flag into an automated decision.
FORBIDDEN_TERMS = frozenset(
    {
        "match",
        "matched",
        "confirmed_match",
        "identity_match",
        "clear",
        "cleared",
        "auto_clear",
        "auto_match",
        "no_match",
        "not_a_match",
        "sanctioned",
        "not_sanctioned",
        "wrongdoing",
        "approve",
        "approved",
        "reject",
        "rejected",
        "block",
        "blocked",
        "decision",
        "candidate_for_follow_up",
        "none_selected",
        "unresolved",
    }
)
POLICY_FIELDS = (
    "version",
    "policy_id",
    "calibration_bundle_digest",
    "priority_threshold",
    "insufficient_below",
    "supported_domains",
    "supported_score_kinds",
    "threshold_partition",
    "selection",
    "evidence",
)


class PolicyError(ValueError):
    """Invalid, tampered or decision-making policy content."""


class StaleBundleError(PolicyError):
    """The supplied calibrator is not the one this policy was frozen against."""


@dataclass(frozen=True)
class ReviewPolicy:
    version: str
    policy_id: str
    calibration_bundle_digest: str
    priority_threshold: float | None
    insufficient_below: float
    supported_domains: tuple[str, ...]
    supported_score_kinds: tuple[str, ...]
    threshold_partition: str
    selection: dict
    evidence: str
    digest: str


def _threshold(value: object, name: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise PolicyError(f"{name} must be a probability in [0, 1]")
    return float(value)


def _contains_forbidden(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_forbidden(k) or _contains_forbidden(v) for k, v in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item) for item in value)
    return isinstance(value, str) and value.lower() in FORBIDDEN_TERMS


def calibrated_scope(
    policy: ReviewPolicy, calibration_artifact: Mapping
) -> tuple[set[str], set[str]]:
    """Domains and score kinds that are both enabled by the policy and seen when fitting.

    A policy may not enable a domain or score kind the calibrator never saw:
    calibrated support outside the fitted population would look informative
    without any evidence behind it.
    """
    domains = calibration_artifact.get("domains")
    kinds = calibration_artifact.get("score_kinds")
    if not isinstance(domains, Mapping) or not isinstance(kinds, (list, tuple)):
        raise PolicyError(
            "calibrator does not record its fitted domains and score kinds"
        )
    unseen = sorted(set(policy.supported_domains) - set(domains))
    unseen += sorted(set(policy.supported_score_kinds) - set(kinds))
    if unseen:
        raise PolicyError(
            f"policy enables domains/score kinds the calibrator never saw: {unseen}"
        )
    return set(policy.supported_domains), set(policy.supported_score_kinds)


def freeze_policy(body: Mapping, calibration_artifact: Mapping) -> dict:
    """Validate and seal a policy body against its calibrator; the digest covers every field."""
    policy = load_policy({**body, "digest": canonical_digest(dict(body))})
    if calibration_artifact.get("bundle_digest") != policy.calibration_bundle_digest:
        raise StaleBundleError(
            "policy names a different calibrator than the one supplied"
        )
    try:
        load_calibrator(calibration_artifact)
    except CalibrationError as exc:
        raise StaleBundleError(str(exc)) from exc
    calibrated_scope(policy, calibration_artifact)
    return {
        **{
            name: getattr(policy, name)
            if not isinstance(getattr(policy, name), tuple)
            else list(getattr(policy, name))
            for name in POLICY_FIELDS
        },
        "digest": policy.digest,
    }


def load_policy(manifest: Mapping) -> ReviewPolicy:
    """Verify a frozen manifest. Unknown fields (e.g. an auto-clear override) are refused."""
    if not isinstance(manifest, Mapping) or set(manifest) != {*POLICY_FIELDS, "digest"}:
        raise PolicyError(
            "policy fields differ from the frozen schema; overrides are not accepted"
        )
    body = {key: manifest[key] for key in POLICY_FIELDS}
    if canonical_digest(body) != manifest["digest"]:
        raise PolicyError("policy digest mismatch; the frozen policy was modified")
    if (
        manifest["version"] != POLICY_VERSION
        or manifest["threshold_partition"] != "threshold_selection"
    ):
        raise PolicyError("unsupported policy version or threshold partition")
    if _contains_forbidden(body):
        raise PolicyError("policy content contains an automated decision term")
    priority = _threshold(
        manifest["priority_threshold"], "priority_threshold", optional=True
    )
    lower = _threshold(manifest["insufficient_below"], "insufficient_below")
    if priority is not None and lower > priority:
        raise PolicyError("insufficient_below cannot exceed priority_threshold")
    for name in ("supported_domains", "supported_score_kinds"):
        values = manifest[name]
        if (
            not isinstance(values, (list, tuple))
            or not values
            or not all(isinstance(v, str) and v for v in values)
        ):
            raise PolicyError(f"{name} must be a nonempty list of names")
    if (
        not isinstance(manifest["calibration_bundle_digest"], str)
        or len(manifest["calibration_bundle_digest"]) != 64
    ):
        raise PolicyError("calibration_bundle_digest must be a SHA-256 hex digest")
    return ReviewPolicy(
        manifest["version"],
        str(manifest["policy_id"]),
        manifest["calibration_bundle_digest"],
        priority,
        lower,
        tuple(manifest["supported_domains"]),
        tuple(manifest["supported_score_kinds"]),
        manifest["threshold_partition"],
        dict(manifest["selection"]),
        str(manifest["evidence"]),
        manifest["digest"],
    )


def _assert_attention_only(record: Mapping) -> None:
    """Last-line guard: refuse to emit any record that carries a decision."""
    if (
        record.get("flag") not in FLAGS
        or record.get("human_review_required") is not True
    ):
        raise PolicyError(
            "policy output must be an attention flag requiring human review"
        )
    if _contains_forbidden(
        {k: v for k, v in record.items() if k not in {"query_id", "selected_entity_id"}}
    ):
        raise PolicyError("policy output contains an automated decision term")


def assess(
    policy: ReviewPolicy, calibration_artifact: Mapping, cases: Sequence[Mapping]
) -> list[dict]:
    """Flag and order cases for human attention; never decide them.

    Each case carries ``query_id``, ``selected_entity_id`` (or ``None``),
    ``score``, ``domain`` and ``score_kind`` plus optional ``truncated``,
    ``stale_provenance`` and ``error``. Thresholds use ``>=`` at boundaries.
    """
    if calibration_artifact.get("bundle_digest") != policy.calibration_bundle_digest:
        raise StaleBundleError(
            "calibrator bundle differs from the one this policy was frozen with"
        )
    try:
        calibrator = load_calibrator(calibration_artifact)
    except CalibrationError as exc:
        raise StaleBundleError(str(exc)) from exc
    domains, kinds = calibrated_scope(policy, calibration_artifact)
    output = []
    for case in cases:
        reasons, support = [], None
        if case.get("error"):
            reasons.append("upstream_failure")
        if case.get("truncated"):
            reasons.append("truncated_candidates")
        if case.get("stale_provenance"):
            reasons.append("stale_provenance")
        if case.get("domain") not in domains:
            reasons.append("unsupported_domain")
        if case.get("score_kind") not in kinds:
            reasons.append("unsupported_score_kind")
        score = case.get("score")
        if (
            case.get("selected_entity_id") is None
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            reasons.append("no_scored_record")
        if not reasons:
            support = float(calibrator.predict([float(score)])[0])
            if not math.isfinite(support) or not 0.0 <= support <= 1.0:
                # NaN compares false everywhere and would fall through to a review band.
                support = None
                reasons.append("nonfinite_calibrated_support")
            elif support < policy.insufficient_below:
                reasons.append("low_calibrated_support")
        if reasons:
            flag = "insufficient_support"
        elif (
            policy.priority_threshold is not None
            and support >= policy.priority_threshold
        ):
            flag = "priority_review"
        else:
            flag = "standard_review"
        record = {
            "query_id": str(case["query_id"]),
            "selected_entity_id": case.get("selected_entity_id"),
            "flag": flag,
            "reasons": reasons,
            "calibrated_support": support,
            "human_review_required": True,
            "policy_version": policy.version,
            "policy_digest": policy.digest,
        }
        _assert_attention_only(record)
        output.append(record)
    rank = {flag: index for index, flag in enumerate(FLAGS)}
    output.sort(
        key=lambda r: (
            rank[r["flag"]],
            -(r["calibrated_support"] or 0.0),
            r["query_id"],
        )
    )
    for position, record in enumerate(output, start=1):
        record["attention_order"] = position
    return output
