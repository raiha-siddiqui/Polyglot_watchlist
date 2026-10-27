# Completion Steps 13 and 17 — training and calibration mechanics on fictional data

Date: 2026-10-26, Asia/Kolkata. **Status: mechanics implemented and verified; the real experiments are BLOCKED.** All numbers below come from commands actually run in this checkout on **fictional generated names** with a **tiny randomly initialised in-repo model**. They are regression-only evidence about the machinery. They are not performance estimates, they do not fine-tune E5 or MiniLM, and they do not close Steps 14–17.

## Goal

- **Step 13:** prove that a tensor training path updates weights, computes correct losses, keeps held-out groups out of training and mining, stops on explicit caps, saves/resumes exactly, never overwrites original or frozen weights and writes verifiable derived artifacts. Run a bounded pilot (≤200 steps).
- **Step 17:** implement partition-locked Platt/isotonic calibration with Brier/log loss/ECE/reliability bins, a finite-grid threshold selector with Wilson intervals, and a frozen review-attention policy that can only order and flag cases for human review.

## Files created (all new; no shared file was edited)

| File | Purpose and main functions |
| --- | --- |
| `src/watchlist/training_data.py` | `generate_fictional_entities(count, seed)` invents syllable names with initial/reorder/typo/Cyrillic aliases (`synthetic: true`). `fictional_assignments` uses `splits.assign_groups`. `partition_entities` validates with `splits.validate_assignments`. `build_training_pairs` builds capped positives plus training-only hard (RapidFuzz token-sort, bulk `cdist`, alias-ID tie-break, rank receipts) and easy (hash-strided) negatives, re-checks every endpoint (`_check_endpoint`) and refuses planted held-out IDs labelled `train`. `_manifest`/`verify_manifest` give an input-order-independent self digest. `group_aware_batches` never puts two pairs from one group in a batch. |
| `src/watchlist/training_losses.py` | `multiple_negatives_ranking_loss` (scaled cosine in-batch softmax, same-group false-negative masking, optional hard-negative columns), `binary_cross_entropy_loss` (raw logits, 0/1 labels), `check_finite_gradients`; all raise `NonFiniteLossError` on NaN/inf. |
| `src/watchlist/model_artifacts.py` | Standard-library only. `atomic_write_bytes/json` (temp file, fsync, `os.replace`), `write_artifact` (payload hashes, config, data digest, metrics, code/environment fingerprints, `is_pretrained_original: false`, manifest written last), `verify_artifact` (schema, self digest, non-empty `files`, plain relative payload names via `_safe_payload_name`, every payload hash), `is_protected_directory` (pretrained `acquisition.json` or sealed manifest at or above a path). The self-digest detects accidental corruption; it is not a signature against deliberate tampering. |
| `src/watchlist/training.py` | `load_training_config` (strict dataclasses; `_validate_pretrained` checks the approved protocol section for pretrained routes), `CharNgramBiEncoder`, `CharNgramPairScorer`, lazy `SentenceTransformerBiEncoder` (`architecture: sentence-transformers-e5`, fixed `max_seq_length`, `TruncationError`) and `MiniLMCrossEncoder` (`architecture: cross-encoder-minilm`, BCE, `TruncationError`), `build_model` routing, `model_lineage` (random-init / pretrained receipt / derived artifact), `torch_runtime` (threads + deterministic algorithms, restored afterwards), `current_rss_bytes` (Windows psapi / Linux procfs), `guard_output_dir`, `preflight`, deterministic `_BatchSchedule`, `batch_loss`, `save_checkpoint`/`load_checkpoint` (step file then atomic SHA-bound pointer; recipe, starting-artifact and data digests must match), `train` (step/wall/memory/non-finite stops; returns lineage), `export_artifact` (records that lineage), `load_artifact_state` (verify first; loads `weights.pt` only if the manifest lists it)/`load_artifact_model`, `retrieval_metrics`, `selected_record_rows`, `run_pilot` (records `null` first/last loss if a cap stops it before step 1), CLI `preflight`/`pilot`. |
| `src/watchlist/calibration.py` | `fit_platt`, `fit_isotonic` (PAV), `require_support`, `brier_score`, `log_loss`, `reliability_bins`, `expected_calibration_error`, `diagnostics`, `require_partition`, `canonical_rows` (order-independent fitting and digests), `fit_calibrator` (calibration_fit only, JSON bundle with digest, `fit_group_ids` and source binding), `require_disjoint_from_fit`, `load_calibrator` (finite parameters only), `bind_source` (verifies `source_bundle` and the rows file's artifact digest), `load_rows`, CLI `fit`. |
| `src/watchlist/review_policy.py` | Flags `priority_review`/`standard_review`/`insufficient_support`; `calibrated_scope` (policy domains/score kinds must be a subset of the calibrator's), `freeze_policy(body, calibration_artifact)`, `load_policy` (schema, digest, decision-term and threshold checks), `assess` (stale-bundle and scope checks, reasons including `nonfinite_calibrated_support`, ordering, last-line `_assert_attention_only`). |
| `src/watchlist/evaluate_policy.py` | `wilson_interval`, `grid_report`, `select_policy` (threshold_selection only, groups disjoint from the fit), `evaluate` (exact allowlist `EVALUABLE_PARTITIONS`; `test` and any other label refused), CLI `select`. |
| `eval/configs/train-pilot-fictional.json`, `calibration-v2.json`, `review-policy-v2.json` | Predeclared pilot, calibration and policy settings. |
| `tests/test_training_data.py` (8), `test_training_losses.py` (9), `test_model_artifacts.py` (15), `test_training.py` (27), `test_calibration.py` (13), `test_review_policy.py` (19) | 91 tests (62 before the adversarial-review fixes). |
| `docs/training-protocol.md`, `docs/calibration-protocol.md` | Protocols and claim boundaries. |

Generated, gitignored outputs: `data/models/derived-pilot/char-ngram-pilot-v1/{run,artifact}/` and `data/eval-comparisons/completion-13-17/` (logs, `selected-records.json`, `calibration/`, `policy/`).

## Commands actually run and results

Environment: Windows 11, `.venv` Python 3.14.9, torch 2.14.1+cpu, numpy 2.5.4, `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.

```sh
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_training.py tests/test_training_losses.py \
  tests/test_model_artifacts.py tests/test_training_data.py tests/test_calibration.py tests/test_review_policy.py
# 91 passed in 77.15s
.tools/qa/Scripts/ruff.exe check --no-force-exclude --select E4,E7,E9,F <the 13 owned Python files>
# All checks passed!
.venv/Scripts/python.exe -B -m watchlist.training preflight --config eval/configs/train-pilot-fictional.json
.venv/Scripts/python.exe -B -m watchlist.training pilot --config eval/configs/train-pilot-fictional.json   # 78.5 s wall incl. imports
.venv/Scripts/python.exe -B -m watchlist.calibration fit --config eval/configs/calibration-v2.json
.venv/Scripts/python.exe -B -m watchlist.evaluate_policy select --partition threshold_selection --config eval/configs/review-policy-v2.json
.venv/Scripts/python.exe -B -m watchlist.evaluate_policy select --partition test --config eval/configs/review-policy-v2.json
# exit 2: "thresholds may be selected only on the threshold_selection partition"
# Refusal checks with scratch copies of the configs (outside the repo):
python -m watchlist.calibration fit --config <calibration-v2 with source_bundle = the previous artifact>
# exit 2: "rows file was not produced by the verified source_bundle (stale or edited rows)"
python -m watchlist.evaluate_policy select --partition threshold_selection --config <review-policy-v2 with supported_domains = ["sanctions-real"]>
# exit 2: "policy enables domains/score kinds the calibrator never saw: ['sanctions-real']"
```

The full pilot → calibration → policy chain has now been run three times in separate processes. The latest run followed the adversarial-review fixes; the previous generated outputs were moved out of the repository first, because sealed artifacts refuse to be overwritten. Training losses, recall, selected thresholds and every policy count were identical across all runs. The Platt parameters differed only in the 15th significant digit, because rows are now sorted before fitting. The artifact manifest digest changed each time because the code fingerprint changed. The calibrator and policy digests changed because the calibrator now records `fit_group_ids`.

### Pilot (fictional, 600 generated entities, seed 42)

Split by the production hasher: 300 train, 97 development, 58 calibration_fit, 62 threshold_selection, 83 test entities. Training pairs: 1,791 positives, 962 hard and 1,791 easy negatives (data digest `2a17996c…730f`). Preflight: 528,448 parameters, estimated optimizer state 8.5 MB, process RSS ~0.32 GB against a 4 GiB cap, 56 group-aware batches per epoch.

| Measure | Before (random init) | After 200 steps |
| --- | --- | --- |
| Status | — | `completed_max_steps`, 200 steps, 12.1 s training wall, peak RSS 351 MB |
| Training loss (first/last step) | 1.949 | 0.0097 |
| Probe loss (8 epoch-0 training batches, in-sample) | 2.403 | 0.0071 |
| Fictional dev Recall@1 (97 held-out-alias queries, 97-entity dev index) | 46/97 = 0.474 | 63/97 = 0.649 |
| Fictional dev Recall@5 | 52/97 = 0.536 | 78/97 = 0.804 |
| Fictional dev MRR@5 | 0.493 | 0.712 |
| RapidFuzz Levenshtein on the same queries (reference) | R@1 49/97 = 0.505, R@5 58/97 = 0.598, MRR@5 0.541 | — |

The probe loss is on training batches, so its near-zero value shows memorisation of the fictional training set, not generalisation. The development gain is on generated names whose alias rules (typo/reorder/initial/Cyrillic rendering) are shared between train and development; it says nothing about real multilingual names. Artifact: `weights.pt` 2,115,937 bytes; manifest digest `937a0afc…3628`; `derived_from` = `{base: random-init, architecture: char-ngram-bi-encoder-v1, init_seed: 42, pretrained_weights: null}`; `verify_artifact` passed and `load_artifact_model` reloaded it. The calibrator's source binding records the same verified digest.

### Calibration (fictional selected-record rows)

`selected_record_rows` produced 172 rows: 82 calibration_fit (58 groups; 58 present + 24 absent queries; 41 positive, 41 negative) and 90 threshold_selection (62 groups; 62 present + 28 absent; 44 positive). Raw cosine on threshold_selection ranged 0.461–0.914.

| Calibrator (fit on calibration_fit) | Params | In-sample Brier / log loss / ECE | On threshold_selection rows (not used for fit) Brier / log loss / ECE |
| --- | --- | --- | --- |
| Platt (primary) | a = 18.837, b = −12.492 | 0.101 / 0.327 / 0.115 | 0.102 / 0.341 / 0.099 |
| Isotonic (alternative) | 7 steps | 0.084 / 0.260 / ≈0 | 0.106 / 0.674 / 0.074 |
| Raw cosine clipped to [0,1] (not a calibrator) | — | — | Brier 0.207, ECE 0.285 |

Isotonic's large held-out log loss comes from 0/1 step values; its near-zero in-sample ECE is expected for PAV and is not evidence. Bins with 2–7 rows are too small to interpret.

### Policy selection (threshold_selection, in-sample)

Rule: grid 0.05–0.95, precision Wilson lower ≥ 0.80 with ≥10 rows; positives-below Wilson upper ≤ 0.25. Selected: priority threshold **0.60**, insufficient-support threshold **0.55**, policy digest `cfa43742…854d`. The 62 threshold_selection groups share no group with the 58 calibration_fit groups, and the selector now checks this.

| Flag | Rows (of 90) | Selected record correct | Wilson 95% |
| --- | --- | --- | --- |
| `priority_review` | 41 | 38/41 = 0.927 | 0.806–0.975 |
| `standard_review` | 3 | 1/3 | 0.061–0.792 |
| `insufficient_support` | 46 | 5/46 = 0.109 | 0.047–0.230 |

Absent-target queries reaching `priority_review`: 1/28 (Wilson 0.006–0.177). Present-target queries flagged `insufficient_support`: 20/62 (0.220–0.446). These are in-sample to the threshold search and unadjusted for multiplicity. All 90 outputs carry `human_review_required: true`; no output is a match/clear decision.

## Example input and output

Actual first threshold_selection row from `selected-records.json` (fictional):

```json
{"query_id": "gen-00006:present", "group_id": "gen-00006", "partition": "threshold_selection",
 "domain": "fictional-generated-v1", "score_kind": "raw_cosine_char_ngram_v1", "presence": "present",
 "query_entity_id": "gen-00006", "selected_entity_id": "gen-00006", "score": 0.69381487, "label": 1}
```

Actual `assess` output with the frozen Platt calibrator and policy:

```json
{"query_id": "gen-00006:present", "selected_entity_id": "gen-00006", "flag": "priority_review",
 "reasons": [], "calibrated_support": 0.6404121272797799, "human_review_required": true,
 "policy_version": "review-policy-v2", "policy_digest": "cfa43742…854d", "attention_order": 40}
```

The same row with `domain` changed to `sanctions-unvalidated` returns `flag: insufficient_support`, `reasons: ["unsupported_domain"]`, `calibrated_support: null`, still with `human_review_required: true`. The `priority_review` flag means "look at this possible record early", not a match.

## What the tests verify

Training data: poison test (near-duplicate dev/test/calibration names never become pairs or mining candidates), contradictory planted IDs refused, group leakage/coverage errors, positive caps, input-order-independent digest and tamper detection, group-aware batches. Losses: MNRL and BCE against hand calculations, masking, hard-negative columns, NaN/inf refusal, gradient step direction. Training: weights change and loss falls, seed control, **bit-for-bit resume** (6+6 steps equals 12), recipe/data/corruption/no-checkpoint refusals, interrupted pointer write keeps the previous checkpoint, deadline and memory stops, non-finite stop without bad updates, refusal under pretrained/protected/frozen directories, artifact reload equivalence, initialisation from an artifact leaves its bytes unchanged, BCE pair-scorer path, config validation, selected-record label semantics, lazy sentence-transformers path verifies weights before import. Calibration: Platt stationarity, PAV hand example with ties, metric hand values, zero/one-class/min-support refusal, partition lock, deterministic tamper-evident bundles. Policy: Wilson reference values, `>=` boundaries, unsupported/failed cases, attention-only output, stale bundle, override/tamper/decision-term refusal, partition-locked deterministic selection, disabled priority band, `test` refusal.

## Adversarial review: outcome per issue (regression tests added)

| # | Issue | Outcome | Regression test |
| --- | --- | --- | --- |
| 1 | Policy could enable domains/score kinds the calibrator never saw | **Fixed.** `freeze_policy` now requires the calibrator and `calibrated_scope` enforces the subset. `assess` re-checks it, and each case's domain must be both enabled and calibrated | `test_policy_cannot_enable_domains_or_score_kinds_the_calibrator_never_saw` |
| 2 | Continued training recorded `random-init` lineage, and the recipe ignored `init_artifact` | **Fixed.** `model_lineage` records the starting artifact's digest, path and `derived_from`. The recipe digest includes the init digest, so resuming with a different or missing init is refused | `test_continued_training_records_and_enforces_artifact_lineage` |
| 3 | Unlisted `weights.pt` loaded; empty `files` and absolute names accepted | **Fixed.** A non-empty `files` is required and only plain relative names are allowed (POSIX and Windows rules). `weights.pt` must be listed. Docs now say the self-digest is not a signature | `test_payload_names_must_be_plain_relative_paths` (8 cases), `test_resealed_empty_file_list_is_refused`, `test_weights_not_listed_in_the_manifest_are_never_loaded` |
| 4 | `run_pilot` raised `IndexError` when stopped at step 0 | **Fixed.** First/last loss is `null` and the stop status is kept | `test_pilot_records_a_run_stopped_before_its_first_step` |
| 5 | sentence-transformers route unreachable; no cross-encoder adapter; no truncation guard | **Fixed.** Added the `sentence-transformers-e5` (MNRL) and `cross-encoder-minilm` (BCE) architectures behind `model.pretrained`, both verified via the existing `verify_model`. `max_seq_length` is fixed and `TruncationError` raised. The model path is auto-protected and a receipt-based lineage is recorded. Exercised with in-memory fakes only | `test_sentence_transformers_route_is_reachable_and_refuses_truncation`, `test_cross_encoder_route_trains_with_bce_and_refuses_truncation`, `test_pretrained_route_configs_are_validated` (3) |
| 6 | NaN Platt parameter gave `standard_review` with no reasons | **Fixed.** `load_calibrator` requires finite parameters and isotonic values in [0, 1]. `assess` flags non-finite support as `insufficient_support` / `nonfinite_calibrated_support` | `test_non_finite_calibrator_or_support_never_reaches_a_review_band`, `test_resealed_out_of_range_or_non_finite_parameters_are_refused` |
| 7 | Test-partition refusal was one exact string | **Fixed.** Exact allowlist `PARTITION_NAMES - {'test'}` | `test_evaluation_partition_is_an_exact_allowlist` (6 labels) |
| 8 | Calibrator digest depended on row order | **Fixed.** `canonical_rows` sorts by `query_id` then content before fitting/digesting (also in `select_policy`) | `test_calibrator_and_policy_digests_ignore_row_order` |
| 9 | No fit/threshold group disjointness check | **Fixed.** Calibrator stores sorted `fit_group_ids`. `select_policy` and `evaluate` (except on calibration_fit) refuse overlap, and older bundles without the list are refused | `test_threshold_rows_must_not_share_groups_with_the_fit` |
| 10 | Calibration CLI trusted the rows file's artifact digest | **Fixed.** `bind_source` runs `verify_artifact(source_bundle)` and requires the rows file digest to equal it | `test_rows_file_must_come_from_the_verified_source_artifact` |

## Limitations

- Fictional generated names share alias-construction rules across partitions; any score here is regression evidence only.
- The pilot model is a random-init char-n-gram encoder, not E5/MiniLM. The `sentence-transformers-e5` and `cross-encoder-minilm` routes are tested only with in-memory fake models and tokenizers. They are untested against real weights, because none are present in this checkout. No MPS/CUDA/mixed-precision route was tested.
- On Windows the cross-encoder route refuses with `cross-encoder route unavailable`. The cause is that `watchlist.reranking` imports the POSIX-only `resource` module at import time (see the shared-file request below).
- Manifest and bundle self-digests detect accidental corruption only. They are not signatures, so a writer with directory access can reseal altered content.
- `torch.use_deterministic_algorithms` and `torch.optim` import the torch compiler stack; with `-B` (no bytecode cache) the first import in a process takes ~40 s on this machine.
- Memory control is a pre-step RSS check plus a static estimate; it cannot pre-empt an allocation spike within one step.
- Grouped internal validation for choosing between Platt and isotonic is not implemented; Platt is predeclared.
- Wilson intervals assume independent rows (present/absent rows of one entity share a group) and are unadjusted for the grid search.
- Absent rows reuse the present query text with the target removed; they are not independently sourced absent queries.

## Shared-file changes requested (not made)

- `src/watchlist/reranking.py` (another engineer's file): `import resource` at module top fails on Windows (`ModuleNotFoundError`). Guard it (for example, import inside `peak_memory_bytes` with a Windows fallback) so `watchlist.reranking.validate_config`/`verify_model`, which the cross-encoder training route reuses, can load on Windows.
- `pyproject.toml` `[tool.ruff] include`: add the seven new modules and six new tests if they should be linted in CI (they pass `E4,E7,E9,F` today).
- No new dependencies: torch and numpy are already in the `embeddings` extra; rapidfuzz is core.
- Optional: a `Makefile` target for `training pilot` → `calibration fit` → `evaluate_policy select`.

## BLOCKED and next action

- **Blocked:** Step 13 smoke runs on the real local E5/MiniLM copies (weights absent here; need approval), Steps 14–15 fine-tuning, Step 16 selection, and real Step 17 fitting/threshold selection. All depend on the reviewed Step 08 corpus and frozen upstream pipeline choices.
- **Next action:** after Step 08 delivers reviewed groups and the local weights are verified, run `training preflight` and a ≤50-step smoke for each real model with a new config (replacing the fictional generator with reviewed training-partition pairs), record measured time/memory/disk, and bring the full-run budget for approval.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](review-p1-final.md) and [its validation record](review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
