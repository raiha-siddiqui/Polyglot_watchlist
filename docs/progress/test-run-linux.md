# Project 1 — Linux test run and infrastructure

Date: 2026-10-27 (Asia/Singapore).

Status: **full offline all-extras suite executed; preservation checks remain blocked by sandbox visibility**. Branch: `p1-tests`, starting commit `fb32c9f`. This report covers the test/infrastructure assignment, not a release or a claim that the project is green.

The final two-shard **all-extras** run finished: **4,298 passed, 49 failed, 30 errors, 153 skipped, 3 xfailed, 3 deselected, and 13 subtests passed**, in **1406.89 seconds**.

The remaining preservation failures require a Linux process environment where `lsof` can inspect the relevant mounts and descriptors. Seven wheels were initially absent; they became available in the shared cache after restart. The final run installs all extras offline and executes the previously skipped Torch tests. This engineer downloaded no dependencies, models or datasets, made no live API calls, and triggered no hosted CI, pushes or merges.

## Complete runs and the interrupted attempt

| Run receipt directory | Concurrent shards | Passed | Failed | Errors | Skipped | Xfailed | Deselected | Elapsed wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `initial-available` | 4 | 4,204 | 104 | 30 | 156 | 3 | 3 | 2774.43 s (46m 14.43s) |
| `final-available` | 4 | 4,260 | 50 | 30 | 156 | 3 | 3 | 734.72 s (12m 14.72s) |
| `final-restarted` | 2 | 4,261 | 49 | 30 | 156 | 3 | 3 | 1389.63 s (23m 9.63s) |
| `final-all-extras` | 2 | 4,298 | 49 | 30 | 153 | 3 | 3 | 1406.89 s (23m 26.89s) |

Each completed run also reports **13 passed subtests**. Counts come from all shard JUnit files and pytest summaries; wall time comes from the parent runner's monotonic clock. The first three completed runs include 156 skips, including two Torch module-level collection skips; their collected totals differ from the final all-extras run. All runs after the initial run include the two new hook regression cases.

`final-verified` was interrupted by the externally reported WSL crash. It has no completed `run.json` or complete JUnit set and is **not** counted as a complete run. Its retained event streams contain 77 passed calls, seven failed calls and incomplete in-flight tests. The seven observed failures are recorded separately below. The partial secret-scan output was empty and was discarded as verification evidence. Nine corpus-fixture repeat receipts survived, all passing; an intended tenth repetition has no receipt and is not claimed.

After restart, Git/status and surviving jobs were checked. The sandbox process view showed no surviving test/scanner workers from this task. Memory initially remained exhausted; after available memory recovered, the entire suite was restarted with **two** pytest shards. No separate Torch-heavy job ran alongside it, and the secret scanner ran separately. There are two top-level pytest workers; the hook regression briefly launches one child pytest, within the test engineer's allowed three-process ceiling. Earlier runs used four shards before the user's reduced-concurrency instruction. The first run experienced severe host contention and initially uncached Python imports; its time is not a controlled performance comparison. Dependency bytecode was compiled only in this engineer's environments before subsequent runs. Test deadlines were not increased.

## Environment and reproduction

- Source of truth: `/mnt/d/Projects/raiha/Polyglot_watchlist-tests`.
- Linux execution mirror: `~/pw-p1-tests/repo`; only root `data/` is excluded. Package data at `src/watchlist/data/` is retained.
- Runtime: CPython **3.14.9**, uv **0.13.5**, pytest **9.1.1**; own runtime `~/pw-tools/venv-p1-tests` and QA `~/pw-tools/qa-p1-tests`.
- Cache `~/pw-tools/uv-cache`; explicit wheelhouse `~/pw-tools/wheelhouse`. Initial runtime inventory: **95 installed / 151 locked** packages; final all-extras runtime: **138 installed / 151 locked**; QA environment: **18 locked** packages.
- Initially installed extras: `agent`, `preprocessing`, `index`, `api`, `ui`, `mcp`, `llm`. The final environment also installs `embeddings`, completing `--all-extras`.
- Full commands, environment overrides, file-to-shard assignments, source hashes, collection coverage proving no duplicate selected items, selected/deselected node IDs, subprocess exit codes, logs and JUnit XML are retained under `~/pw-p1-tests/evidence/<run>/`.

Commands actually used (environment prefixes shown explicitly; no network installation):

```bash
rsync -a --delete --exclude /.git --exclude /.venv --exclude /.tools \
  --exclude '/.venv-*' --exclude /data --exclude __pycache__ \
  --exclude .pytest_cache /mnt/d/Projects/raiha/Polyglot_watchlist-tests/ \
  ~/pw-p1-tests/repo/

UV_CACHE_DIR=~/pw-tools/uv-cache \
UV_PROJECT_ENVIRONMENT=~/pw-tools/venv-p1-tests \
UV_PYTHON_DOWNLOADS=never ~/pw-tools/uvbin/bin/uv sync \
  --offline --locked --dev --all-extras --python 3.14 \
  --project ~/pw-p1-tests/repo
# Failed offline: missing locked NVIDIA/Torch artifacts.

UV_CACHE_DIR=~/pw-tools/uv-cache \
UV_PROJECT_ENVIRONMENT=~/pw-tools/venv-p1-tests \
UV_PYTHON_DOWNLOADS=never ~/pw-tools/uvbin/bin/uv sync \
  --offline --locked --dev --extra agent --extra preprocessing --extra index \
  --extra api --extra ui --extra mcp --extra llm --python 3.14 \
  --project ~/pw-p1-tests/repo

UV_CACHE_DIR=~/pw-tools/uv-cache \
UV_PROJECT_ENVIRONMENT=~/pw-tools/qa-p1-tests \
UV_PYTHON_DOWNLOADS=never ~/pw-tools/uvbin/bin/uv sync \
  --offline --locked --only-group qa --no-install-project --python 3.14 \
  --project ~/pw-p1-tests/repo

python3 ~/pw-p1-tests/run_suite.py initial-available
python3 ~/pw-p1-tests/run_final_suite.py final-available
python3 ~/pw-p1-tests/run_final_suite.py final-verified  # interrupted
python3 ~/pw-p1-tests/run_low_memory_suite.py final-restarted
# After this exits, repeat the above --all-extras offline sync successfully.
python3 ~/pw-p1-tests/run_all_extras_suite.py final-all-extras
```

The runner discovers every `tests/**/test_*.py` file, assigns each once (94 modules after the hook regression was added), and invokes pytest with `-q -m 'not integration' -p no:cacheprovider -rfEsxX --tb=short --durations=20`, separate `--basetemp` directories and separate JUnit outputs. `live_pytest.py` only records collection and outcomes; it does not alter outcomes. Runner scripts and raw receipts are machine-local handoff artifacts, not committed repository files.

Each shard sets `PYTHONDONTWRITEBYTECODE=1`, Hugging Face/Transformers/datasets offline flags, disabled LangSmith/LangChain tracing, `UV_OFFLINE=1`, `UV_PYTHON_DOWNLOADS=never`, `PIP_NO_INDEX=1`, disabled pip version checks and all three integration opt-ins to zero. BLAS/OpenMP thread counts are one. `QA_PYTHON`, `WATCHLIST_WHEELHOUSE`, `UV`, and `UV_CACHE_DIR` are explicit. These local runs do not claim the hosted CI job's OS network-namespace enforcement.

The initially absent wheels totaled **2,409,824,892 bytes** (2.41 GB): `torch==2.14.1`, `triton==3.8.0`, `nvidia-cudnn-cu13==9.24.0.43`, `nvidia-nccl-cu13==2.30.7`, `nvidia-cublas==13.1.1.3`, `nvidia-cufft==12.0.0.61`, and `nvidia-cusolver==12.0.4.66`. Exact locked hashes, sizes and URLs are in `evidence/missing-locked-wheels.json`; none were downloaded by this engineer. A read-only offline dry run after restart showed every registry artifact cached; its sole uncached item was the local editable project build. After the active suite exited, an actual `uv sync --offline --locked --dev --all-extras` completed without network access. The earlier download request was no longer necessary. The all-extras import preflight passed for every runtime extra in 59.73 seconds, with Torch configured for one thread. The all-extras runner groups the training, training-loss and reranking modules in one shard to limit simultaneous Torch imports. Final collection coverage records 4,533 selected test items, no duplicates and the same three integration deselections.

## Changes, by file and function

| File | Change and reason |
| --- | --- |
| `tests/conftest.py` | `_missing_private_evidence` decodes byte filenames with `os.fsdecode` and rejects invalid filename types rather than crashing pytest. It refuses chained/contextual errors, because missing evidence must be the sole cause. `pytest_runtest_makereport` leaves teardown failures intact. The private root remains exactly `data/eval-comparisons`; strict `WATCHLIST_REQUIRE_PRIVATE_EVIDENCE=1` remains available. |
| `tests/test_private_evidence.py` (new) | Two parameterized real-pytest subprocess checks exercise normal and strict modes against 16 inner cases: absolute/relative/byte private paths, setup, public/byte public paths, sibling-prefix/parent/symlink escapes, existing files, missing/invalid filenames, other exception types, wrapped/prior errors and teardown. Only four missing-private setup/call cases skip in normal mode; every unrelated error remains a failure/error. |
| `Makefile` | Adds the new hook regression module to `QUALITY_FILES`. |
| `pyproject.toml` | Adds the same module to Ruff's explicit include list. No dependency or build-backend change; `uv.lock` is unchanged. |
| `tests/test_packaging.py` | Adds explicit entry-point assertions for `review`, `api`, `ui`, `mcp_server`, `case_memory`, and `llm`; adds review/memory SQL schemas to required resources. Existing exact wheel/sdist file and byte comparisons remain. Clean-wheel inspection now reads all six declared resources from the installed package using `importlib.resources`, without a checkout/config dependency. |
| `scripts/project_checks.py` | Raises the bounded secret-scan timeout from 120 to 600 seconds for the expanded scope; an actual diagnostic scan took 547.47 seconds. Adds seven exact file-hash/line/type exceptions for reviewed non-secret literals and refreshes the existing reranking revision pin. Refreshes the existing corpus-test schema-digest pin after the six-line fixture change. No detector or publication file is excluded. |
| `tests/test_corpus.py` | In `test_file_change_during_read_is_rejected_even_when_content_is_identical`, backdates the saved fixture before injecting a same-byte rewrite. This guarantees a metadata change even when filesystem timestamps share one tick. Keeps the required `CorpusValidationError` assertion and adds checks that the injected rewrite happened and bytes stayed identical. No corpus implementation change. |
| `docs/progress/test-run-linux.md` (new) | This result, failure inventory, commands and handoff. |

The seven new scanner exceptions cover environment-variable names in `config/llm.json` and UI code, a fictional snapshot digest in `config/memory.json`, a deliberately rejected fake key in `tests/test_llm_contracts.py`, a public model revision in `tests/test_training.py`, and a fictional loopback URL credential in `tests/test_ui_client.py`. The existing `reranking.py` exception remains for its pinned public model revision. Whole-file hashes, exact lines/types and duplicate checks still fail closed when content changes.

All **125 application source files** match the starting manifest, including all **ten** `src/watchlist/agent/*` files and all concurrently reviewed new modules. `.github/workflows/ci.yml` needed no edit. No other engineer's fixes were merged into this branch or included in these results.

## Focused verification and packaging

| Check | Actual result |
| --- | --- |
| Hook regression before fix | **1 failed, 1 passed**; bytes filename reproduced a pytest internal error. |
| Hook regression after fix | **2 passed**; normal/strict behavior verified through real pytest reports. |
| Hook plus exact scanner-exception guards | **8 passed, 43 deselected** in **41.62 s**. |
| Three corpus mutation checks | **3 passed, 283 deselected** in **1.91 s**. Nine additional fresh-process repetitions of the same-byte case passed; no result claimed for a missing tenth receipt. |
| Readiness probe | **41 passed, 1 private-evidence skip** in **35.72 s**. |
| Packaging focused run | **8 passed** in **229.88 s**; all **13** recorded subprocess commands returned zero. |
| Offline lock check | `uv lock --check --offline`: **151 packages**, exit zero; repeated after restart. |
| Ruff check and format | Pass; **138 files already formatted**, repeated after restart. |
| QA pre-commit config | `python -m pre_commit validate-config` passed; full hooks were not claimed as run here. |
| Inventory | Pass; initial **95**, final **138 installed / 151 locked**; two final license-metadata review items remain (one before all extras), not a completed licensing determination. |
| Fictional evaluation smoke | Pass; **157 fictional queries**, no skipped transformations. This is a deterministic regression check, not a real-data performance claim. |
| Secret scan | **passed**; **369 files**, **171 reviewed non-secret findings**, **0 unresolved**; actual detect-secrets with credential verification disabled and a Python socket audit guard. |

Packaging receipts are in `evidence/package-strengthened/receipt.json`. The wheel is **645,218 bytes** and the sdist **688,048 bytes**; their exact hashes are in the receipt. The wheel rebuilt from the sdist is byte-identical. Wheel/sdist contents include every source package and required package resource and exclude root datasets, model/evidence files and local environments. The package's small fictional `watchlist/data/synthetic_entities.json` is intentionally included; it is not repository `data/`.

The empty environment installation used **`uv pip install --offline --no-index --require-hashes`** with the built wheel and verified wheelhouse artifact. The installed environment contained only `polyglot-watchlist==0.1.0` and `rapidfuzz==3.14.6`, with no user/system-site fallback. An independently empty QA environment installed all 18 locked QA packages and their tool CLIs worked. The clean package probe read `agent/schema.sql`, `ingest/schema.sql`, `corpus_schema.sql`, `review/schema.sql`, `case_memory/schema.sql`, and `data/synthetic_entities.json` from site-packages.

Example actually executed outside the checkout:

```bash
<clean-environment>/bin/python -I -B -m watchlist --name 'Luma Vesper' --top-k 1
```

It returned fictional entity `syn-001`, raw similarity **100.0**, `synthetic: true`, and the notice that this is a possible record match for research review, not identity confirmation or a probability. The base-wheel CLI smoke does not claim an installed-wheel all-extras server test.

The source scan receipt is `evidence/secret-scan-restarted.json`; the later report file has a separate scanner receipt at `evidence/report-secret-scan.json`.

## CI validation

`evidence/ci-static-restarted.json` and `evidence/ci-target-resolution.json` record the static checks. YAML parses; all **five** shell blocks pass `bash -n`; the referenced Make targets `inventory`, `build`, `test`, `eval-smoke` and `demo` exist and resolve with `make -n`. All **44** explicitly referenced test paths exist. Referenced repository configuration/tooling files exist; environments, wheelhouse and output paths are produced by earlier steps or checked at runtime. Make/Ruff scope matches exactly across 138 existing paths.

The Linux job installs `--locked --dev --all-extras` and imports required extras before pytest, preventing missing imports from being silently accepted as module skips in CI. The Windows job lists the 44 portable v2/feature modules; none directly imports POSIX `fcntl` or frozen `watchlist.agent`. The v2 CLI dispatch is lazy and the reviewed locking path uses the portable abstraction. The POSIX v1/archive/preservation tests are absent from its list. This is static portability validation, not a native Windows execution result. Hosted CI, network-namespace setup and action downloads were not triggered or revalidated remotely. The existing workflow comment's unverified Windows uv checksum remains an existing open item.

## Failure groups and routing

Codes in the exhaustive inventory below:

- **L — sandbox `lsof` visibility:** `PreservationError: Cannot establish quiescence with lsof`; warnings say `can't stat() hugetlbfs file system /dev/hugepages` and `can't stat() mqueue file system /dev/mqueue`, followed by `Output information may be incomplete.` The utility deliberately rejects incomplete descriptor visibility in `scripts/preserve_v1.py::_lsof`. Final failures include direct preservation failures and archive setup errors. Coordinator: rerun in an ordinary Linux environment with working `lsof` mount/PID visibility. Do not suppress warnings or weaken the quiescence checks.
- **I — preservation preflight blocks fault injection:** seven tests receive the same preflight `PreservationError` before their intended mutation/fsync/copy point. Assertions are `0 > 0`, `0 == 2`, or `assert False` because the injection was never reached. Same environment fix as L; these are not evidence that a partial archive was accepted.
- **T — 30-second child-process deadline:** `subprocess.TimeoutExpired` in `tests/agent_support.py` subprocesses. The initial run had **85** failures/errors: 31 resume failures, 23 compatibility failures, 30 archive setup errors and one preservation failure. Shared-host contention/startup cost is the likely cause; subsequent completed runs are the relevant retry evidence. No deadlines or frozen source were changed.
- **A — API load-test contract mismatch (route to `p1-review`):** `tests/test_load_failure.py::test_many_concurrent_mixed_requests_never_500`, line 189, expected `workspace_busy`, received `operation_pending` in a 503 response. `src/watchlist/api/app.py::_Runtime.write` explicitly returns `503 operation_pending` when its HTTP wait expires while queued work continues, with retryability depending on idempotency. Likely a timing-sensitive mismatch between the test's blanket 503 assertion and the pending-write contract. Suggested owner fix: exercise this path deterministically and verify status lookup/idempotency and safe retry behavior; do not simply permit every 503 or blindly retry a non-idempotent case start. It passed in the second run; the final matrix records the restart outcome. No API/new-module file was edited here.
- **C — corpus fixture timestamp collision, fixed here:** `tests/test_corpus.py::test_file_change_during_read_is_rejected_even_when_content_is_identical` failed with `DID NOT RAISE CorpusValidationError` in the second run. A same-byte write can leave compared metadata unchanged within one filesystem timestamp tick. The fixture now starts with an old mtime so the intentional mutation is observable; required rejection and same-byte assertions remain.

- `initial-available`: **A: 1**, **I: 7**, **L: 41**, **T: 85**.
- `final-available`: **C: 1**, **I: 7**, **L: 72**.
- `final-restarted`: **I: 7**, **L: 72**.
- `final-all-extras`: **I: 7**, **L: 72**.

In the first three completed runs, the **156 skips** comprise **153** missing-private-evidence skips for `data/eval-comparisons/completion-08b-02/permissions/opensanctions_un.grant.json`, plus the Torch-dependent reranking test and two module-level skips:

- `tests/test_reranking.py::test_pair_tokenization_no_prefix_raw_logits_and_no_truncation`
- `tests/test_training.py` (collection skip)
- `tests/test_training_losses.py` (collection skip)

The final all-extras run passes all 36 training/training-loss cases and the formerly skipped reranking case; its remaining skips are the 153 private-evidence cases. The hook's private path was not broadened. The public/malformed/escape/permission/context/teardown cases demonstrably remain failures. The **three existing xfails** are `tests/test_security_hardening.py::test_bidi_controls_and_invisible_only_names_are_refused` with U+200E, U+200F and U+061C in `Luma…Vesper`; their recorded reason is that `review/validation.py::_BIDI_CONTROLS` omits those marks (review-service owner, completion-27). They were not newly marked or edited here.

## Git and coordinator handoff

`git status --short` fails because Linux Git 2.43 does not support this repository's `relativeworktrees` extension. The prescribed `git.exe status --short` also fails inside the sandbox with `WSL … UtilBindVsockAnyPort:307: socket failed 1`. Therefore **no commits were made**; the user explicitly authorized leaving these changes uncommitted for the coordinator. The worktree's HEAD file still references `refs/heads/p1-tests` at the starting commit. File-byte comparisons against saved originals and the initial source manifest were used to inventory changes; this is not a successful Git status claim.

Uncommitted files: `Makefile`, `pyproject.toml`, `tests/conftest.py`, `tests/test_private_evidence.py` (new), `tests/test_packaging.py`, `scripts/project_checks.py`, `tests/test_corpus.py`, and `docs/progress/test-run-linux.md` (new). Grouped patches are saved under `~/pw-p1-tests/evidence/patches/` for review. Suggested small commits: private-evidence hook/tests/scope; package-resource assertions; scanner exceptions/timeout; deterministic corpus fixture plus its scanner pin; report. Preserve the required `Co-Authored-By: GPT 6 Astra <noreply@openai.com>` trailer when committing.

Coordinator work remaining: rerun preservation/archive cases with valid `lsof` visibility; route A and the three existing xfails to the review engineer; integrate both branches and repeat affected checks. Exact-content scanner exceptions will need re-review if the other branch changes the pinned files. The all-extras run is complete, but no green-suite or release acceptance is asserted here.

What/why: run the complete offline all-extras suite and repair test infrastructure without hiding real errors. Who/where: the Project 1 test engineer on `p1-tests`, with the Linux mirror and isolated environments above. When: initial, post-infrastructure, interrupted, restarted and final all-extras runs as recorded in receipts. How: offline dependency resolution, isolated pytest shards, exact artifact/resource checks, a narrow private-evidence hook and retained failure evidence. The next work belongs to the coordinator and the new-module review owner; no new project phase was started.

## Exhaustive failure/error inventory

`F` means a failed test call; `E` means an error (typically fixture setup); the following letter is the cause above. `pass` is an observed successful result from that completed run. Each row is an exact pytest node ID that failed/errored in at least one complete run. Full error text is retained in that run's `summary.json`, shard log and JUnit XML.

### L — Sandbox descriptor visibility (72 node IDs)

| Exact node ID | Initial | After infrastructure | Restart available | Final all extras |
| --- | --- | --- | --- | --- |
| `tests/test_preserve_v1.py::test_archive_corruption_is_rejected_before_restore[extra]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_archive_corruption_is_rejected_before_restore[incomplete]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_archive_corruption_is_rejected_before_restore[missing]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_archive_corruption_is_rejected_before_restore[tamper]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_archive_is_private_under_permissive_umask_and_uses_independent_files` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_archive_member_cannot_reference_an_external_inode[hardlink]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_archive_member_cannot_reference_an_external_inode[symlink]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_cli_apply_verify_and_restore` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_cli_dry_run_and_conflicting_actions` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_dangling_internal_runtime_link_is_declared_and_restored_exactly` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_dry_run_does_not_create_or_modify_any_project_file` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_hot_rollback_journal_recovers_committed_rows_without_modifying_sources` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_idle_sqlite_reader_prevents_claiming_quiescence` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_insufficient_space_is_rejected_before_output_creation` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_logically_corrupt_store_is_not_archived_as_verified[audit]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_logically_corrupt_store_is_not_archived_as_verified[extension_outside_interrupt]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_logically_corrupt_store_is_not_archived_as_verified[missing_checkpoint_parent]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_logically_corrupt_store_is_not_archived_as_verified[packet]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_logically_corrupt_store_is_not_archived_as_verified[submission]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_logically_corrupt_store_is_not_archived_as_verified[typed_pickle]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_missing_schema_constraints_are_rejected_even_with_valid_rows[audit_events- REFERENCES cases(audit_id)]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_missing_schema_constraints_are_rejected_even_with_valid_rows[audit_events-PRIMARY KEY(audit_id, sequence),]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_missing_schema_constraints_are_rejected_even_with_valid_rows[cases- UNIQUE]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_normal_archive_verify_and_restore_preserve_case_and_source` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_cannot_omit_a_captured_database` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_duplicate_paths` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_escaped_or_cyclic_runtime_links[absolute_escape]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_escaped_or_cyclic_runtime_links[cycle]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_escaped_or_cyclic_runtime_links[prefix_cycle]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_escaped_or_cyclic_runtime_links[relative_escape]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_unsafe_paths[../escape.txt]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_unsafe_paths[/tmp/preservation-absolute-path-escape]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_unsafe_paths[payload/project/../../escape.txt]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_wrong_member_types[changes0]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_wrong_member_types[changes1]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_resigned_manifest_rejects_wrong_member_types[changes2]` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_restore_cannot_nest_inside_archive` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_restore_never_overwrites_an_existing_destination` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_root_secret_files_are_not_copied` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_runtime_link_through_linked_directory_restores_captured_target` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_sqlite_connections_never_target_original_stores` | FL | FL | FL | FL |
| `tests/test_preserve_v1.py::test_wal_only_committed_case_survives_backup_and_restore` | FT | FL | FL | FL |
| `tests/test_review_archive.py::test_archive_cli_reads_complete_fictional_archive[inspect]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_archive_cli_reads_complete_fictional_archive[verify]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_archive_cli_unknown_case_is_a_concise_failure` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_archive_member_links_are_rejected[hardlink]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_archive_member_links_are_rejected[symlink]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_clean_subprocess_reader_performs_no_writes_setup_or_heavy_imports` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_different_current_workflow_does_not_block_historical_inspection[completed]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_different_current_workflow_does_not_block_historical_inspection[interrupted]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_different_current_workflow_does_not_block_historical_inspection[pending]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_inspect_reads_completed_pending_and_interrupted_evidence[completed]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_inspect_reads_completed_pending_and_interrupted_evidence[interrupted]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_inspect_reads_completed_pending_and_interrupted_evidence[pending]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_missing_completion_or_unknown_archive_contract_is_rejected[manifest_schema]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_missing_completion_or_unknown_archive_contract_is_rejected[marker_schema]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_missing_completion_or_unknown_archive_contract_is_rejected[missing_complete]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_missing_completion_or_unknown_archive_contract_is_rejected[receipt_schema]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_normalized_database_sidecars_are_rejected_without_recovery[-journal]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_normalized_database_sidecars_are_rejected_without_recovery[-shm]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_normalized_database_sidecars_are_rejected_without_recovery[-wal]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_resigned_manifest_requires_both_raw_and_normalized_database_members[databases]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_resigned_manifest_requires_both_raw_and_normalized_database_members[payload/project]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_resigned_manifest_traversal_is_rejected[../escape]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_resigned_manifest_traversal_is_rejected[/fictional/absolute/escape]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_resigned_manifest_traversal_is_rejected[payload/project/../../escape]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_tampered_missing_or_extra_archive_members_are_rejected[changed]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_tampered_missing_or_extra_archive_members_are_rejected[extra]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_tampered_missing_or_extra_archive_members_are_rejected[missing]` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_unavailable_current_workflow_is_reported_separately_from_archive_integrity` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_unknown_case_id_is_rejected_without_mutation` | ET | EL | EL | EL |
| `tests/test_review_archive.py::test_verify_returns_only_case_catalogue_and_integrity_summary` | ET | EL | EL | EL |

### I — Preflight prevents intended fault injection (7 node IDs)

| Exact node ID | Initial | After infrastructure | Restart available | Final all extras |
| --- | --- | --- | --- | --- |
| `tests/test_preserve_v1.py::test_copy_failure_never_produces_complete_archive` | FI | FI | FI | FI |
| `tests/test_preserve_v1.py::test_fsync_failure_never_produces_complete_archive` | FI | FI | FI | FI |
| `tests/test_preserve_v1.py::test_late_failure_never_leaves_a_valid_completion_marker[backup]` | FI | FI | FI | FI |
| `tests/test_preserve_v1.py::test_late_failure_never_leaves_a_valid_completion_marker[last_directory_fsync]` | FI | FI | FI | FI |
| `tests/test_preserve_v1.py::test_late_failure_never_leaves_a_valid_completion_marker[lock_recheck]` | FI | FI | FI | FI |
| `tests/test_preserve_v1.py::test_source_change_during_capture_prevents_completion[sidecar_membership]` | FI | FI | FI | FI |
| `tests/test_preserve_v1.py::test_source_change_during_capture_prevents_completion[source_bytes]` | FI | FI | FI | FI |

### T — Earlier child-process timeouts (54 node IDs)

| Exact node ID | Initial | After infrastructure | Restart available | Final all extras |
| --- | --- | --- | --- | --- |
| `tests/test_review_compatibility.py::test_actual_historical_stages_are_inspected_without_recovery[after_case-created_before_first_checkpoint]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_actual_historical_stages_are_inspected_without_recovery[after_final_audit-final_audit_pending_checkpoint]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_actual_historical_stages_are_inspected_without_recovery[after_packet-packet_saved_pending_review]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_actual_historical_stages_are_inspected_without_recovery[after_submission-submission_accepted_pending_completion]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_actual_historical_stages_are_inspected_without_recovery[completed-completed_audit_checkpoint]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_actual_historical_stages_are_inspected_without_recovery[during_explanation-submission_accepted_pending_completion]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_actual_historical_stages_are_inspected_without_recovery[pending-packet_saved_pending_review]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_current_code_changes_do_not_block_reading_but_still_block_execution` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_empty_checkpoint_exception_cannot_hide_lost_pending_history` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_mixed_ledger_and_checkpoint_stores_are_rejected` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_reader_runs_without_third_party_packages_or_runtime_class_revival` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_reader_uses_only_immutable_sqlite_and_never_writes_or_executes` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_rehashed_creation_only_case_requires_a_valid_initial_state[candidates-not a list]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_rehashed_creation_only_case_requires_a_valid_initial_state[query-None]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_rehashed_creation_only_case_requires_a_valid_initial_state[review_status-recorded]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_runtime_extensions_never_revive_classes[unknown_extension]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_semantically_invalid_packet_is_rejected_even_with_rehashed_receipts` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_semantically_invalid_submission_is_rejected_even_with_rehashed_audit` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_unknown_case_is_an_explicit_read_only_error` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_unknown_checkpoint_serializer_is_rejected_as_stored_data[future_serializer]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_unknown_checkpoint_serializer_is_rejected_as_stored_data[pickle]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_unknown_database_schema_is_rejected[unexpected_table]` | FT | pass | pass | pass |
| `tests/test_review_compatibility.py::test_unknown_database_schema_is_rejected[user_version]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_concurrent_review_one_winner` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_killed_process_recovery[after_case]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_killed_process_recovery[after_final_audit]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_killed_process_recovery[after_packet]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_killed_process_recovery[after_submission]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_killed_process_recovery[during_explanation]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_process_restart_pause_resume_and_duplicate` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_crash_windows_preserve_evidence_without_retrieval[after_case]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_crash_windows_preserve_evidence_without_retrieval[after_final_audit]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_crash_windows_preserve_evidence_without_retrieval[after_packet]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_crash_windows_preserve_evidence_without_retrieval[after_submission]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_crash_windows_preserve_evidence_without_retrieval[during_explanation]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[delete-all-history-accepted]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[delete-all-history-completed]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[delete-all-history-pending]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[delete-target-history-accepted]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[delete-target-history-completed]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[delete-target-history-pending]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[empty-schema-replacement-accepted]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[empty-schema-replacement-completed]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_lost_checkpoint_history_unchanged[empty-schema-replacement-pending]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_missing_or_truncated_storage_unchanged[missing-cases.sqlite]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_missing_or_truncated_storage_unchanged[missing-checkpoints.sqlite]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_missing_or_truncated_storage_unchanged[missing-writer.lock]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_missing_or_truncated_storage_unchanged[truncated-cases.sqlite]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_missing_or_truncated_storage_unchanged[truncated-checkpoints.sqlite]` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_other_workspace_checkpoint_generation` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_prior_peer_generation_even_for_healthy_target` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_rejects_real_pre_retrieval_checkpoint_rollback` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_resumes_input_only_checkpoint_without_new_input` | FT | pass | pass | pass |
| `tests/test_review_resume.py::test_recovery_subprocess_uses_saved_retrieval_before_packet_commit[after_retrieval_writes]` | FT | pass | pass | pass |

### A — API pending-write contract (1 node IDs)

| Exact node ID | Initial | After infrastructure | Restart available | Final all extras |
| --- | --- | --- | --- | --- |
| `tests/test_load_failure.py::test_many_concurrent_mixed_requests_never_500` | FA | pass | pass | pass |

### C — Corrected corpus fixture (1 node IDs)

| Exact node ID | Initial | After infrastructure | Restart available | Final all extras |
| --- | --- | --- | --- | --- |
| `tests/test_corpus.py::test_file_change_during_read_is_rejected_even_when_content_is_identical` | pass | FC | pass | pass |


### Interrupted-run observed failures

The interrupted run is incomplete. These seven failed call reports are retained, not extrapolated into suite totals:

- `tests/test_review_resume.py::test_killed_process_recovery[after_packet]` — call failure, **T**.
- `tests/test_review_compatibility.py::test_actual_historical_stages_are_inspected_without_recovery[after_final_audit-final_audit_pending_checkpoint]` — call failure, **T**.
- `tests/test_preserve_v1.py::test_dry_run_does_not_create_or_modify_any_project_file` — call failure, **L**.
- `tests/test_preserve_v1.py::test_sqlite_connections_never_target_original_stores` — call failure, **L**.
- `tests/test_preserve_v1.py::test_idle_sqlite_reader_prevents_claiming_quiescence` — call failure, **L**.
- `tests/test_preserve_v1.py::test_archive_is_private_under_permissive_umask_and_uses_independent_files` — call failure, **L**.
- `tests/test_preserve_v1.py::test_root_secret_files_are_not_copied` — call failure, **L**.
