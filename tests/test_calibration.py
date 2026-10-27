"""Calibration maps, metrics and partition locks on hand-checkable fictional rows."""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from watchlist.calibration import (
    CalibrationError,
    bind_source,
    brier_score,
    diagnostics,
    expected_calibration_error,
    fit_calibrator,
    fit_isotonic,
    fit_platt,
    load_calibrator,
    log_loss,
    reliability_bins,
    require_partition,
)
from watchlist.model_artifacts import canonical_digest, write_artifact


def rows(partition: str = "calibration_fit", n: int = 40) -> list[dict]:
    """Deterministic fictional selected-record rows: higher score, more often correct."""
    output = []
    for i in range(n):
        score = i / (n - 1)
        output.append(
            {
                "query_id": f"q{i}",
                "group_id": f"g{i // 2}",
                "partition": partition,
                "domain": "fictional-generated-v1",
                "score_kind": "raw",
                "score": score,
                "label": int((i * 7919) % 100 < 100 * score),
            }
        )
    return output


def test_platt_fit_reaches_the_smoothed_likelihood_optimum() -> None:
    data = rows()
    x = np.array([r["score"] for r in data])
    y = np.array([r["label"] for r in data])
    calibrator = fit_platt(x, y)
    assert calibrator.a > 0
    positives, negatives = y.sum(), len(y) - y.sum()
    targets = np.where(y == 1, (positives + 1) / (positives + 2), 1 / (negatives + 2))
    p = calibrator.predict(x)
    assert abs(np.sum((p - targets) * x)) < 1e-8 and abs(np.sum(p - targets)) < 1e-8
    assert np.all(np.diff(calibrator.predict(np.linspace(0, 1, 11))) > 0)


def test_isotonic_pav_matches_hand_example_and_pools_ties() -> None:
    calibrator = fit_isotonic([1, 2, 3, 4], [1, 0, 1, 1])
    assert calibrator.lower == (1.0, 3.0) and calibrator.values == (0.5, 1.0)
    assert list(calibrator.predict([0, 2.5, 3, 10])) == [0.5, 0.5, 1.0, 1.0]
    tied = fit_isotonic([1, 1, 2], [0, 1, 1])
    assert tied.lower == (1.0, 2.0) and tied.values == (0.5, 1.0)
    fitted = fit_isotonic([r["score"] for r in rows()], [r["label"] for r in rows()])
    assert list(fitted.values) == sorted(fitted.values)


def test_metrics_match_hand_calculations() -> None:
    assert brier_score([0.2, 0.8], [0, 1]) == pytest.approx(0.04)
    assert log_loss([0.2, 0.8], [0, 1]) == pytest.approx(-math.log(0.8))
    assert expected_calibration_error(
        [0.1, 0.1, 0.9, 0.9], [0, 1, 1, 1]
    ) == pytest.approx(0.25)
    bins = reliability_bins([0.0, 1.0], [0, 1], n_bins=4)
    assert [b["count"] for b in bins] == [1, 0, 0, 1]
    assert bins[1]["mean_predicted"] is None
    report = diagnostics([0.2, 0.8], [0, 1], 2)
    assert report["prevalence"] == 0.5 and report["rows"] == 2
    with pytest.raises(CalibrationError):
        brier_score([1.2], [1])


@pytest.mark.parametrize(
    "labels, message",
    [
        ([], "zero support"),
        ([1, 1, 1], "one-class"),
        ([0, 0, 0], "one-class"),
        ([0, 2], "0 or 1"),
    ],
)
def test_zero_support_and_one_class_fits_are_refused(labels, message) -> None:
    for fit in (fit_platt, fit_isotonic):
        with pytest.raises(CalibrationError, match=message):
            fit([0.1 * i for i in range(len(labels))], labels)


def test_fit_is_locked_to_calibration_fit_partition() -> None:
    with pytest.raises(CalibrationError, match="calibration_fit"):
        fit_calibrator(rows("threshold_selection"), {"method": "platt"})
    with pytest.raises(CalibrationError, match="calibration_fit"):
        fit_calibrator(rows() + rows("test", 4), {"method": "platt"})
    with pytest.raises(CalibrationError, match="zero support"):
        require_partition([], "calibration_fit")
    with pytest.raises(CalibrationError, match="group_id"):
        require_partition([{**rows()[0], "group_id": ""}], "calibration_fit")
    with pytest.raises(CalibrationError, match="insufficient support"):
        fit_calibrator(
            rows()[:3] + rows()[-3:], {"method": "platt", "min_class_count": 5}
        )
    with pytest.raises(CalibrationError, match="method"):
        fit_calibrator(rows(), {"method": "temperature"})


@pytest.mark.parametrize("method", ["platt", "isotonic"])
def test_artifact_is_deterministic_inspectable_and_tamper_evident(method: str) -> None:
    artifact = fit_calibrator(rows(), {"method": method, "min_class_count": 5})
    assert artifact == fit_calibrator(
        list(rows()), {"method": method, "min_class_count": 5}
    )
    assert (
        artifact["fit_partition"] == "calibration_fit"
        and artifact["support"]["rows"] == 40
    )
    assert (
        artifact["domains"] == {"fictional-generated-v1": 40}
        and artifact["groups"] == 20
    )
    calibrator = load_calibrator(artifact)
    assert np.allclose(
        calibrator.predict([0.0, 1.0]), load_calibrator(artifact).predict([0.0, 1.0])
    )
    with pytest.raises(CalibrationError, match="digest"):
        load_calibrator(
            {**artifact, "method": "isotonic" if method == "platt" else "platt"}
        )


def test_resealed_invalid_isotonic_parameters_are_refused() -> None:
    artifact = fit_calibrator(rows(), {"method": "isotonic"})
    body = {k: v for k, v in artifact.items() if k != "bundle_digest"}
    body["params"] = {"lower": [0.0, 0.5], "values": [0.9, 0.1]}
    with pytest.raises(CalibrationError, match="nondecreasing"):
        load_calibrator({**body, "bundle_digest": canonical_digest(body)})


def test_rows_file_must_come_from_the_verified_source_artifact(tmp_path: Path) -> None:
    bundle = tmp_path / "artifact"
    bundle.mkdir()
    (bundle / "weights.pt").write_bytes(b"fictional weights")
    manifest = write_artifact(
        bundle,
        payload_files=["weights.pt"],
        config={},
        data_manifest_digest="a" * 64,
        metrics={},
        derived_from={"base": "random-init"},
    )
    rows_path = tmp_path / "rows.json"
    rows_path.write_text(
        json.dumps(
            {"artifact_manifest_digest": manifest["manifest_digest"], "rows": rows()}
        ),
        encoding="utf-8",
    )
    source = bind_source(rows_path, str(bundle))
    assert source["artifact_manifest_digest"] == manifest["manifest_digest"]
    rows_path.write_text(
        json.dumps({"artifact_manifest_digest": "b" * 64, "rows": rows()}),
        encoding="utf-8",
    )
    with pytest.raises(CalibrationError, match="stale or edited"):
        bind_source(rows_path, str(bundle))
    with pytest.raises(CalibrationError, match="source_bundle"):
        bind_source(rows_path, None)
    (bundle / "weights.pt").write_bytes(b"altered weights!!")
    with pytest.raises(CalibrationError, match="verification"):
        bind_source(rows_path, str(bundle))


def test_resealed_out_of_range_or_non_finite_parameters_are_refused() -> None:
    artifact = fit_calibrator(rows(), {"method": "isotonic"})
    body = {k: v for k, v in artifact.items() if k != "bundle_digest"}
    for params, message in (
        ({"lower": [0.0, float("nan")], "values": [0.1, 0.2]}, "finite"),
        ({"lower": [0.0, 0.5], "values": [-0.1, 0.2]}, "nondecreasing"),
        ({"lower": [0.0]}, "malformed"),
    ):
        changed = {**body, "params": params}
        with pytest.raises(CalibrationError, match=message):
            load_calibrator({**changed, "bundle_digest": canonical_digest(changed)})
