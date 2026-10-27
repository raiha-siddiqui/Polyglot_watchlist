# Decision 0007 — one versioned v2 review service for every interface

Date: 2026-10-26. Status: **implemented for Completion Step 19 on fictional data; API/UI/MCP/memory layers are not built yet.** See the [completion plan](../project-completion-plan.md#step-19--build-one-versioned-review-service-for-all-interfaces), [Decision 0005](0005-review-workflow.md), [Decision 0006](0006-review-versioning.md) and the [v1 recovery matrix](../maintenance/recovery-state-matrix.md). Evidence: [completion-19](../progress/completion-19.md).

## Context

The v1 workflow (`src/watchlist/agent/`) works, but its pending cases are bound to a SHA-256 of Python/SQL **source bytes**, so a comment edit locks them out. It also uses `fcntl` (POSIX-only), two SQLite files that can drift apart, and `getpass` as the reviewer label. Steps 24–26 need one Python API that a FastAPI service, a Streamlit UI and an MCP server can share without copying orchestration. Step 20 needs a place to attach reviewer-memory context.

## Decision

Add a **new** v2 implementation in `src/watchlist/review/` and leave v1 untouched (v1 keeps its strict guard for its archived cases; the archive reader of Decision 0006 is unchanged). Interfaces call only `watchlist.review.service.ReviewService`.

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Version constants, frozen dataclass contracts with `to_dict()`/`from_dict()`, structured errors, retriever and context-provider protocols. Standard library only. |
| `validation.py` | Policy and saved-state checks: names, origins, bounds, retrieval linkage, submission-to-packet binding. |
| `locking.py` | `portalocker` exclusive lock on `writer.lock` with a monotonic bounded wait. |
| `storage.py` + `schema.sql` | Explicit workspace creation, owned-schema checks, append-only ledger, per-case audit hash chain. |
| `recovery.py` | Pure status assessment and the single legitimate recovery action. |
| `workflow.py` + `nodes.py` | LangGraph `StateGraph` with `SqliteSaver`, interrupt-based human review, retriever adapters, deterministic explanation. |
| `service.py` | `ReviewService` operations. |
| `cli.py` | Existing `archive` commands unchanged; new `v2` command group. |

### Explicit contracts

| Contract | Version | Bound how |
| --- | --- | --- |
| Workspace format | `review-workspace-v2` | `workspace_meta` row + SQLite `application_id` `PWR2` + `user_version 2` + exact schema comparison at every operation |
| Case schema | `review-case-v2` | stored in each case header |
| Packet schema | `review-packet-v2` | stored in each packet; packet SHA-256 binds submissions |
| Submission schema | `review-submission-v2` | stored in each accepted review |
| Audit schema | `review-audit-v2` | part of every hashed event envelope |
| Workflow identity | `review-workflow-v2.0` + SHA-256 of the `workflow` policy section of `config/review-v2.json` | stored in each case header and packet |
| Checkpoint format | `langgraph-checkpoint-sqlite-3` | meta row + installed major version check |

Workflow identity is a **declared version plus a policy digest**, never a source hash. Editing comments, reformatting the JSON, reordering `allowed_origins` (stored in canonical order) or changing anything in `operations` (`lock_timeout_seconds`, `max_cases`, `page_max`) does not change it. Changing review semantics (top K, text bounds, the set of allowed origins, context size budget, explanation template) does, and unfinished cases from the old identity become `incompatible`: readable, cancellable, but not resumable until the original version/config is used. Changing semantics in code requires bumping `WORKFLOW_VERSION`; that is a review-process rule, not something the service can detect.

The retriever is **not** part of the workflow identity. `config/review-v2.json` selects RapidFuzz over the packaged fictional entities as the default adapter, as Step 19 was assigned, so the service runs and is tested without model weights. This deliberately differs from item 4 of the Step 19 plan ("production/default selection stays E5"): switching to E5 is a config change (`retriever.kind = e5` plus an approved local embedding config), and choosing the production default remains open until the E5 parity run with approved weights. Each case freezes a `RetrieverDescriptor` (kind, name, version, reference-data digest, settings). Once evidence is saved it is never recomputed, so a model change cannot alter pending evidence. Recovery that still needs retrieval refuses a different descriptor (`retriever_mismatch`).

Every case has an explicit `origin`: `human`, `simulated_demo`, `test` or `unknown`; there is no default. Each submission carries an origin that **must equal the case origin** (`invalid_input` otherwise; assessment also flags a stored mismatch as `inconsistent`), so demo or test cases can never be counted as human-reviewed evidence by Step 08 or Step 20. `list_cases(origin=...)` reads one partition. Each submission also carries an `actor` label supplied by the interface (authenticated identity in Step 24; a typed label in the CLI). The label is attribution, not authentication. Names, actors and reasons must contain a visible character and no control or bidi-override characters; names and reasons keep ZWJ/ZWNJ, which some scripts need.

### Workspace and storage

`ReviewService.initialize(workspace, config)` creates `writer.lock` (exclusively) and `review.sqlite` in a missing or empty directory. `ReviewService.open(workspace, config, read_only=False)` requires both files and never creates, migrates or repairs anything. Ledger tables and the pinned LangGraph checkpoint tables live in **one** SQLite file, so a backup/rollback always captures a coherent pair. The file stays in rollback-journal mode (the saver's `setup()`, which enables WAL, is never called), so a read-only `mode=ro` connection creates no `-wal`/`-shm` files. Append-only triggers block `UPDATE` and `DELETE` on every ledger table; they are not tamper-proof. Dropping one changes the schema signature (rejected at open), and every operation also runs a catalogue check: rows without a case, checkpoints without a case thread, or a gap in the case sequence raise `storage_inconsistent`, so a removed case cannot silently disappear from listings.

### Workflow

```text
normalize -> retrieve -> format_packet -> prepare_review -> human_review (interrupt)
          -> explain (template evidence-v2) -> audit
```

`retrieve` commits evidence + `evidence_recorded` and reuses saved evidence on any replay. `format_packet` calls context providers and builds the immutable packet. `prepare_review` commits packet + `review_ready`. `human_review` interrupts unconditionally and only accepts a resume value equal to the durably accepted review. `explain` renders saved facts and the attributed reviewer opinion only. `audit` commits one `case_finalized` event (idempotent replay). No LLM, reranker, threshold or automatic outcome exists.

### Statuses, operations and errors

`CaseView.status` is one of `preparing`, `awaiting_review`, `finalizing` (accepted but not finalized), `completed`, `cancelled`, `incompatible`, `inconsistent`; `next_action` is `submit_review`, `recover` or `none`.

| Operation | Writes | Notes |
| --- | --- | --- |
| `start_case(name, *, origin, actor, request_id=None)` | yes | input validated before any effect; `request_id` UUID makes retries idempotent |
| `get_case(case_id)`, `list_cases(limit=None, offset=0, *, origin=None)` | no | read-only SQLite; listing omits names and notes; default page `min(50, page_max)` |
| `submit_review(case_id, submission)` | yes | `submission_id` UUID gives idempotent retries |
| `recover(case_id)` | yes | the only way to finish interrupted work |
| `cancel_case(case_id, *, actor, reason)` | yes | only before a review is accepted; allowed for incompatible cases |
| `audit(case_id)`, `verify_audit(case_id, anchor=None)` | no | `verify_audit` reports issues instead of raising |

Suggested transport mapping for Step 24 (proposal, not implemented here):

| `code` | HTTP | `code` | HTTP |
| --- | --- | --- | --- |
| `invalid_input` | 422 | `stale_submission`, `submission_conflict` | 409 |
| `case_not_found` | 404 | `invalid_state`, `recovery_required` | 409 |
| `incompatible_version` | 409 | `workspace_busy` | 503 (retryable) |
| `read_only` | 405/403 | `retrieval_failed`, `context_provider_failed` | 503 (retryable) |
| `storage_inconsistent`, `workspace_invalid` | 500 | `retriever_mismatch` | 409 |
| `request_conflict` | 409 | `reentrant_call` | 500 (programming error) |

Submission semantics: identical replay of the accepted form returns the current view; same ID with different content or an ID used by another case is `submission_conflict`; any other review after acceptance, or a packet digest different from the saved packet, is `stale_submission`. Exactly one review per case is guaranteed by the workspace lock plus `UNIQUE` constraints.

Start semantics: the case row commits before retrieval, so a failure after that point leaves a `preparing` case with `details.case_id`. With a `request_id`, a retry of the same request (same name, origin and actor) returns that case and recovers it if needed, and a different request with that ID is `request_conflict`; the error may stay `retryable`. Without a `request_id`, such an error is returned with `retryable: false` and `details.next_action = "recover"`, because a blind retry would create a duplicate case. API clients should always send a request UUID (Step 24 item 4).

### Locking

Every operation takes the exclusive `writer.lock` with a bounded wait (`operations.lock_timeout_seconds`, default 5 s; zero tries once), opens fresh connections, and releases both. The service object keeps no handle between calls, so threads may share it and processes may share a workspace. Readers also lock exclusively because Windows `msvcrt` has no shared mode without pywin32; reads are short. Only contention (`portalocker.AlreadyLocked`) is retried; any other lock failure (for example `ENOLCK`) is `workspace_invalid` at once. A second acquisition by the thread that already holds the lock raises `reentrant_call` immediately instead of waiting out the timeout. A SQLite `database is locked`/`busy` error (a process outside the service holding the file) is `workspace_busy`, not `storage_inconsistent`. Retrieval runs inside the lock but outside SQLite write transactions; a slow model lengthens lock hold time, so Step 24 should keep one inference worker and a bounded queue rather than a lock held for the app lifetime.

### Crash windows (tested with real `os._exit` in a child process)

| Failpoint | Saved before crash | Status after crash | Recovery |
| --- | --- | --- | --- |
| `after_case` | case + `case_created`; no checkpoint | preparing | start from the saved header; retrieval runs once (descriptor must match) |
| `after_evidence` | + evidence | preparing | reuse evidence; zero retrievals |
| `after_packet` | + packet | preparing | idempotent prepare; zero retrievals |
| `after_submission` | + accepted review; interrupt pending | finalizing | resume with the saved review |
| `during_explanation` | review transition checkpoint | finalizing | explain from saved state |
| `after_final_audit` | + `case_finalized`; final checkpoint missing | finalizing | replay finalize, same receipt |

A process dying **inside** a SQLite transaction leaves a hot journal; read-only operations report `recovery_required` and a writable open lets SQLite roll it back. Durable progress without checkpoint history, checkpoint values that contradict the ledger, edited audit rows and rehashed record forgeries are `inconsistent`; mutations are refused and nothing is repaired.

### Context-provider hook (for Step 20)

`ReviewService(..., context_providers=[provider])`, where a provider has `name`, `version` and `provide(ContextRequest) -> JSON object`. Output is size-bounded, frozen into `packet.context[name]` and into the packet digest, and is display-only: it cannot change candidates, scores or outcomes. Provider names are recorded per case; recovery with a different provider set is refused. Every provider exception, including a `ReviewError` the provider raises, becomes `context_provider_failed` with `details.cause` (the original code or exception type), so a provider cannot leak codes such as `stale_submission` to callers; the case stays recoverable without new retrieval. Providers run while the service holds the workspace lock and must not call back into the service; a same-thread callback fails at once with cause `reentrant_call` and `retryable: false`.

## Alternatives considered

- **Patch v1 in place.** Rejected: v1 must stay byte-stable for its archived, fingerprinted cases.
- **Keep two SQLite files.** Rejected: a single file removes the mixed-generation pair failure class.
- **Hash source files for identity.** Rejected: comment edits would strand cases. A declared version requires discipline instead.
- **Shared reader locks.** Not portable without pywin32; exclusive short reads are simpler.
- **Hold one lock for the service lifetime.** Rejected: blocks other processes; conflicts with the Step 24 requirement.

## Consequences and limits

- One semantics implementation for future interfaces; v1 CLI and archive reader remain separate, documented version boundaries. No v1→v2 migration exists; a future migration must create linked new v2 cases.
- Local hash chains are tamper evidence, not authentication; an owner can rewrite the whole file. An `AuditAnchor` kept elsewhere detects tail truncation.
- `verify_audit`/`get_case` check one case in depth; the catalogue check run by every operation detects removed cases and orphan rows/checkpoints, but it is a count check, not a full per-case re-verification of the workspace.
- The E5 adapter is implemented lazily but only tested for laziness and its missing-model error; real E5 inference stays an opt-in integration check.
- All evidence so far is fictional; nothing here measures matching quality or reviewer benefit.
