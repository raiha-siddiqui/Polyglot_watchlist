# Calibration, threshold and abstention protocol (Completion Step 17 mechanics)

Status: **mechanics implemented and exercised on fictional rows only.** Real calibration fitting and threshold selection are **blocked**: they require the reviewed Step 08 corpus, a frozen Step 16 pipeline and (per the plan's DAG) frozen Step 18/22/23 choices. No result here is an operational guarantee.

## The calibrated event

The only event calibrated is: *the selected top record belongs to the query's independently supported target set, for this frozen pipeline and fixed index.* It is not identity probability, wrongdoing, sanctions status or clearance. Rows are selected-record rows after retrieval, including absent-target queries (label 0), not random balanced pairs.

Each row carries `query_id`, `group_id`, `partition`, `domain`, `score_kind`, `score`, `label`, `presence` and `selected_entity_id`.

## Partition locks

| Partition | Allowed use | Enforcement |
| --- | --- | --- |
| `calibration_fit` | Fit Platt (primary) or isotonic (predeclared alternative) | `fit_calibrator` calls `require_partition(rows, 'calibration_fit')`; any other or mixed label is refused |
| `threshold_selection` | Choose the finite-grid policy thresholds | `select_policy` requires every row to be `threshold_selection` and refuses any row whose `group_id` is in the calibrator's `fit_group_ids`; the CLI refuses any other `--partition` |
| `test` | Final confirmation only (Step 28) | `evaluate` accepts only an exact, case-sensitive allowlist (`train`, `development`, `calibration_fit`, `threshold_selection`); `test`, `TEST`, `holdout` and any other label are refused |

## Methods

- **Platt scaling:** Newton–Raphson logistic fit on one score with Platt's smoothed targets `(N+ + 1)/(N+ + 2)` and `1/(N- + 2)`.
- **Isotonic regression:** pool-adjacent-violators on score-sorted rows; tied scores are pooled; prediction is a nondecreasing step function.
- **Refusals:** zero rows, one class, or fewer than `min_class_count` rows in a class raise `CalibrationError`.
- **Artifacts:** plain JSON parameters (no pickle), support counts, group and domain counts, the sorted `fit_group_ids`, score kinds, input digest, a source binding and a self `bundle_digest` verified by `load_calibrator`. Rows are sorted by `query_id` (then content) before fitting and digesting, so the same rows in any order give the same `bundle_digest`. `load_calibrator` refuses non-finite Platt parameters and isotonic values outside [0, 1]. Like the model manifest, the self-digest detects accidental edits; it is not a signature.
- **Source binding:** the CLI verifies `source_bundle` with `verify_artifact` and refuses a rows file whose `artifact_manifest_digest` differs from that verified manifest (a stale or edited rows file).
- **Diagnostics:** Brier score, log loss, fixed-bin ECE and reliability bins with counts. Fit-partition diagnostics are labelled in-sample. Selection-partition diagnostics are reported separately as rows not used for fitting.

Grouped internal validation inside `calibration_fit` for method/regularisation choice is **not yet implemented**; Platt is predeclared as primary to avoid choosing by held-out results.

## Review-attention policy

`review_policy.py` can only flag and order cases:

- `priority_review`: calibrated support ≥ priority threshold (boundary inclusive).
- `standard_review`: intermediate support.
- `insufficient_support`: support below the lower threshold, non-finite support, or unsupported domain/score kind, truncation, stale provenance, upstream failure or no scored record. This never means "no target exists".

A policy may enable only domains and score kinds the calibrator was fitted on. `freeze_policy` (and therefore `select_policy`) requires the calibrator and refuses anything outside its `domains`/`score_kinds`. `assess` re-checks the same subset, so a hand-sealed manifest that enables an unseen domain is refused, and each case is flagged `unsupported_domain` unless its domain is both enabled and calibrated.

Every record has `human_review_required: true`. The policy cannot emit match/clear/identity/sanctions terms or the human outcomes `candidate_for_follow_up`, `none_selected`, `unresolved`; a last-line guard refuses such output. Frozen manifests are digest-sealed; unknown fields (for example an `auto_clear_below` override), tampering and decision terms are refused. A calibrator whose `bundle_digest` differs from the one the policy was frozen with raises `StaleBundleError`.

## Threshold selection rule (predeclared in `eval/configs/review-policy-v2.json`)

- Grid: 0.05 to 0.95 in steps of 0.05.
- Priority threshold: smallest grid value with at least `min_selected` rows whose precision Wilson 95% lower bound is at least `target_precision_lower`. If none qualifies, the priority band is disabled (`no_threshold_supported`).
- Insufficient-support threshold: largest grid value not above the priority threshold whose share of positive rows falling below it has a Wilson 95% upper bound within `max_positive_below_upper`; otherwise 0.
- Wilson intervals are two-sided, assume independent rows and are **unadjusted for the grid search**. They are descriptive. Stronger statements need independent confirmation with sample sizes and multiplicity frozen in advance (see the plan's Step 17 item 7: for example 299 independent eligible cases for a zero-error one-sided 95% bound below 1%).
- Coverage is simulated suggestion coverage, not measured workload saved.

## Commands

```sh
python -m watchlist.calibration fit --config eval/configs/calibration-v2.json
python -m watchlist.evaluate_policy select --partition threshold_selection --config eval/configs/review-policy-v2.json
```

## What invalidates a calibrator or policy

A changed model, tokenizer, normalisation, index view, aggregation, routing, query mixture or domain. Recalibrate and reselect a compatible bundle; never reuse an incumbent calibrator silently. Fictional-generated rows (`domain: fictional-generated-v1`) do not support any sanctions-domain probability.
