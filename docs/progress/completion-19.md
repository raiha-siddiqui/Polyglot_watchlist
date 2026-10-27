# Completion Step 19 — one versioned v2 review service for all interfaces

Date: 2026-10-26, Asia/Kolkata. Status: **v2 service mechanics implemented and tested on fictional data only. Step 19's acceptance is not fully closed**: there is no v1→v2 archive/migration path, no adapter parity test against the v1 E5 workflow, and Steps 24–26 (API/UI/MCP) have not yet been built on it. All numbers below come from commands actually run in this step on the Windows development machine. They are regression evidence for software behavior, not matching-quality or reviewer-benefit results.

## Goal

Give the CLI, and next the FastAPI, Streamlit, MCP and memory layers, **one** stable Python API for review cases. That API owns typed contracts, storage, workflow, recovery and audit, so each interface does not copy the orchestration. The design record is [Decision 0007](../adr/0007-review-service-v2.md).

Requirements, as assigned:

- explicit contract versions, with workflow identity = declared version + config digest (not a source-byte hash);
- a case `origin` field;
- a retriever adapter protocol: RapidFuzz over the packaged fictional entities by default, with an optional lazy E5 adapter;
- a LangGraph StateGraph with SqliteSaver and an interrupt for human review;
- a hash-chained audit log;
- a portable portalocker lock with a bounded wait;
- explicit init vs open vs read-only;
- structured errors;
- idempotent, stale-safe submissions;
- crash recovery that never re-retrieves;
- read APIs;
- a context-provider hook.

The v1 agent files were **not modified**. v1 keeps its strict execution guard for its own archived cases.

## Files

New: `src/watchlist/review/{contracts.py, validation.py, locking.py, storage.py, schema.sql, recovery.py, workflow.py, nodes.py, service.py}`, `config/review-v2.json`, `tests/review_v2_support.py`, `tests/test_review_v2_{contracts,service,recovery,concurrency,cli,portability}.py`, `docs/adr/0007-review-service-v2.md`, this report.
Modified: `src/watchlist/review/cli.py`, which adds a `v2` command group. The archive parser and code path are byte-for-byte the same apart from the two-line `v2` dispatch at the top of `main` and an extended module docstring.

After an adversarial review, a follow-up pass changed the same owned files. See "Adversarial review follow-up" below for each issue and its outcome.

### `contracts.py` (standard library only)

- **Version constants:**
  - `WORKSPACE_FORMAT = 'review-workspace-v2'`
  - `CASE_SCHEMA`, `PACKET_SCHEMA`, `SUBMISSION_SCHEMA`, `AUDIT_SCHEMA`
  - `WORKFLOW_VERSION = 'review-workflow-v2.0'`
  - `CONFIG_VERSION`, `EXPLANATION_TEMPLATE = 'evidence-v2'`
  - `ORIGINS`, `OUTCOMES`, `CASE_STATUSES`
- **Errors:** the `ReviewError` hierarchy, each with a stable `code`, `retryable`, `details` and `to_dict()`. The classes are:
  - `ValidationError`, `NotFoundError`, `WorkspaceError`, `WorkspaceBusyError`, `ReadOnlyError`
  - `VersionMismatchError`, `InvalidStateError`, `StaleSubmissionError`, `SubmissionConflictError`
  - `RecoveryRequiredError`, `StorageIntegrityError`, `RetrieverMismatchError`, `RetrievalError`, `ContextProviderError`
  - `RequestConflictError` (`request_conflict`) and `ReentrantCallError` (`reentrant_call`), added in the follow-up
  - `ReviewError(..., retryable=None)` can override `retryable` per instance.
- **JSON helpers:**
  - `canonical`/`digest` use the same byte definition as v1.
  - `loads_strict` rejects duplicate keys and NaN.
  - `json_value` makes a finite, detached copy of a JSON value.
  - Field validators reject booleans as integers and non-canonical UUIDs.
- **Frozen dataclasses**, each with `to_dict()` (pure JSON, lists instead of tuples) and a strict `from_dict()` (exact keys):
  - `WorkflowIdentity`, `WorkflowPolicy` (hashed; `allowed_origins` kept in canonical order), `RetrieverSettings`, `OperationsSettings` (`lock_timeout_seconds`, `max_cases`, `page_max`; not hashed)
  - `ReviewConfig`, with `load(path)` and `workflow_identity()`
  - `RetrieverDescriptor`, `AliasEvidence`, `Candidate`, `RetrievalResult`, `ContextRequest`
  - `ReviewPacket`, whose `.sha256` is the packet digest
  - `ReviewSubmission`, which is validated in `__post_init__`
  - `AcceptedReview`, `Cancellation`, `Explanation`, `CaseHeader`, `AuditReceipt`
  - `CaseView`, `CaseSummary`, `AuditEvent`, `AuditView`, `AuditAnchor` (validated in `__post_init__`, `count >= 1`), `AuditVerification`
  - `CaseHeader.request_id` holds the optional client UUID of `start_case`.
- **Protocols:** `Retriever` (`descriptor()`, `search(normalized_name, *, top_k)`) and `ContextProvider` (`name`, `version`, `provide(ContextRequest)`).

### `validation.py`

Policy and saved-state checks, all raising `ValidationError`:

- `validate_name` returns the original name and its normalized form; blank or oversized names are rejected before a case exists.
- `validate_origin`: the origin is explicit and must be in the policy's allowed list.
- `visible_text` rejects control characters (reasons may keep `\n`, `\r`, `\t`), bidi embedding/override/isolate controls, and text with no visible character. Names and reasons keep format characters such as ZWJ/ZWNJ, which Persian and Indic spellings need; actors reject all format characters.
- `validate_actor`, `validate_reason`, `validate_case_id`, `validate_request_id`.
- `validate_page(limit, offset, operations, origin=None)`: `limit=None` means `min(50, page_max)`; `origin` must be a known origin.
- `coerce_submission` accepts a dataclass or a dict.
- `validate_retrieval` checks:
  - the descriptor matches the frozen one and the result respects `top_k`;
  - ranks are consecutive and IDs unique;
  - evidence links to the right entity and the winning alias is present;
  - there is no orphan evidence;
  - non-synthetic evidence has provenance.
- `validate_providers`, `validate_context` (finite JSON object within the byte budget).
- `validate_submission_for_packet`: a different packet digest raises `StaleSubmissionError`; the submission origin must equal the case (packet) origin; a follow-up candidate must be in the shortlist.
- `same_submission`.

### `locking.py`

- `validated_timeout` accepts finite non-negative real numbers and rejects bools, NaN, inf and strings.
- `create_lock_file` creates the lock file exclusively.
- `workspace_lock(root, timeout)`:
  - takes a portalocker `EXCLUSIVE|NON_BLOCKING` lock on a read-only handle;
  - polls every 20 ms against a monotonic deadline;
  - raises `WorkspaceBusyError` (retryable) on timeout; only `portalocker.AlreadyLocked` is retried, and any other `LockException` is `WorkspaceError` at once;
  - keeps a per-thread set of held lock files, so a nested acquisition by the holding thread raises `ReentrantCallError` at once instead of waiting out the timeout.
- Before writing this module I checked locally that the lock blocks a second handle in the same process on Windows.
- POSIX uses `fcntl.flock` through portalocker. That backend was **not run in this step**; see Limitations.

### `storage.py` + `schema.sql`

- One `review.sqlite` holds the ledger tables, `audit_events`, and the exact `checkpoints`/`writes` tables of langgraph-checkpoint-sqlite 3.x. Its identity markers are `application_id` 0x50575232 and `user_version` 2.
- Triggers block `UPDATE` and `DELETE` on every ledger table (`cases`, `evidence`, `packets`, `submissions`, `cancellations`, `audit_events`) and forbid a case from having both a submission and a cancellation. `cases.request_id` is a nullable `UNIQUE` column.
- Rollback-journal mode is used and the saver's WAL-enabling `setup()` is never called, so `mode=ro` reads create no sidecar files.
- `create_workspace` is the only function that creates files. It requires a missing or empty directory and creates the lock file exclusively first.
- `check_workspace` checks without creating anything. `connect` uses `mode=rw`/`mode=ro`. `connect_checkpoints` opens the saver's own connection.
- `verify_identity` checks, before every operation:
  - application ID and user version;
  - not in WAL mode;
  - whitespace-normalized `sqlite_master` signature equals `schema.sql`;
  - `workspace_meta` versions;
  - the installed saver's major version;
  - `catalogue_issues`: no ledger rows without a case, no checkpoint/write rows without a case thread, and no gap in the case sequence. Any finding raises `StorageIntegrityError`.
- `sqlite_failure` maps a SQLite `locked`/`busy` error to `WorkspaceBusyError` (retryable) and anything else to `StorageIntegrityError`.
- Workspace paths are normalized with `os.path.abspath`, so `../data/review-v2` works.
- `verify_chain` recomputes each envelope hash and its previous-hash link, and checks payloads are canonical.
- `Ledger` writes each record in one short `BEGIN IMMEDIATE` transaction:
  - `create_case` assigns a stable `seq` used for listing order.
  - `record_evidence`, `record_packet` and `finalize` are idempotent: a byte-equal replay is a no-op and a conflicting replay raises `StorageIntegrityError`.
  - `record_submission` maps `UNIQUE` and trigger failures to stale, conflict or invalid-state errors.
  - `record_cancellation`.
  - `snapshot` returns the raw rows for assessment.

### `recovery.py`

- `assess_case(snapshot, facts, identity)` is pure. It cross-checks:
  - the header digest;
  - every row's digest and its matching audit event;
  - packet ↔ evidence ↔ header, review ↔ packet/shortlist, and review origin = case origin;
  - header `request_id` and `origin` against the catalogue row;
  - cancellation exclusivity and the final payload;
  - checkpoint values against the ledger: the checkpoint may lag one durable step behind but may never contradict it;
  - the final receipt.

  It returns `preparing / awaiting_review / finalizing / completed / cancelled / incompatible / inconsistent`, plus `next_action` and `recovery_required`.
- `plan_recovery` returns `none`, `start` (no checkpoint), `continue` (a saved task result or any unfinished node) or `resume_review` (a pending interrupt with an accepted review).
- `final_payload` binds the packet digest, the review digest and the explanation.

### `workflow.py` + `nodes.py`

- `workflow.py` sets the same offline/tracing environment variables as v1 and builds the graph `normalize → retrieve → format_packet → prepare_review → human_review → explain → audit`, compiled with `SqliteSaver` (`is_setup=True`).
- `checkpoint_facts` inspects the explicit latest checkpoint ID, as v1 learned to do.
- Node behavior:
  - `retrieve` **returns saved ledger evidence whenever it exists**. Otherwise it checks the frozen `RetrieverDescriptor`, calls the retriever, validates the result and commits it.
  - `format_packet` freezes context-provider output. Every provider exception, including a `ReviewError`, becomes `ContextProviderError` with `details.cause`; a `reentrant_call` cause is not retryable.
  - `human_review` calls `interrupt()` unconditionally and accepts only the durable accepted review.
  - `explain` is the deterministic `evidence-v2` template.
- Retriever adapters:
  - `RapidFuzzRetriever` wraps `watchlist.matching.rank_candidates`. It reports scores as `normalized_levenshtein_similarity_x100` and gives evidence readable IDs `entity/alias`.
  - `E5Retriever` imports nothing heavy until `descriptor()`/`search()`.
  - `make_retriever(settings)` builds the configured adapter.

### `service.py`

- `ReviewService` holds no open handle between calls. Each operation runs: lock → fresh connections → identity check → work → close → unlock.
- Retrieval runs inside the workspace lock but outside SQLite write transactions.
- Before a review is accepted, the same `submission_id` together with the stored packet hash makes duplicate requests safe to repeat (idempotent).
- `start_case(..., request_id=UUID)` is idempotent. A replay of the same name/origin/actor returns that case, recovering it through the shared `_recover_in` if it is still `preparing`. A different request with the same ID is `RequestConflictError`. Without a `request_id`, a failure after the case row commits is returned with `retryable: false` and `details.next_action = 'recover'`.
- `list_cases(limit=None, offset=0, *, origin=None)` filters by origin in SQL.
- If a crashed process left a SQLite journal behind, read-only calls raise `RecoveryRequiredError`; a writable `open` lets SQLite roll the journal back.

### `cli.py` (v2 group)

`python -m watchlist.review v2 {init,start,status,list,review,recover,cancel,audit,verify} --workspace PATH [--config PATH]`:

- `--config` defaults to the repository `config/review-v2.json`, resolved from the source file rather than the current directory. An installed copy without that file gets `cannot read review config`; pass `--config` then.
- `start --request-id UUID` and `list --origin ORIGIN`; `list --limit` defaults to `min(50, page_max)`.

- `status`, `list`, `audit` and `verify` open the workspace read-only.
- Domain errors print `{"error": code, "message", "retryable", "details"}` to stderr and exit 1.
- `--origin` is limited to the four origins at parse time (argparse exit 2).

## Public API

```python
ReviewService.initialize(workspace, config, *, retriever=None, context_providers=(), clock=None,
                         failpoint=None, lock_timeout=None) -> ReviewService
ReviewService.open(workspace, config, *, read_only=False, **same_options) -> ReviewService
service.start_case(name: str, *, origin: str, actor: str, request_id: str | None = None) -> CaseView
service.get_case(case_id: str) -> CaseView
service.list_cases(limit: int | None = None, offset: int = 0, *, origin: str | None = None) -> list[CaseSummary]
service.submit_review(case_id: str, submission: ReviewSubmission | Mapping) -> CaseView
service.recover(case_id: str) -> CaseView
service.cancel_case(case_id: str, *, actor: str, reason: str) -> CaseView
service.audit(case_id: str) -> AuditView
service.verify_audit(case_id: str, anchor: AuditAnchor | Mapping | None = None) -> AuditVerification
properties: workspace, config, workflow_identity, read_only, context_providers
```

`config` is a `ReviewConfig` or a path to one.

## Commands actually run and results

All commands were run from `D:\Projects\raiha\Polyglot_watchlist` with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1` and the Windows venv (Python 3.14.9; langgraph 1.2.16, langgraph-checkpoint-sqlite 3.1.2, portalocker 4.4.1, rapidfuzz 3.14.6).

1. **Full v2 suite (final, after the review follow-up).** Final result: **169 passed in 345.23 s**, 0 failed. Per file (`--collect-only`): contracts 71, service 60, recovery 11, concurrency 14, cli 6, portability 7. The pre-review run was 132 passed in 334.25 s.

   ```
   .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_review_v2_contracts.py tests/test_review_v2_service.py tests/test_review_v2_recovery.py tests/test_review_v2_concurrency.py tests/test_review_v2_cli.py tests/test_review_v2_portability.py
   ```

   During the follow-up, one existing test failed and was changed. `test_lock_wait_is_bounded_in_process` simulated contention by taking the lock again in the same thread, which is now correctly a `ReentrantCallError`. The test now holds the lock from a second thread.

   The slowest tests were the subprocess ones, about 22–28 s each: six crash windows, the three-process race and the `-m` entry point. Most of that time is cold interpreter/LangGraph import in each child process while other engineers' jobs were running on the same machine.

   Failures seen during development, before the final run:
   - One wrong test expectation, since fixed. A failpoint placed after a fresh retrieval cannot fire on a replay, because the replay reuses evidence.
   - One over-broad portability check, which flagged the word `fcntl` inside a docstring. It now checks real imports with `ast`.
2. **Lint.** `.tools/qa/Scripts/ruff.exe check --no-cache --no-force-exclude --select E4,E7,E9,F --target-version py314 <all v2 sources, cli.py and tests>` printed "All checks passed!". It was re-run after the follow-up.
3. **Secret scan.** `.tools/qa/Scripts/detect-secrets-hook.exe <all v2 sources, schema, config and tests>` exited 0 with no findings. It was re-run after the follow-up.
4. **Archive CLI sanity.** `python -B -m watchlist.review archive verify --archive C:/nonexistent-archive` printed `{"error": "Expected a private archive directory"}` and exited 1, which is unchanged behavior.
5. **Existing archive tests not run on Windows.** `pytest tests/test_review_archive.py -k cli` **could not be collected on Windows**: the existing v1 `watchlist/agent/storage.py` imports `fcntl`. This is pre-existing and not caused by this step. Those tests still need to be run on Linux.
6. **Timing profile.** In-process timings of one fictional lifecycle, measured on this machine and not a benchmark:
   - `initialize` 1.26 s, `start_case` 0.83 s, `get_case` 0.20 s, `submit_review` 1.02 s;
   - the first cold import of the service plus LangGraph took 31.4 s;
   - each SQLite commit took about 12 ms with full sync.

## Example input/output

This is the actual CLI run on the authored fictional dataset; the case ID and timestamps differ per run.

```
v2 init  --workspace <scratch>/demo-v2                        -> {"status": "initialized", ...}
v2 start --name "Lumo Vespar" --origin simulated_demo --actor fictional-demo-reviewer
  -> awaiting_review, next_action submit_review
     1 syn-002 Lumo Vespar 100.0 | 2 syn-001 Luma Vesper 81.82 | 3 syn-012 Luma Vesper 81.82
     4 syn-006 Naro Zefir 36.36  | 5 syn-003 Ẹlo Nivara 27.27   (normalized Levenshtein x100, not probability)
v2 review --submission form.json   (candidate_for_follow_up, syn-002)
  -> completed; "Saved retrieval (rapidfuzz: watchlist.matching.rank_candidates) returned 5 candidate records for
     review. Reviewer label "fictional-demo-reviewer" (origin: simulated_demo) recorded candidate_for_follow_up with
     selected record "syn-002". ... Evidence references: syn-002/syn-002-a1, syn-002/syn-002-a2. Packet SHA-256: ..."
v2 review (same form again)   -> completed  (idempotent replay, no second effect)
v2 verify                     -> ok true, count 5
v2 list                       -> one summary: origin simulated_demo, status completed, workflow review-workflow-v2.0
```

The follow-up re-ran `init` and `start` for "Lumo Vespar" in a fresh scratch workspace. It returned the same five candidates and scores: syn-002 100.0, syn-001 81.82, syn-012 81.82, syn-006 36.36 and syn-003 27.27. A review form for that `simulated_demo` case with `"origin": "human"` was then rejected (exit 1) and nothing was written:

```
{"details": {"case_origin": "simulated_demo", "submission_origin": "human"}, "error": "invalid_input",
 "message": "submission origin must equal the case origin", "retryable": false}
v2 list --origin human   -> {"cases": []}
```

## What the tests cover

- **Contracts.** 71 contract tests cover:
  - round trips;
  - missing/extra/wrong-type fields, non-canonical UUIDs, uppercase digests and invented outcomes such as `match`/`cleared`;
  - candidate/outcome rules, lone surrogates, duplicate JSON keys and NaN;
  - the identity being stable under reformatting, key order, `operations` changes and `allowed_origins` reordering, and changing with policy changes;
  - control, bidi and invisible-only names/actors/reasons rejected while ZWNJ names and multi-line reasons pass;
  - direct `AuditAnchor` construction validated.
- **Lifecycle.**
  - The default RapidFuzz retriever returns the two `Luma Vesper` records (`syn-001`, `syn-012`) as separate candidates.
  - An empty result still requires review.
  - The explanation contains no identity or clearance claims.
  - Listing is ordered and omits names.
- **Workspace modes.**
  - `initialize` refuses non-empty directories.
  - `open`, both read-only and writable, never creates a missing workspace or repairs an incomplete one.
  - Read-only inspection leaves every byte and mtime unchanged, with no sidecar files.
  - Every mutation fails with `ReadOnlyError`.
  - Repeated status reads of a crashed `preparing` case have no side effects.
- **Submissions.**
  - An identical replay is idempotent.
  - The same ID with different content conflicts, and so does an ID reused across cases.
  - A second review is stale, and a wrong packet digest is stale with no writes.
  - Cancellation blocks review and is idempotent.
- **Versions and adapters.**
  - A policy change makes a pending case `incompatible`: submit and recover are refused, the case is readable and cancellable, and completed cases stay `completed`.
  - A retriever change after evidence exists leaves the packet unchanged.
  - Foreign or mismatched meta, `user_version`, `application_id` or extra tables are rejected at open.
- **Recovery.**
  - Real `os._exit` crashes at all six failpoints.
  - Zero retrievals after evidence exists.
  - One final event; the same receipt after a final-event crash.
  - Recovery is idempotent with no file changes on repeat.
  - Recovery before evidence refuses a different retriever.
  - Checkpoint history deleted under ledger progress is detected as `inconsistent` with no retrieval.
  - A hot journal from a crash inside a transaction is rejected read-only and rolled back by a writable open.
- **Concurrency.**
  - Six threads racing different reviews, with a shared service or separate instances: exactly one completes and five are stale.
  - Five threads retrying one form produce one effect.
  - Four threads create cases concurrently.
  - Three **processes** race: exactly one accepted and two stale.
  - Lock timeouts are bounded both in-process and against a lock held by another process, which is released when that process is killed.
- **Audit.**
  - Edited payloads are detected, and mutations are then refused.
  - A rehashed forged submission is detected through its audit event.
  - A truncated tail is detected through an anchor.
  - Append-only triggers block ordinary edits.
- **Review follow-up regressions.**
  - A review origin different from the case origin is rejected with no file change, and `list_cases(origin=...)` partitions cases.
  - `page_max`, `max_cases` and origin order changes keep a pending case compatible and submittable.
  - The default `list_cases()` works with `page_max=20`.
  - A provider calling back into the service fails fast as `context_provider_failed` with cause `reentrant_call` and `retryable: false`. A provider raising `stale_submission` is wrapped. Both cases recover once the provider is fixed.
  - Three failed `start_case` calls with one `request_id` leave one case; the fourth call completes it. Six threads starting with one `request_id` create one case. A different request with that ID is `request_conflict`. Without an ID, the error is recover-only.
  - `DELETE` and `UPDATE` on each of the six ledger tables are blocked. A case deleted with its trigger dropped makes `open`, `get_case` and `list_cases` raise `storage_inconsistent`.
  - An external `BEGIN EXCLUSIVE` is `workspace_busy` (retryable). A non-contention `LockException` is `workspace_invalid`.
  - The CLI works from another directory without `--config`, and with a `../relative-ws` workspace.
- **Windows and imports.**
  - Paths with spaces, umlauts and CJK characters work, and RTL and mixed-script names survive a JSON round trip.
  - No POSIX-only imports, and every `read_text` call passes an encoding.
  - `import watchlist.review.service` loads no langgraph, portalocker or torch.
  - The E5 adapter stays lazy and fails with `RetrievalError` without a model.

## Limitations

- **Platforms.**
  - Tested only on Windows in this step. The POSIX `flock` path and the v1-dependent archive tests need a Linux run.
  - The rollback-journal-only design relies on SQLite's standard locking; network filesystems are unsupported.
- **Lock and concurrency.**
  - The lock is cooperative and exclusive even for readers, so reads serialize.
  - Retrieval runs inside the lock, so E5 inference would lengthen lock hold times.
  - The exactly-one guarantee was tested at small scale only: 6 threads and 3 processes.
- **Workflow identity.** Identity relies on developers bumping `WORKFLOW_VERSION` when node semantics change. The service cannot detect an un-bumped code change.
- **Integrity checks.**
  - `get_case`/`verify_audit` check one case in depth. The per-operation catalogue check finds removed cases and orphan rows by count only; it does not fully re-verify every case.
  - Local hash chains plus triggers are tamper *evidence* against accidental or partial edits. An owner who rewrites the whole file can recompute everything; only an anchor kept elsewhere detects that.
- **Actor and E5.**
  - The actor is a caller-supplied label, not authentication. Step 24 must derive it from authenticated context.
  - The E5 adapter was not run against real weights in this step.
- **No migration.** There is no v1→v2 migration or archive import; v1 cases stay v1, inspectable through Decision 0006's reader.
- **Default retriever.** The v2 config defaults to RapidFuzz, as this step was assigned. Plan item 4 says the production default stays E5. Decision 0007 records this difference, and the production choice stays open until the E5 parity run.
- **Origin binding is strict equality.** A human reviewing a `test` case must submit `origin: test`. If cross-origin reviews are ever needed, they need an explicit, separately approved rule.
- **Re-entrancy detection is per thread.** A provider that calls the service from another thread still waits for the lock timeout and then gets `workspace_busy`.
- **Config shape change.** `max_cases`/`page_max` moved from `workflow` to `operations` inside `review-config-v2` before any interface used them. Workspaces created by the pre-review build are not compatible: the case header and `cases` table gained `request_id`, and the schema signature check rejects them. They held fictional test data only.
- **Unpinned langgraph internals.** `checkpoint_facts` relies on LangGraph exposing saved task results through `get_state(checkpoint_id=...)`, as v1 does. It is covered by the crash tests on langgraph 1.2.16 only.

## What remains BLOCKED or open

- **Real reviewed data (blocked on Step 08).** Running v2 over the real reviewed multilingual corpus, and any claim about reviewer usefulness, needs that corpus. Everything here uses the authored fictional entities.
- **Step 19 acceptance items still open:**
  - v1 adapter parity, or a documented compatibility wrapper for the `watchlist.agent` CLI;
  - an archive/migration path that creates linked v2 cases;
  - frozen model-bundle integration (an E5 parity run with approved local weights);
  - a Linux CI run of these tests.

## Shared-file changes requested (not made by me)

- Add `src/watchlist/review/{contracts,validation,locking,storage,recovery,workflow,nodes,service}.py` and `tests/test_review_v2_*.py`, `tests/review_v2_support.py` to the ruff `include` list in `pyproject.toml`. They already pass `E4,E7,E9,F`.
- `portalocker==4.4.1` appears in the `agent` extra only in the **uncommitted** working-tree diff of `pyproject.toml`, not at HEAD. The owner of `pyproject.toml`/`uv.lock` must commit that pin; v2 needs it at runtime.
- Optional: a `make test-review-v2` target.

## Adversarial review follow-up

| # | Issue | Outcome |
| --- | --- | --- |
| 1 | High: review origin not tied to case origin | **Fixed.** `validate_submission_for_packet` requires equality; `assess_case` flags a stored mismatch as `inconsistent`; `list_cases(origin=...)` and `v2 list --origin` added. |
| 2 | Medium: identity hashed `page_max`, `max_cases` and origin order | **Fixed.** Both moved to `operations`; `allowed_origins` is stored in canonical order. ADR 0007 updated. |
| 3 | Medium: provider callback deadlock and leaked error codes | **Fixed.** All provider exceptions are wrapped with `details.cause`. Same-thread re-entry raises `reentrant_call` at once, so the error is not retryable. The protocol docstring forbids callbacks. |
| 4 | Medium-low: `start_case` retries duplicate cases | **Fixed.** Optional `request_id` (unique column; replay recovers the same case; conflicts are `request_conflict`). Without it, errors after case creation are `retryable: false`, `next_action: recover`. |
| 5 | Low-medium: deletes not blocked | **Fixed.** Delete triggers added on all ledger tables, plus the missing `cancellations_no_update`. A catalogue orphan/sequence check runs on every operation. |
| 6 | Low: default `list_cases()` vs small `page_max` | **Fixed.** `limit=None` means `min(50, page_max)`. |
| 7 | Low: `..` workspace paths rejected | **Fixed.** Paths are normalized with `os.path.abspath`. |
| 8 | Low: CLI default config depends on the current directory | **Fixed.** Resolved from the source tree; an installed copy without it gets a clear error. |
| 9a | Low: `AuditAnchor` not validated on construction | **Fixed.** `__post_init__`. |
| 9b | Low: invisible/control-only names and actors | **Fixed.** `visible_text`. |
| 9c | Low: non-contention lock errors reported as busy | **Fixed.** Only `AlreadyLocked` is retried. |
| 9d | Low: SQLite `database is locked` reported as integrity | **Fixed.** `sqlite_failure` maps it to `workspace_busy`. |
| Doc | RapidFuzz default vs plan item 4 (E5) | **Documented** in ADR 0007 and Limitations; not changed, because the assignment specified RapidFuzz. |
| Doc | `portalocker` pin only in the uncommitted `pyproject.toml` | **Confirmed.** It is a shared-file request (above). |

## Next action

1. Run `tests/test_review_v2_*.py` on Linux (the CI runner) to exercise the `flock` backend.
2. Have the Step 24 engineer build `watchlist.api` strictly on `ReviewService`, using `CaseView.to_dict()` and `ReviewError.to_dict()` with the code→HTTP table in Decision 0007, and deriving `actor` from authentication.
3. Have the Step 20 engineer implement reviewer memory as a `ContextProvider`.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](review-p1-final.md) and [its validation record](review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
