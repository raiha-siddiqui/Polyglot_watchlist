"""Review policy flags/orders cases only; selection is partition-locked and frozen."""

import json
import math

import pytest

from watchlist import review_policy
from watchlist.calibration import CalibrationError, fit_calibrator
from watchlist.evaluate_policy import (
    PolicySelectionError,
    evaluate,
    grid_report,
    select_policy,
    wilson_interval,
)
from watchlist.model_artifacts import canonical_digest
from watchlist.review_policy import (
    FLAGS,
    FORBIDDEN_TERMS,
    POLICY_VERSION,
    PolicyError,
    StaleBundleError,
    assess,
    freeze_policy,
    load_policy,
)

DOMAIN, KIND = "fictional-generated-v1", "raw"


def row(
    i: int,
    score: float,
    label: int,
    partition: str = "threshold_selection",
    presence: str = "present",
):
    return {
        "query_id": f"q{i:03d}",
        "group_id": f"g{i:03d}",
        "partition": partition,
        "domain": DOMAIN,
        "score_kind": KIND,
        "score": score,
        "label": label,
        "presence": presence,
        "selected_entity_id": f"e{i:03d}",
    }


@pytest.fixture
def isotonic() -> dict:
    """Hand-checkable step calibrator: scores < 3 -> 0.5, scores >= 3 -> 1.0."""
    return fit_calibrator(
        [
            row(i, s, y, "calibration_fit")
            for i, (s, y) in enumerate([(1, 1), (2, 0), (3, 1), (4, 1)])
        ],
        {"method": "isotonic"},
    )


def policy(calibration: dict, **overrides) -> dict:
    body = {
        "version": POLICY_VERSION,
        "policy_id": "fictional-test",
        "priority_threshold": 1.0,
        "calibration_bundle_digest": calibration["bundle_digest"],
        "insufficient_below": 0.5,
        "supported_domains": [DOMAIN],
        "supported_score_kinds": [KIND],
        "threshold_partition": "threshold_selection",
        "selection": {"note": "hand-set"},
        "evidence": "fictional test policy",
        **overrides,
    }
    return freeze_policy(body, calibration)


def selection_rows() -> list[dict]:
    output = []
    for i in range(120):
        score = i / 119
        present = i % 3 != 0
        label = int(present and (i * 37) % 100 < 100 * score)
        output.append(row(i, score, label, presence="present" if present else "absent"))
    return output


def calibration_rows() -> list[dict]:
    return [
        {
            **r,
            "partition": "calibration_fit",
            "query_id": f"c{r['query_id']}",
            "group_id": f"c{r['group_id']}",
        }
        for r in selection_rows()
    ]


def test_wilson_interval_matches_reference_values() -> None:
    low, high = wilson_interval(5, 10)
    assert low == pytest.approx(0.2366, abs=1e-4) and high == pytest.approx(
        0.7634, abs=1e-4
    )
    assert wilson_interval(0, 0) == (None, None)
    low, high = wilson_interval(0, 299)
    assert low == 0.0 and 0 < high < 0.02
    assert wilson_interval(10, 10)[1] == 1.0
    with pytest.raises(PolicySelectionError):
        wilson_interval(3, 2)


def test_boundaries_use_greater_or_equal(isotonic: dict) -> None:
    frozen = load_policy(policy(isotonic))
    cases = [row(1, 3.0, 1), row(2, 2.0, 0), row(3, 0.0, 0)]
    flags = {r["query_id"]: r["flag"] for r in assess(frozen, isotonic, cases)}
    assert flags == {
        "q001": "priority_review",
        "q002": "standard_review",
        "q003": "standard_review",
    }
    stricter = load_policy(policy(isotonic, insufficient_below=0.6))
    flags = {
        r["query_id"]: (r["flag"], tuple(r["reasons"]))
        for r in assess(stricter, isotonic, cases)
    }
    assert flags["q002"] == ("insufficient_support", ("low_calibrated_support",))


def test_unsupported_or_failed_cases_request_attention_without_calibrated_support(
    isotonic: dict,
) -> None:
    frozen = load_policy(policy(isotonic))
    cases = [
        {**row(1, 4.0, 1), "domain": "sanctions-unvalidated"},
        {**row(2, 4.0, 1), "truncated": True},
        {**row(3, 4.0, 1), "stale_provenance": True},
        {**row(4, 4.0, 1), "error": "timeout"},
        {**row(5, 4.0, 1), "score_kind": "other"},
        {**row(6, float("nan"), 1)},
        {**row(7, 4.0, 1), "selected_entity_id": None},
    ]
    output = assess(frozen, isotonic, cases)
    assert all(
        r["flag"] == "insufficient_support" and r["calibrated_support"] is None
        for r in output
    )
    reasons = {r["query_id"]: r["reasons"] for r in output}
    assert reasons["q001"] == ["unsupported_domain"] and reasons["q002"] == [
        "truncated_candidates"
    ]
    assert reasons["q003"] == ["stale_provenance"] and reasons["q004"] == [
        "upstream_failure"
    ]
    assert reasons["q005"] == ["unsupported_score_kind"] and reasons["q006"] == [
        "no_scored_record"
    ]


def test_outputs_only_order_and_flag_never_decide(isotonic: dict) -> None:
    frozen = load_policy(policy(isotonic, insufficient_below=0.6))
    cases = [row(i, s, 0) for i, s in enumerate([0.0, 4.0, 2.0, 3.5, 1.0])]
    output = assess(frozen, isotonic, cases)
    assert [r["attention_order"] for r in output] == [1, 2, 3, 4, 5]
    assert [r["query_id"] for r in output] == ["q001", "q003", "q000", "q002", "q004"]
    for record in output:
        assert record["flag"] in FLAGS and record["human_review_required"] is True
        text = json.dumps(
            {
                k: v
                for k, v in record.items()
                if k not in {"query_id", "selected_entity_id"}
            }
        )
        assert not any(f'"{term}"' in text for term in FORBIDDEN_TERMS)
    assert output == assess(frozen, isotonic, list(reversed(cases)))


def test_stale_or_tampered_calibration_bundles_are_refused(isotonic: dict) -> None:
    frozen = load_policy(policy(isotonic))
    other = fit_calibrator(
        [
            row(i, s, y, "calibration_fit")
            for i, (s, y) in enumerate([(1, 0), (2, 1), (3, 1), (4, 1)])
        ],
        {"method": "isotonic"},
    )
    with pytest.raises(StaleBundleError, match="differs"):
        assess(frozen, other, [row(1, 3.0, 1)])
    with pytest.raises(StaleBundleError, match="digest"):
        assess(frozen, {**isotonic, "params": other["params"]}, [row(1, 3.0, 1)])


def test_overrides_tampering_and_decision_terms_are_refused(isotonic: dict) -> None:
    manifest = policy(isotonic)
    with pytest.raises(PolicyError, match="overrides"):
        load_policy({**manifest, "auto_clear_below": 0.1})
    with pytest.raises(PolicyError, match="digest"):
        load_policy({**manifest, "priority_threshold": 0.2})
    with pytest.raises(PolicyError, match="decision term"):
        policy(isotonic, selection={"low_band_action": "clear"})
    with pytest.raises(PolicyError, match="cannot exceed"):
        policy(isotonic, priority_threshold=0.3, insufficient_below=0.4)
    with pytest.raises(PolicyError, match="threshold partition"):
        policy(isotonic, threshold_partition="test")
    with pytest.raises(PolicyError, match="probability"):
        policy(isotonic, priority_threshold=1.5)
    resealed = {k: v for k, v in manifest.items() if k != "digest"}
    assert (
        load_policy({**resealed, "digest": canonical_digest(resealed)}).digest
        == manifest["digest"]
    )


def test_selection_is_partition_locked_deterministic_and_frozen() -> None:
    calibration = fit_calibrator(
        calibration_rows(), {"method": "platt", "min_class_count": 5}
    )
    config = {
        "policy_id": "fictional",
        "threshold_grid": [0.1 * i for i in range(1, 10)],
        "target_precision_lower": 0.45,
        "min_selected": 10,
        "max_positive_below_upper": 0.3,
        "supported_domains": [DOMAIN],
        "supported_score_kinds": [KIND],
    }
    for wrong in ("calibration_fit", "test", "development"):
        with pytest.raises(CalibrationError, match="threshold_selection"):
            select_policy(
                [{**r, "partition": wrong} for r in selection_rows()],
                calibration,
                config,
            )
    manifest, report = select_policy(selection_rows(), calibration, config)
    assert (manifest, report) == select_policy(selection_rows(), calibration, config)
    frozen = load_policy(json.loads(json.dumps(manifest)))
    assert frozen.priority_threshold is not None
    chosen = next(
        r for r in report["grid"] if r["threshold"] == frozen.priority_threshold
    )
    assert (
        chosen["precision"]["wilson_95"][0] >= 0.45
        and chosen["coverage"]["numerator"] >= 10
    )
    assert 0 < frozen.insufficient_below <= frozen.priority_threshold
    with pytest.raises(PolicySelectionError, match="increasing"):
        select_policy(
            selection_rows(), calibration, {**config, "threshold_grid": [0.5, 0.2]}
        )


def test_unattainable_precision_disables_the_priority_band() -> None:
    calibration = fit_calibrator(calibration_rows(), {"method": "platt"})
    config = {
        "policy_id": "fictional",
        "threshold_grid": [0.5, 0.9],
        "target_precision_lower": 0.999,
        "min_selected": 10,
        "max_positive_below_upper": 0.0,
        "supported_domains": [DOMAIN],
        "supported_score_kinds": [KIND],
    }
    manifest, report = select_policy(selection_rows(), calibration, config)
    assert (
        manifest["priority_threshold"] is None and manifest["insufficient_below"] == 0.0
    )
    assert report["selection"]["priority_status"] == "no_threshold_supported"
    outcome = evaluate(selection_rows(), calibration, manifest)
    assert outcome["flags"]["priority_review"]["coverage"]["numerator"] == 0


def test_evaluation_reports_intervals_and_refuses_final_test() -> None:
    calibration = fit_calibrator(calibration_rows(), {"method": "platt"})
    config = {
        "policy_id": "fictional",
        "threshold_grid": [0.2, 0.4, 0.6, 0.8],
        "target_precision_lower": 0.5,
        "min_selected": 5,
        "max_positive_below_upper": 0.5,
        "supported_domains": [DOMAIN],
        "supported_score_kinds": [KIND],
    }
    manifest, _ = select_policy(selection_rows(), calibration, config)
    report = evaluate(selection_rows(), calibration, manifest)
    assert report["rows"] == 120 and report["in_sample_selection"] is True
    assert sum(f["coverage"]["numerator"] for f in report["flags"].values()) == 120
    assert (
        report["human_review_required_for_all"]
        and "Fictional-only" in report["statement"]
    )
    assert report["absent_reaching_priority"]["denominator"] == 40
    with pytest.raises(PolicySelectionError, match="reserved"):
        evaluate(
            [{**r, "partition": "test"} for r in selection_rows()],
            calibration,
            manifest,
            "test",
        )
    table = grid_report([0.1, 0.9], [0, 1], [0.5])
    assert (
        table[0]["precision"]["value"] == 1.0 and table[0]["coverage"]["value"] == 0.5
    )


SELECTION_CONFIG = {
    "policy_id": "fictional",
    "threshold_grid": [0.2, 0.4, 0.6, 0.8],
    "target_precision_lower": 0.5,
    "min_selected": 5,
    "max_positive_below_upper": 0.5,
    "supported_domains": [DOMAIN],
    "supported_score_kinds": [KIND],
}


def reseal(manifest: dict, key: str = "digest", **changes) -> dict:
    body = {k: v for k, v in manifest.items() if k != key}
    body.update(changes)
    return {**body, key: canonical_digest(body)}


def test_policy_cannot_enable_domains_or_score_kinds_the_calibrator_never_saw(
    isotonic: dict,
) -> None:
    calibration = fit_calibrator(calibration_rows(), {"method": "platt"})
    for field, value in (
        ("supported_domains", ["sanctions-real"]),
        ("supported_score_kinds", ["e5-cosine"]),
    ):
        with pytest.raises(PolicyError, match="never saw"):
            select_policy(
                selection_rows(), calibration, {**SELECTION_CONFIG, field: value}
            )
        with pytest.raises(PolicyError, match="never saw"):
            policy(isotonic, **{field: [*SELECTION_CONFIG[field], *value]})
    # A manifest sealed by hand (bypassing freeze_policy) is still refused at assessment time.
    sneaky = reseal(policy(isotonic), supported_domains=[DOMAIN, "sanctions-real"])
    with pytest.raises(PolicyError, match="never saw"):
        assess(
            load_policy(sneaky),
            isotonic,
            [{**row(1, 4.0, 1), "domain": "sanctions-real"}],
        )
    with pytest.raises(StaleBundleError):
        freeze_policy(
            {k: v for k, v in policy(isotonic).items() if k != "digest"},
            fit_calibrator(calibration_rows(), {"method": "isotonic"}),
        )


def test_non_finite_calibrator_or_support_never_reaches_a_review_band(
    isotonic: dict, monkeypatch
) -> None:
    platt = fit_calibrator(calibration_rows(), {"method": "platt"})
    for params in ({"a": float("nan"), "b": 0.0}, {"a": 1.0, "b": float("inf")}):
        with pytest.raises(CalibrationError, match="finite"):
            review_policy.load_calibrator(reseal(platt, "bundle_digest", params=params))
    bad_iso = reseal(
        isotonic, "bundle_digest", params={"lower": [1.0, 3.0], "values": [0.5, 1.5]}
    )
    with pytest.raises(CalibrationError, match="nondecreasing"):
        review_policy.load_calibrator(bad_iso)

    class NanCalibrator:
        def predict(self, scores):
            return [math.nan] * len(scores)

    monkeypatch.setattr(
        review_policy, "load_calibrator", lambda _artifact: NanCalibrator()
    )
    [record] = assess(load_policy(policy(isotonic)), isotonic, [row(1, 3.0, 1)])
    assert (
        record["flag"] == "insufficient_support"
        and record["calibrated_support"] is None
    )
    assert record["reasons"] == ["nonfinite_calibrated_support"]


@pytest.mark.parametrize(
    "partition", ["test", "TEST", "Test", "holdout", " test", "final"]
)
def test_evaluation_partition_is_an_exact_allowlist(partition: str) -> None:
    calibration = fit_calibrator(calibration_rows(), {"method": "platt"})
    manifest, _ = select_policy(selection_rows(), calibration, SELECTION_CONFIG)
    rows = [{**r, "partition": partition} for r in selection_rows()]
    with pytest.raises(PolicySelectionError, match="reserved"):
        evaluate(rows, calibration, manifest, partition)


def test_calibrator_and_policy_digests_ignore_row_order() -> None:
    forward = fit_calibrator(calibration_rows(), {"method": "platt"})
    backward = fit_calibrator(list(reversed(calibration_rows())), {"method": "platt"})
    assert forward == backward
    manifest, report = select_policy(selection_rows(), forward, SELECTION_CONFIG)
    assert (manifest, report) == select_policy(
        list(reversed(selection_rows())), backward, SELECTION_CONFIG
    )
    assert assess(load_policy(manifest), backward, selection_rows()[:3])


def test_threshold_rows_must_not_share_groups_with_the_fit() -> None:
    calibration = fit_calibrator(calibration_rows(), {"method": "platt"})
    relabelled = [{**r, "partition": "threshold_selection"} for r in calibration_rows()]
    with pytest.raises(CalibrationError, match="disjoint"):
        select_policy(relabelled, calibration, SELECTION_CONFIG)
    manifest, _ = select_policy(selection_rows(), calibration, SELECTION_CONFIG)
    with pytest.raises(CalibrationError, match="disjoint"):
        evaluate(relabelled, calibration, manifest)
    legacy = reseal(
        {k: v for k, v in calibration.items() if k != "fit_group_ids"}, "bundle_digest"
    )
    with pytest.raises(CalibrationError, match="fit groups"):
        select_policy(selection_rows(), legacy, SELECTION_CONFIG)
    assert calibration["fit_group_ids"] == sorted(
        {r["group_id"] for r in calibration_rows()}
    )
