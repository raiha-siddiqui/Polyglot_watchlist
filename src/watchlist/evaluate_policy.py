"""Select and evaluate the review-attention policy on threshold-selection rows only.

Threshold search uses a predeclared finite grid on ``threshold_selection``
groups, never ``calibration_fit`` or ``test``. Precision and coverage carry
Wilson score intervals. Those intervals are unadjusted for the grid search and
assume independent rows, so they are descriptive; independent confirmation is
a later, separate step. Coverage is simulated suggestion coverage, not measured
workload saved. On fictional rows every number is regression-only evidence.
"""

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import json
import math
from pathlib import Path

from watchlist.calibration import (
    FIT_PARTITION,
    canonical_rows,
    diagnostics,
    load_calibrator,
    load_rows,
    require_disjoint_from_fit,
    require_partition,
)
from watchlist.model_artifacts import atomic_write_json, canonical_digest
from watchlist.review_policy import (
    FLAGS,
    POLICY_VERSION,
    assess,
    freeze_policy,
    load_policy,
)
from watchlist.splits import PARTITION_NAMES

SELECTION_PARTITION = "threshold_selection"
# Exact, case-sensitive allowlist; the final test partition is reserved for frozen confirmation.
EVALUABLE_PARTITIONS = frozenset(PARTITION_NAMES - {"test"})
Z_95 = 1.959963984540054


class PolicySelectionError(ValueError):
    """Refusal for wrong partitions or an invalid selection configuration."""


def wilson_interval(
    successes: int, total: int, z: float = Z_95
) -> tuple[float | None, float | None]:
    """Two-sided Wilson score interval; ``(None, None)`` when there is no denominator."""
    if (
        type(successes) is not int
        or type(total) is not int
        or not 0 <= successes <= total
    ):
        raise PolicySelectionError(
            "Wilson interval needs integers 0 <= successes <= total"
        )
    if total == 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    )
    # Exact endpoints at 0 and n avoid floating-point bounds such as 0.9999999999999999.
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == total else min(1.0, centre + half)
    return low, high


def _rate(successes: int, total: int) -> dict:
    low, high = wilson_interval(successes, total)
    return {
        "value": successes / total if total else None,
        "numerator": successes,
        "denominator": total,
        "wilson_95": [low, high],
    }


def _grid(values: object) -> list[float]:
    if (
        not isinstance(values, list)
        or not values
        or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1
            for v in values
        )
    ):
        raise PolicySelectionError(
            "threshold grid must be a nonempty list of probabilities"
        )
    if values != sorted(set(values)):
        raise PolicySelectionError("threshold grid must be strictly increasing")
    return [float(v) for v in values]


def grid_report(
    supports: Sequence[float], labels: Sequence[int], grid: Sequence[float]
) -> list[dict]:
    """Coverage, precision and below-threshold positives at each grid value."""
    total, positives = len(labels), sum(labels)
    rows = []
    for threshold in grid:
        chosen = [y for s, y in zip(supports, labels) if s >= threshold]
        below_positive = sum(y for s, y in zip(supports, labels) if s < threshold)
        rows.append(
            {
                "threshold": threshold,
                "coverage": _rate(len(chosen), total),
                "precision": _rate(sum(chosen), len(chosen)),
                "positives_below": _rate(below_positive, positives),
            }
        )
    return rows


def select_policy(
    rows: Sequence[Mapping], calibration_artifact: Mapping, config: Mapping
) -> tuple[dict, dict]:
    """Choose priority and insufficient-support thresholds, then freeze the policy.

    Priority: the smallest grid value with at least ``min_selected`` rows whose
    precision Wilson lower bound reaches ``target_precision_lower``. Insufficient
    support: the largest grid value (not above priority) whose share of positive
    rows falling below it has a Wilson upper bound within
    ``max_positive_below_upper``. If no value qualifies, the band is disabled.
    """
    require_partition(rows, SELECTION_PARTITION)
    require_disjoint_from_fit(rows, calibration_artifact)
    rows = canonical_rows(rows)
    grid = _grid(config.get("threshold_grid"))
    calibrator = load_calibrator(calibration_artifact)
    supports = [float(v) for v in calibrator.predict([row["score"] for row in rows])]
    labels = [int(row["label"]) for row in rows]
    table = grid_report(supports, labels, grid)
    target, minimum = config["target_precision_lower"], config["min_selected"]
    priority = next(
        (
            r["threshold"]
            for r in table
            if r["coverage"]["numerator"] >= minimum
            and r["precision"]["wilson_95"][0] is not None
            and r["precision"]["wilson_95"][0] >= target
        ),
        None,
    )
    ceiling = priority if priority is not None else 1.0
    allowed = [
        r["threshold"]
        for r in table
        if r["threshold"] <= ceiling
        and r["positives_below"]["wilson_95"][1] is not None
        and r["positives_below"]["wilson_95"][1] <= config["max_positive_below_upper"]
    ]
    lower = max(allowed) if allowed else 0.0
    selection = {
        "partition": SELECTION_PARTITION,
        "rows": len(rows),
        "groups": len({r["group_id"] for r in rows}),
        "input_digest": canonical_digest(rows),
        "grid": grid,
        "rule": {
            k: config[k]
            for k in (
                "target_precision_lower",
                "min_selected",
                "max_positive_below_upper",
            )
        },
        "priority_status": "selected"
        if priority is not None
        else "no_threshold_supported",
        "multiplicity": "Wilson bounds unadjusted for the grid search; independent confirmation required",
    }
    policy = freeze_policy(
        {
            "version": POLICY_VERSION,
            "policy_id": config["policy_id"],
            "calibration_bundle_digest": calibration_artifact["bundle_digest"],
            "priority_threshold": priority,
            "insufficient_below": lower,
            "supported_domains": list(config["supported_domains"]),
            "supported_score_kinds": list(config["supported_score_kinds"]),
            "threshold_partition": SELECTION_PARTITION,
            "selection": selection,
            "evidence": config.get(
                "evidence", "fictional/synthetic regression-only evidence"
            ),
        },
        calibration_artifact,
    )
    held_out = {
        **diagnostics(supports, labels, config.get("n_bins", 10)),
        "note": "calibrator fitted on calibration_fit; these rows were not used for fitting",
    }
    return policy, {
        "grid": table,
        "selection": selection,
        "calibration_diagnostics": held_out,
    }


def evaluate(
    rows: Sequence[Mapping],
    calibration_artifact: Mapping,
    policy_manifest: Mapping,
    partition: str = SELECTION_PARTITION,
) -> dict:
    """Apply a frozen policy and report per-flag coverage and precision with intervals.

    Absent-target rows that reach ``priority_review`` and present-target rows
    flagged ``insufficient_support`` are reported separately, because a low
    flag cannot establish that no target exists.
    """
    if partition not in EVALUABLE_PARTITIONS:
        raise PolicySelectionError(
            f"partition {partition!r} is not evaluable here (allowed: "
            f"{sorted(EVALUABLE_PARTITIONS)}); final test evaluation is reserved "
            "for the frozen confirmation step"
        )
    require_partition(rows, partition)
    if partition != FIT_PARTITION:
        require_disjoint_from_fit(rows, calibration_artifact)
    policy = load_policy(policy_manifest)
    by_id = {str(row["query_id"]): row for row in rows}
    if len(by_id) != len(rows):
        raise PolicySelectionError("query_id values must be unique")
    assessed = assess(policy, calibration_artifact, rows)
    flags = Counter(record["flag"] for record in assessed)
    report = {
        "partition": partition,
        "rows": len(rows),
        "policy_digest": policy.digest,
        "groups": len({row["group_id"] for row in rows}),
        "flags": {},
        "in_sample_selection": partition == policy.threshold_partition,
        "in_sample_calibration": partition == FIT_PARTITION,
    }
    for flag in FLAGS:
        members = [by_id[r["query_id"]] for r in assessed if r["flag"] == flag]
        report["flags"][flag] = {
            "coverage": _rate(flags[flag], len(rows)),
            "selected_record_correct": _rate(
                sum(int(m["label"]) for m in members), len(members)
            ),
        }
    present = [r for r in assessed if by_id[r["query_id"]].get("presence") == "present"]
    absent = [r for r in assessed if by_id[r["query_id"]].get("presence") == "absent"]
    report["absent_reaching_priority"] = _rate(
        sum(r["flag"] == "priority_review" for r in absent), len(absent)
    )
    report["present_flagged_insufficient"] = _rate(
        sum(r["flag"] == "insufficient_support" for r in present), len(present)
    )
    report["human_review_required_for_all"] = all(
        r["human_review_required"] for r in assessed
    )
    report["statement"] = (
        "Fictional-only evidence: rows come from generated names and a tiny in-repo "
        "model. Coverage is simulated suggestion coverage, not workload saved; "
        "no flag is a match, clear or identity decision."
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["select"])
    parser.add_argument("--partition", required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if args.partition != SELECTION_PARTITION:
        parser.error(
            "thresholds may be selected only on the threshold_selection partition"
        )
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        calibration = json.loads(
            Path(config["calibration_path"]).read_text(encoding="utf-8")
        )
        rows = [
            r
            for r in load_rows(Path(config["rows_path"]))
            if r.get("partition") == args.partition
        ]
        policy, selection = select_policy(rows, calibration, config)
        report = evaluate(rows, calibration, policy, args.partition)
        output = Path(config["output_dir"])
        atomic_write_json(output / "review-policy.json", policy)
        atomic_write_json(
            output / "policy-selection-report.json", {**selection, "evaluation": report}
        )
    except (OSError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "priority_threshold": policy["priority_threshold"],
                "insufficient_below": policy["insufficient_below"],
                "digest": policy["digest"],
                "evaluation": report,
            },
            indent=2,
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
