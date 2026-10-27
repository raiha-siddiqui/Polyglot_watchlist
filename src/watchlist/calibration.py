"""Partition-locked score calibration research with inspectable JSON parameters.

The calibrated event is narrow: the *selected top record belongs to the
query's independently supported target set* for a frozen pipeline and index.
It is not an identity, wrongdoing or sanctions-status probability. Fitting is
allowed only on rows labelled ``calibration_fit``; threshold work belongs to
``threshold_selection`` (see ``evaluate_policy``). Zero-support and one-class
fits are refused instead of returning a constant that looks informative.
"""

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import math
from pathlib import Path

import numpy as np

from watchlist.model_artifacts import (
    ArtifactError,
    atomic_write_json,
    canonical_digest,
    file_sha256,
    verify_artifact,
)

CALIBRATION_VERSION = "calibration-v2"
METHODS = ("platt", "isotonic")
FIT_PARTITION = "calibration_fit"


class CalibrationError(ValueError):
    """Refusal for wrong partitions, insufficient support or tampered artifacts."""


@dataclass(frozen=True)
class PlattCalibrator:
    """``sigmoid(a * score + b)`` fitted with Platt's smoothed targets."""

    a: float
    b: float

    def predict(self, scores: Sequence[float]) -> np.ndarray:
        values = _scores(scores)
        return 1.0 / (1.0 + np.exp(-(self.a * values + self.b)))

    def params(self) -> dict:
        return {"a": self.a, "b": self.b}


@dataclass(frozen=True)
class IsotonicCalibrator:
    """Nondecreasing step function from pool-adjacent-violators blocks.

    ``lower[i]`` is the smallest fitted score in block ``i``; a new score takes
    the value of the last block whose lower bound it reaches (first block below).
    """

    lower: tuple[float, ...]
    values: tuple[float, ...]

    def predict(self, scores: Sequence[float]) -> np.ndarray:
        values = _scores(scores)
        index = np.searchsorted(np.asarray(self.lower), values, side="right") - 1
        return np.asarray(self.values)[np.clip(index, 0, len(self.values) - 1)]

    def params(self) -> dict:
        return {"lower": list(self.lower), "values": list(self.values)}


def _scores(scores: Sequence[float]) -> np.ndarray:
    values = np.asarray(scores, dtype=np.float64)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise CalibrationError("scores must be a finite one-dimensional sequence")
    return values


def _labels(labels: Sequence[int]) -> np.ndarray:
    values = np.asarray(labels)
    if values.ndim != 1 or not np.isin(values, (0, 1)).all():
        raise CalibrationError("labels must be exactly 0 or 1")
    return values.astype(np.float64)


def require_support(labels: Sequence[int], min_class_count: int = 1) -> dict:
    """Refuse empty inputs, one-class inputs or classes below ``min_class_count``."""
    y = _labels(labels)
    positives, negatives = int(y.sum()), int(len(y) - y.sum())
    if not len(y):
        raise CalibrationError("zero support: no calibration rows")
    if not positives or not negatives:
        raise CalibrationError("one-class support: calibration needs both labels")
    if min(positives, negatives) < min_class_count:
        raise CalibrationError(
            f"insufficient support: each class needs >= {min_class_count} rows"
        )
    return {"rows": len(y), "positives": positives, "negatives": negatives}


def fit_platt(
    scores: Sequence[float], labels: Sequence[int], *, max_iter: int = 100
) -> PlattCalibrator:
    """Newton-Raphson logistic fit on one score with Platt (1999) target smoothing."""
    x, y = _scores(scores), _labels(labels)
    if len(x) != len(y):
        raise CalibrationError("scores and labels differ in length")
    support = require_support(y)
    targets = np.where(
        y == 1,
        (support["positives"] + 1) / (support["positives"] + 2),
        1 / (support["negatives"] + 2),
    )
    a, b = 0.0, math.log((support["positives"] + 1) / (support["negatives"] + 1))
    for _ in range(max_iter):
        p = 1.0 / (1.0 + np.exp(-(a * x + b)))
        weight = np.maximum(p * (1 - p), 1e-12)
        gradient = np.array([np.sum((p - targets) * x), np.sum(p - targets)])
        hessian = np.array(
            [
                [np.sum(weight * x * x), np.sum(weight * x)],
                [np.sum(weight * x), np.sum(weight)],
            ]
        ) + 1e-9 * np.eye(2)
        delta = np.linalg.solve(hessian, gradient)
        a, b = a - float(delta[0]), b - float(delta[1])
        if np.max(np.abs(delta)) < 1e-10:
            break
    if not (math.isfinite(a) and math.isfinite(b)):
        raise CalibrationError("Platt fit did not converge to finite parameters")
    return PlattCalibrator(a, b)


def fit_isotonic(scores: Sequence[float], labels: Sequence[int]) -> IsotonicCalibrator:
    """Pool-adjacent-violators on score-sorted rows; tied scores share one block."""
    x, y = _scores(scores), _labels(labels)
    if len(x) != len(y):
        raise CalibrationError("scores and labels differ in length")
    require_support(y)
    order = np.lexsort((y, x))
    blocks: list[list[float]] = []  # [lower score, label sum, count]
    for score, label in zip(x[order], y[order]):
        if blocks and blocks[-1][0] == score:
            blocks[-1][1] += label
            blocks[-1][2] += 1
        else:
            blocks.append([float(score), float(label), 1.0])
        while (
            len(blocks) > 1
            and blocks[-2][1] / blocks[-2][2] >= blocks[-1][1] / blocks[-1][2]
        ):
            lower, total, count = blocks.pop()
            blocks[-1][1] += total
            blocks[-1][2] += count
    return IsotonicCalibrator(
        tuple(b[0] for b in blocks), tuple(b[1] / b[2] for b in blocks)
    )


def brier_score(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    p, y = _probabilities(probabilities, labels)
    return float(np.mean((p - y) ** 2))


def log_loss(
    probabilities: Sequence[float], labels: Sequence[int], eps: float = 1e-15
) -> float:
    p, y = _probabilities(probabilities, labels)
    p = np.clip(p, eps, 1 - eps)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _probabilities(
    probabilities: Sequence[float], labels: Sequence[int]
) -> tuple[np.ndarray, np.ndarray]:
    p, y = _scores(probabilities), _labels(labels)
    if len(p) != len(y) or not len(p) or (p < 0).any() or (p > 1).any():
        raise CalibrationError(
            "probabilities must be in [0, 1] and match nonempty labels"
        )
    return p, y


def reliability_bins(
    probabilities: Sequence[float], labels: Sequence[int], n_bins: int = 10
) -> list[dict]:
    """Fixed-width bins on [0, 1]; the last bin includes 1.0. Empty bins keep count 0."""
    p, y = _probabilities(probabilities, labels)
    if type(n_bins) is not int or n_bins < 1:
        raise CalibrationError("n_bins must be a positive integer")
    index = np.minimum((p * n_bins).astype(int), n_bins - 1)
    bins = []
    for i in range(n_bins):
        mask = index == i
        count = int(mask.sum())
        bins.append(
            {
                "lower": i / n_bins,
                "upper": (i + 1) / n_bins,
                "count": count,
                "mean_predicted": float(p[mask].mean()) if count else None,
                "observed_rate": float(y[mask].mean()) if count else None,
            }
        )
    return bins


def expected_calibration_error(
    probabilities: Sequence[float], labels: Sequence[int], n_bins: int = 10
) -> float:
    """Count-weighted mean absolute gap between predicted and observed rates."""
    bins = reliability_bins(probabilities, labels, n_bins)
    total = sum(b["count"] for b in bins)
    return float(
        sum(
            b["count"] * abs(b["mean_predicted"] - b["observed_rate"])
            for b in bins
            if b["count"]
        )
        / total
    )


def diagnostics(
    probabilities: Sequence[float], labels: Sequence[int], n_bins: int = 10
) -> dict:
    """Brier, log loss, ECE, prevalence and reliability bins with counts."""
    y = _labels(labels)
    return {
        "rows": len(y),
        "prevalence": float(y.mean()) if len(y) else None,
        "brier": brier_score(probabilities, labels),
        "log_loss": log_loss(probabilities, labels),
        "ece": expected_calibration_error(probabilities, labels, n_bins),
        "reliability_bins": reliability_bins(probabilities, labels, n_bins),
    }


def require_partition(rows: Sequence[Mapping], partition: str) -> None:
    """Every row must carry the expected partition label; mixing is refused."""
    if not rows:
        raise CalibrationError(f"zero support: no {partition} rows")
    found = sorted({str(row.get("partition")) for row in rows})
    if found != [partition]:
        raise CalibrationError(f"rows must all be labelled {partition}; found {found}")
    for row in rows:
        if not isinstance(row.get("group_id"), str) or not row["group_id"]:
            raise CalibrationError("every row needs a group_id for grouped reporting")


def canonical_rows(rows: Sequence[Mapping]) -> list[dict]:
    """Rows in a stable order (``query_id``, then content) so digests ignore input order."""
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: (str(row.get("query_id")), canonical_digest(row)),
    )


def require_disjoint_from_fit(rows: Sequence[Mapping], artifact: Mapping) -> None:
    """Refuse rows whose groups were used to fit the calibrator (same group, two roles)."""
    fitted = artifact.get("fit_group_ids")
    if not isinstance(fitted, list):
        raise CalibrationError(
            "calibrator does not record its fit groups; refit with calibration-v2"
        )
    overlap = sorted({str(row.get("group_id")) for row in rows} & set(fitted))
    if overlap:
        raise CalibrationError(
            f"{len(overlap)} group(s) were also used to fit the calibrator, "
            f"e.g. {overlap[0]}; fit and threshold groups must be disjoint"
        )


def fit_calibrator(rows: Sequence[Mapping], config: Mapping) -> dict:
    """Fit the configured method on ``calibration_fit`` rows and return a sealed artifact.

    In-sample diagnostics are labelled as such; they are not held-out evidence.
    The raw-score diagnostic shows what an uncalibrated score would claim.
    """
    require_partition(rows, FIT_PARTITION)
    rows = canonical_rows(rows)
    method = config.get("method")
    if method not in METHODS:
        raise CalibrationError(f"method must be one of {METHODS}")
    scores = [row["score"] for row in rows]
    labels = [row["label"] for row in rows]
    support = require_support(labels, config.get("min_class_count", 1))
    calibrator = (
        fit_platt(scores, labels) if method == "platt" else fit_isotonic(scores, labels)
    )
    n_bins = config.get("n_bins", 10)
    body = {
        "version": CALIBRATION_VERSION,
        "method": method,
        "params": calibrator.params(),
        "event": "selected top record belongs to the supported target set for this frozen pipeline",
        "fit_partition": FIT_PARTITION,
        "support": support,
        "groups": len({row["group_id"] for row in rows}),
        "fit_group_ids": sorted({row["group_id"] for row in rows}),
        "domains": dict(
            sorted(Counter(row.get("domain", "unknown") for row in rows).items())
        ),
        "score_kinds": sorted({str(row.get("score_kind")) for row in rows}),
        "input_digest": canonical_digest(rows),
        "in_sample_diagnostics": diagnostics(
            calibrator.predict(scores), labels, n_bins
        ),
        "source_bundle": config.get("source_bundle"),
        "evidence": config.get(
            "evidence", "fictional/synthetic regression-only evidence"
        ),
    }
    return {**body, "bundle_digest": canonical_digest(body)}


def load_calibrator(artifact: Mapping) -> PlattCalibrator | IsotonicCalibrator:
    """Verify the bundle digest, then rebuild the calibrator from plain parameters."""
    body = {key: value for key, value in artifact.items() if key != "bundle_digest"}
    if artifact.get("version") != CALIBRATION_VERSION or canonical_digest(
        body
    ) != artifact.get("bundle_digest"):
        raise CalibrationError("calibration bundle digest or version mismatch")
    try:
        params = artifact["params"]
        if artifact["method"] == "platt":
            return PlattCalibrator(_finite(params["a"]), _finite(params["b"]))
        if artifact["method"] == "isotonic":
            lower, values = (
                tuple(map(_finite, params["lower"])),
                tuple(map(_finite, params["values"])),
            )
            if (
                len(lower) != len(values)
                or not lower
                or list(lower) != sorted(set(lower))
                or list(values) != sorted(values)
                or not 0.0 <= values[0] <= values[-1] <= 1.0
            ):
                raise CalibrationError(
                    "isotonic parameters are not a valid nondecreasing step function"
                )
            return IsotonicCalibrator(lower, values)
    except (KeyError, TypeError) as exc:
        raise CalibrationError("calibration parameters are malformed") from exc
    raise CalibrationError("unknown calibration method")


def _finite(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise CalibrationError("calibration parameters must be finite numbers")
    return float(value)


def bind_source(rows_path: Path, source_bundle: object) -> dict:
    """Verify the scoring artifact and that the rows file was produced by it."""
    if not isinstance(source_bundle, str) or not source_bundle:
        raise CalibrationError(
            "config must name the source_bundle artifact that produced the rows"
        )
    try:
        manifest = verify_artifact(Path(source_bundle))
    except ArtifactError as exc:
        raise CalibrationError(f"source_bundle failed verification: {exc}") from exc
    payload = json.loads(Path(rows_path).read_text(encoding="utf-8"))
    claimed = (
        payload.get("artifact_manifest_digest") if isinstance(payload, dict) else None
    )
    if claimed != manifest["manifest_digest"]:
        raise CalibrationError(
            "rows file was not produced by the verified source_bundle (stale or edited rows)"
        )
    return {
        "path": source_bundle,
        "rows_file_sha256": file_sha256(Path(rows_path)),
        "artifact_manifest_digest": manifest["manifest_digest"],
    }


def load_rows(path: Path) -> list[dict]:
    """Read a selected-records JSON file written by the training pilot."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload["rows"] if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise CalibrationError("selected-record rows must be a list")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["fit"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        rows_path = Path(config["rows_path"])
        rows = [
            row for row in load_rows(rows_path) if row.get("partition") == FIT_PARTITION
        ]
        # Bind the calibrator to the exact, verified scoring artifact that produced its rows.
        source = bind_source(rows_path, config.get("source_bundle"))
        results = {}
        for method in [config["method"], *config.get("alternatives", [])]:
            artifact = fit_calibrator(
                rows, {**config, "method": method, "source_bundle": source}
            )
            atomic_write_json(
                Path(config["output_dir"]) / f"calibrator-{method}.json", artifact
            )
            results[method] = {
                key: artifact[key]
                for key in ("support", "groups", "params", "bundle_digest")
            }
            results[method]["in_sample"] = {
                k: artifact["in_sample_diagnostics"][k]
                for k in ("brier", "log_loss", "ece", "prevalence")
            }
    except (OSError, KeyError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(results, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
