# Completion Steps 20–21 — fictional exact-ID reviewer memory, replay-safe publication, lifecycle, backup/restore and anchoring

Date: 2026-10-26, Asia/Kolkata. **Status: mechanics implemented and tested on fictional data only. Steps 20 and 21 are not fully closed**: revalidation of memory freshness *under the v2 acceptance lock*, source refresh/continuity tooling, ledger-side retention, encrypted storage and independent (off-machine) anchors remain BLOCKED with named owners below. All numbers come from commands actually run in this checkout on the Windows development machine. They are software-behaviour evidence on authored fictional episodes, not matching-quality, identity or reviewer-benefit results.

## Goal (5W1H)

- **What:** a reviewer-memory layer that shows *dated prior annotations* about the **exact** reference-record IDs in a new review packet, plus the lifecycle around it: corrections, retention deletion drills, backup/restore drills and audit-head anchors.
- **Why:** a reviewer looking at `syn-007` today should be able to see that `syn-007` was selected for follow-up in an earlier case — without that becoming an identity fact, a wrongdoing finding, a sanctions status or a clearance, and without similar names ever linking cases.
- **Who:** only a human records `candidate_for_follow_up`, `none_selected` or `unresolved`. Memory displays earlier opinions with their origin (`human`, `simulated_demo`, `test`; `unknown` is never displayed automatically). Maintainers run corrections, retention and backups explicitly.
- **When:** history is looked up while the v2 service prepares a packet (before the human-review interrupt); completed cases are published afterwards by an outbox publisher.
- **Where:** `src/watchlist/case_memory/`, `src/watchlist/review/{backup,retention}.py`, `config/memory.json`, `eval/configs/memory.json`, `eval/fixtures/memory-episodes.json`.
- **How:** the one v2 `ReviewService` is the only review backend. Memory plugs in through its `ContextProvider` hook (display-only, frozen into the packet digest) and reads completed cases through its read-only API. No review logic is copied.

## Design decisions (reusing [Phase 8 plan](../phase-08-plan.md))

| Phase 8 design element | Implemented as |
| --- | --- |
| Exact scoped key, never names | `record_key = SHA-256(["case-memory-record", namespace, entity_id])` + exact snapshot fingerprint. `validate_identifier` rejects whitespace/name-like text. No name, alias, query or name-hash lookup exists anywhere. |
| Registered namespaces/snapshots | `config/memory.json`: one fictional namespace, one snapshot (`c1804f29…fb004`, the current packaged-entities digest as computed by `watchlist.review.nodes.reference_digest`). Registration rejects a snapshot in two namespaces, a source label in two namespaces and (since the adversarial review) a namespace with more or fewer than **one** source label, because `record_key` does not include the source. |
| Annotation ≠ identity | Each card carries a fixed `interpretation` sentence per relation (`selected_for_follow_up`, `considered_without_selection`, `unresolved_context`) and the `MEMORY_NOTICE` ("not an identity fact … not a clearance"). |
| 3 cards per candidate, deterministic order, omission counts, 90-day window | `policy.cards_per_candidate = 3`, `reviewed_at` desc then annotation ID; omission reasons `withdrawn`, `superseded`, `expired`, `origin_not_displayable`, `after_lookup_time`, `current_case`, `over_cap`; `now >= reviewed_at + 90 days` is expired (exclusive boundary tested to the microsecond). |
| Differing annotations, no vote | All eligible outcomes shown; `differing_outcomes` flag; no majority/newest-wins field. |
| Bind displayed history into the packet | The provider's JSON is frozen into `packet.context['case_memory']` and the packet SHA-256; it records lookup time, memory head, policy digest and **per-key revisions**, including keys with empty history. |
| Outbox + atomic publication + same receipt on retry | `publication_intents` (one row per case) → single-transaction publish of annotation + records + chained event + key revisions; deterministic annotation ID; `Publisher.receipt(case_id)` is stable across retries. A submission ID already used by another case is quarantined at `enqueue` (`duplicate_submission_id`); an intent that still fails to publish is quarantined as `publication_blocked` and `drain` continues with the next one. |
| Lifecycle events, append-only corrections | `corrections` table (one per original) with chained `annotation_withdrawn` / `annotation_superseded` events; originals never rewritten. |
| Retention ≠ expiry; tombstones | `plan_retention` (dry run, may forecast a future `now`) / `apply_retention` (stale plans and plans dated after the store clock + 300 s refused) delete content rows and leave a tombstone without notes; publisher never republishes a tombstoned case. |
| Quiesced complete-set backup, restore to new directory, anchors | `review/backup.py` (see below). |

Deviation from the Phase 8 file plan: the outbox lives in the **memory store**, not in the v2 case ledger, because `review/{storage,schema,service}.py` are owned by the review-service owner and the v2 ledger is append-only. Consequence: "commit final audit and publication intent together" is approximated by *the ledger's final receipt is the source of truth and the outbox is idempotently derived from it*; nothing is lost because `enqueue` rescans completed cases on every run. `legacy.py` (v1 import) was not part of this assignment's file list and is not built.

## Files

New (all owned by this step):

| File | Responsibility |
| --- | --- |
| `src/watchlist/case_memory/__init__.py` | Package docstring; imports nothing heavy. |
| `src/watchlist/case_memory/state.py` | Version constants (`case-memory-config-v1`, `case-memory-store-v1`, `case-memory-annotation-v1`, provider `case_memory` / `case-memory-context-v1`), notices, structured errors (`CaseMemoryError` → `memory_invalid_input`, `memory_unavailable` (retryable), `memory_inconsistent`, `memory_incompatible_version`, `memory_unregistered_identifier`, `publication_conflict`, `correction_invalid`, `retention_invalid`, `memory_not_found`; all subclass `ReviewError`), time helpers (aware UTC only), config dataclasses (`MemoryPolicy`, `RetentionPolicy`, `NamespaceConfig`, `SnapshotConfig`, `MemoryConfig.load`), stored contracts `AnnotationRecord`, `Annotation` (strict `from_dict`), `annotation_id_for`. |
| `src/watchlist/case_memory/identifiers.py` | `validate_identifier`, `validate_snapshot`, `record_key`, `RecordRef`, `Registry` (ambiguity checks, `require_namespace`, `refs`, `check_source`). |
| `src/watchlist/case_memory/schema.sql` | Store tables (`memory_meta`, `source_workspaces`, `publication_intents`, `annotations`, `annotation_records`, `corrections`, `tombstones`, `quarantine`, `key_versions`, `memory_events`), lookup index, append-only triggers (content deletable only after a tombstone exists; key revisions only +1), `application_id` "PWM1", `user_version 1`. No query/name column. |
| `src/watchlist/case_memory/store.py` | `MemoryStore.initialize/open` (explicit create vs open; never migrates), `session(write)` (portable lock, fresh connection, identity + exact schema signature check, hot/stale journal handling), `MemoryDb.append_event` (hash chain + key revisions), `register_source_workspace`, `head`, `verify` → `MemoryVerification` (integrity_check, FK check, chain, payload digests, record rows vs payload, publication events, tombstones, corrections, recomputed key revisions). |
| `src/watchlist/case_memory/lookup.py` | `lookup_keys`, `build_context` (default panel), `inspect_history` (explicit, paginated, other snapshots labelled "continuity not established"), `get_annotation`, `check_freshness` → `FreshnessReport`. |
| `src/watchlist/case_memory/publisher.py` | `annotation_from_case` (completed + verified case only; exact IDs from frozen evidence; evidence source must belong to the snapshot's namespace), `Publisher.enqueue/drain/publish/receipt`, `PublicationReport`, quarantine for conflicts/unregistered data. |
| `src/watchlist/case_memory/provider.py` | `MemoryContextProvider` (implements `ContextProvider`), `memory_context(packet)`, `render_context_text` (JSON-escaped free text). |
| `src/watchlist/case_memory/lifecycle.py` | `withdraw`, `supersede` (replacement must share an exact key, same origin, later review; no self/chained-corrected replacement), `RetentionPlan`, `plan_retention`, `apply_retention` → `DeletionManifest` (lists `copies_not_deleted`). |
| `src/watchlist/case_memory/evaluation.py` | `ScriptedRetriever` (authored candidates per snapshot), `EpisodeClock`, `TimedProvider`, `EpisodeRunner`, `score_episode`, `run_evaluation`, CLI. |
| `src/watchlist/review/backup.py` | `create_backup`, `verify_backup`, `restore_backup` (new destination or `dry_run`; optional deep audit check via read-only `ReviewService`), `export_anchor`, `verify_anchor`, `logical_digest`, CLI. Error `BackupError` (`backup_failed`). |
| `src/watchlist/review/retention.py` | `plan` (inventory + memory/backup/ledger due items), `apply` (re-plans, deletes planned memory rows and expired backup sets, exclusive manifest), CLI with `--confirm-plan-id`. |
| `config/memory.json` | Policy (3 cards, 90 days, display-origin partition), retention (365 days annotations, 30 days backups), fictional namespace + packaged snapshot, lock timeout. |
| `eval/configs/memory.json`, `eval/fixtures/memory-episodes.json` | Evaluation config and 24 authored episodes / 12 groups (8 development, 4 held-out; same-name pair `syn-001`/`syn-012` in one split) with hand-written expected cards. |
| `tests/test_case_memory_{support,identifiers,store,publisher,provider,lifecycle,evaluation}.py`, `tests/test_review_{backup,retention}.py` | Tests; `test_case_memory_support.py` holds shared helpers and the subprocess crash worker (it collects no tests). |
| `docs/data-lifecycle.md`, `docs/threat-model.md`, `docs/maintenance/restore.md`, this report | Documentation. |

No existing file was modified. `pyproject.toml`, `README.md`, `review/{service,storage,contracts,validation,...}.py` and v1 files are untouched.

## Public API (summary)

```python
from watchlist.case_memory.state import MemoryConfig
from watchlist.case_memory.store import MemoryStore
from watchlist.case_memory.provider import MemoryContextProvider, memory_context, render_context_text
from watchlist.case_memory.publisher import Publisher, annotation_from_case
from watchlist.case_memory.lifecycle import withdraw, supersede, plan_retention, apply_retention
from watchlist.case_memory.lookup import inspect_history, get_annotation, check_freshness
from watchlist.review.backup import create_backup, verify_backup, restore_backup, export_anchor, verify_anchor
from watchlist.review import retention   # retention.plan(...), retention.apply(...)

store = MemoryStore.initialize(memory_dir, MemoryConfig.load("config/memory.json"))   # or .open(...)
service = ReviewService.open(workspace, review_config, context_providers=[MemoryContextProvider(store)])
Publisher(store, ReviewService.open(workspace, review_config, read_only=True), "my-workspace").publish()
```

Context shape (`packet.context["case_memory"]["data"]`): `status` (`ok` | `unregistered_snapshot` | `source_mismatch`), `notice`, `lookup_at`, `policy_sha256`, `memory_head`, `namespace_id`, `snapshot_sha256`, `case_origin`, `displayable_origins`, `visibility_days`, `pending_publications`, `blocked_publications`, `warnings`, and per candidate (in ranking order) `keys[]` with `record_key`, `revision`, `eligible_count`, `shown_count`, `omitted{…}`, `eligible_outcomes`, `differing_outcomes`, `cards[]` (case ID, dates, origin, outcome, relation, interpretation, reviewer opinion, actor label, workflow version, retriever, earlier rank/score, packet/final-event/annotation hashes, `supersedes_annotation_id`).

Commands:

```sh
python -m watchlist.case_memory.evaluation --config eval/configs/memory.json [--episodes e01 ...] [--output NEW.json]
python -m watchlist.review.backup {create,verify,restore,anchor,verify-anchor} ...
python -m watchlist.review.retention {plan,apply} --memory-config config/memory.json ...
```

API changes from the adversarial-review fixes (all additive except the namespace rule):

- `lifecycle.CLOCK_SKEW_SECONDS = 300`, `lifecycle.refuse_future_plan(planned_at, now)`; `apply_retention` refuses future plans (`retention_invalid`).
- `retention.apply(..., clock=None)`: refuses future plans; creates the manifest exclusively before deleting (`backup_failed`, nothing deleted, on an unusable path); writes a `failed` record if a deletion step raises. CLI `apply --now` must repeat the plan's `planned_at`.
- `Publisher.drain()` now returns `{"published", "pending", "blocked", "newly_blocked"}`; `PublicationReport.blocked`; `publisher.quarantine(...)` helper; `store.BLOCKED_REASON`, `store.PENDING_INTENTS_WHERE`, `store.blocked_intent_count(mdb)`; `MemoryVerification.blocked_intents`.
- Provider: `source_mismatch` status when a request exposes `candidate_sources` that the namespace does not own (forward compatible; today's `ContextRequest` has none).
- `NamespaceConfig`: exactly one `sources` entry (configs with several now fail with `memory_invalid_input`).
- `verify_anchor`: exact structure/type check, malformed anchors raise `backup_failed`; `case_count` mismatch is reported as an issue.
- `inspect_history`: bad `limit`/`offset` raise `MemoryInputError` (`memory_invalid_input`) instead of `ValueError`.

## Commands run and actual results

Tests (exact command):

```sh
cd D:\Projects\raiha\Polyglot_watchlist
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider \
  tests/test_case_memory_identifiers.py tests/test_case_memory_store.py tests/test_case_memory_publisher.py \
  tests/test_case_memory_provider.py tests/test_case_memory_lifecycle.py tests/test_case_memory_evaluation.py \
  tests/test_review_backup.py tests/test_review_retention.py tests/test_case_memory_support.py
```

Result after the adversarial-review fixes: **83 passed in 309.88 s** (exit 0; 63 before the fixes plus 20 new regression tests, of which 9 are parametrized malformed-anchor cases). The slowest tests are the real-subprocess crash tests, because each worker process imports the review stack. Earlier run before the fixes: 63 passed in 275.46 s.

Not verified here: `tests/test_packaging.py` and `tests/test_project_checks.py` could not run in this environment (installed `uv` 0.8.16 versus the pinned 0.13.5, no QA interpreter for the detector, and Windows symlink privilege errors). These failures are environmental and occur before any of this step's code runs. As a result, inclusion of `case_memory/schema.sql` in the wheel was not checked by a build.

Ruff (after the fixes): `ruff format` then `.tools/qa/Scripts/python.exe -m ruff check --no-force-exclude --config pyproject.toml src/watchlist/case_memory src/watchlist/review/retention.py src/watchlist/review/backup.py <9 owned test files>` → `All checks passed!`; `ruff format --check` on the same paths → already formatted.

Evaluation (full fixture): `python -m watchlist.case_memory.evaluation --config eval/configs/memory.json` → exit 0:

| Measure | All | Development (8 groups) | Held-out (4 groups) |
| --- | --- | --- | --- |
| Episodes passed | 24/24 | 16/16 | 8/8 |
| Query keys (candidate lookups) | 32 | 23 | 9 |
| Displayed links (cards) | 27 | 19 | 8 |
| Wrong links | **0** | 0 | 0 |
| Link precision | 27/27 = 1.0 | 19/19 | 8/8 |
| Expected cards shown (recall at cap 3) | 27/27 = 1.0 | 19/19 | 8/8 |
| Keys with exactly the oracle order | 32/32 | 23/23 | 9/9 |
| Stale exposures (withdrawn/superseded/expired shown) | 0 | 0 | 0 |
| Ranking parity, memory on vs off | 24/24 | 16/16 | 8/8 |

Re-run after the fixes (`run_at 2026-10-26T22:51:22+05:30`): identical totals (24/24 episodes, 32 keys, 27 cards, 0 wrong links, 27/27 expected cards, 32/32 exact order, 0 stale exposures, parity 24/24; development 16/16, held-out 8/8). Provider lookup wall time: n = 24, median 18.512 ms, p95 49.346 ms (first run: 31.241 / 64.268 ms; includes lock, SQLite open, identity and schema checks; machine-local, not a performance claim). Run time 69.162 s (first run 101.374 s, which overlapped other work). Interpretation: on these 24 authored episodes the software linked only exact keys and applied every display rule as the hand-written oracle expected. **Ranking parity 24/24 is guaranteed by construction, not measured**: every episode uses the `ScriptedRetriever`, whose candidates are fixed by script, and the v2 service never lets a provider touch retrieval. Measured parity with the real packaged RapidFuzz retriever is a separate test (`test_real_rapidfuzz_ranking_is_identical_with_memory_on_and_off`). **0 wrong links covers only the episodes' cases**; it does not cover a retriever whose snapshot fingerprint fails to cover its evidence source labels (see Limitations). The episodes are correlated variants of 12 fictional groups and were authored by the implementer; zero wrong links here is not an operational guarantee, and nothing here measures whether memory helps a reviewer.

CLI drill (scratch directory under the session scratchpad, not the repo): a 3-case workspace (2 completed, 1 awaiting review) + memory (2 annotations) → `backup create` exit 0; `backup verify` `ok: true`; `restore --dry-run --review-config` `ok: true`, `case_count 3`, `elapsed_seconds 15.672946`; `restore --destination` `ok: true`, `elapsed_seconds 14.56818`; `anchor` exit 0; `verify-anchor` `ok: true, cases_checked 3, memory_checked true`; `retention plan --now 2027-12-01…` → 2 memory candidates, 1 expired backup set, ledger `blocked_append_only` (3 cases); `retention apply --confirm-plan-id …` → `verified: true`, rows deleted `annotations 2, annotation_records 3, publication_intents 2, corrections 0`, 1 backup set removed. **That apply step only worked because of the bug fixed below** (a future `--now` was accepted); with the fixed code it is refused with `retention_invalid` and deletes nothing. The CLI drill was not re-run by hand after the fix; the CLI behaviour is covered by `test_cli_plan_and_confirmed_apply` (past plan time, real clock) and `test_cli_apply_refuses_a_future_plan_and_deletes_nothing`. The ~15 s restore times were measured in fresh processes and include importing the LangGraph/review stack for the deep audit check while a pytest run was executing concurrently; they are an upper-bound observation for a tiny fictional workspace, not a recovery-time objective.

## What the tests prove (by requirement)

| Requirement | Evidence |
| --- | --- |
| Memory keyed only by exact IDs; similar names never link | `test_shared_name_and_look_alike_records_never_link` (`syn-001`/`syn-012` "Luma Vesper", look-alike `syn-002`), `test_names_and_malformed_values_are_never_identifiers`, evaluation e01, e02, e23, e24. |
| Reused IDs in different sources/snapshots never link | `test_reused_id_in_another_namespace_or_snapshot_never_links`, `test_unregistered_snapshot_and_foreign_source_are_quarantined`, `test_alt_namespace_projection_uses_its_own_namespace`, `test_namespace_must_have_exactly_one_source_label`, `test_packaged_snapshot_fingerprint_covers_evidence_source_labels`, `test_provider_refuses_cross_source_evidence_when_sources_are_supplied`, evaluation e11, e12, e18. Lookup-time residual gap: see Limitations. |
| Dated annotations as context, not identity | card `interpretation` + notice; `test_differing_outcomes_are_all_shown_without_a_verdict`; projection contains no query/display name (`test_projection_keeps_exact_ids_relations_and_no_query_text`). |
| Ranking invariance | By construction (provider sees only `ContextRequest`); `test_ranking_and_evidence_are_identical_with_memory_on_and_off` (scripted); `test_real_rapidfuzz_ranking_is_identical_with_memory_on_and_off` (real packaged RapidFuzz retriever, query "Luma Vesper", memory card attached, retrieval identical). Evaluation parity 24/24 is scripted and only re-confirms the construction. |
| Replay-safe outbox, no duplicates/no loss | `test_publish_is_idempotent_and_returns_the_same_receipt`; `test_real_crash_windows_lose_nothing_and_duplicate_nothing` with real `os._exit` at `after_enqueue`, inside the publish transaction (`before_publish_commit`, hot journal present then removed by a writable open) and `after_publish_commit`; copied-workspace republish quarantined; `test_submission_id_reused_across_workspaces_is_quarantined_not_blocking`, `test_unpublishable_intent_is_quarantined_and_drain_continues` (a poisoned intent is quarantined once, later cases still publish, no retry events, `verify()` reports `blocked_intents 1, pending 0`, the panel warns). |
| Valid empty vs unregistered vs unavailable/corrupt | `test_valid_empty_history_differs_from_unregistered_and_unavailable` (missing file → `context_provider_failed` cause `memory_unavailable`, case recovered without re-retrieval), `test_schema_change_is_rejected_as_inconsistent_not_empty`. |
| Visibility window, cap, origins | `test_expiry_boundary_is_exclusive_at_exactly_ninety_days`, `test_cards_are_capped_ordered_and_count_omissions`, `test_display_origin_partition_and_simulated_origin_is_visible`, evaluation e04–e06, e09, e10, e19, e21. |
| Bound history revisions / staleness | `test_frozen_context_detects_changed_and_formerly_empty_keys`; evaluation e14. |
| Corrections append-only, linked | `test_withdrawal_hides_future_display_but_keeps_history`, `test_supersession_links_original_to_replacement_and_rejects_merges` (cross-record, cross-namespace, cross-origin, backwards and self corrections refused). |
| Retention drill with tombstones and audit | `test_retention_drill_deletes_with_tombstones_and_blocks_republication`, `test_stale_retention_plan_is_refused`, `test_crash_during_retention_deletes_nothing` (real `os._exit`), `test_apply_drill_deletes_planned_items_and_writes_a_verified_manifest`, `test_cli_plan_and_confirmed_apply`, `test_future_retention_plan_is_a_forecast_and_deletes_nothing`, `test_cli_apply_refuses_a_future_plan_and_deletes_nothing`, `test_unusable_manifest_path_is_refused_before_any_deletion`, `test_failure_after_manifest_creation_is_recorded_in_the_manifest`. |
| Backup/restore into new destinations with verification | `test_backup_and_restore_drill_into_new_directories` (restored cases equal the originals; pending case reviewable in the restored copy; live copy unchanged), `test_restore_dry_run_leaves_nothing_behind`, `test_destinations_must_be_new_and_outside_sources`, `test_corrupt_or_incomplete_backups_are_detected_before_restore`, `test_backup_waits_for_the_writer_lock`. |
| Audit-head anchoring (limited tamper evidence) | `test_anchor_detects_truncation_removed_cases_and_memory_rewind` (restored older generation → memory chain shorter than anchor and anchored case `case_not_found`; edited anchor → self-digest mismatch); `test_malformed_anchor_files_raise_backup_failed` (9 malformed shapes → `backup_failed`, CLI exit 2, no traceback). |
| Integrity checks | `test_append_only_triggers_block_edits_and_unauthorized_deletes`, `test_verify_detects_payload_tamper_and_rehash_attempts`, `test_event_chain_break_is_reported`, `test_busy_store_is_unavailable_and_retryable`, `test_read_only_open_and_lookups_change_no_files`. |

## Bugs found during this step

- A process killed inside a *small* SQLite transaction can leave a rollback journal that SQLite does not treat as hot (header not yet synced). SQLite then neither rolls it back nor deletes it, so read-only sessions — which refuse to run while a journal exists — would have failed forever. Fixed in `store._settle_journal`: a writable session performs a value-preserving header write in its own transaction, which makes SQLite reuse and delete the file. **The v2 review service has the same read-only-refusal pattern** (`ReviewService._locked`); its existing hot-journal test uses a large transaction that does spill to disk. Reported to the review-service owner below; not changed here.
- The scripted evaluation clock originally reset its millisecond counter per day, which let a lookup on the same day appear earlier than a just-written review. Fixed (counter never resets).

### Adversarial review findings (2026-10-26) and outcomes

| # | Finding | Verified | Outcome |
| --- | --- | --- | --- |
| 1 | HIGH: retention with a future `now` deleted unexpired annotations (API and CLI `apply --now FUTURE`) | Reproduced (`apply_retention` deleted a same-day annotation with `verified=True`) | **Fixed.** `apply_retention` and `retention.apply` refuse plans dated after the applying clock + 300 s with `retention_invalid`. Forecasting with `plan --now FUTURE` still works. Existing drills now advance the fictional store clock to the plan time; the crash worker runs with an `EpisodeClock` at the plan time. |
| 2 | HIGH: one unpublishable outbox entry (cross-workspace `submission_id` reuse) blocked every later publication; `verify()` said ok with pending 2 | Reproduced | **Fixed.** Enqueue-time `duplicate_submission_id` quarantine (no intent written); `drain` quarantines a failing intent as `publication_blocked` in its own transaction and continues; blocked intents are excluded from pending and reported (`blocked`, `blocked_intents`, `blocked_publications`, panel warning). They make no integrity issue (`ok` stays true) because the store is consistent; they are an operator task. |
| 3 | MEDIUM-HIGH: missing manifest directory → memory deleted, then raw `FileNotFoundError`, no manifest | Reproduced | **Fixed.** Manifest opened with `"x"` before any deletion; `OSError` → `backup_failed`, nothing deleted. A failure after creation writes a `failed` record and re-raises. `rmtree` errors become manifest issues. |
| 4 | MEDIUM: cross-source wrong link possible at lookup; several sources per namespace could collide | Reproduced with a mis-labelled `ScriptedRetriever` | **Partly fixed.** Namespaces now need exactly one source label (second route closed). Provider returns `source_mismatch` when a request exposes `candidate_sources` (forward compatible). Test shows the real RapidFuzz fingerprint covers `source`, so relabelled data is unregistered. **Remaining (BLOCKED):** `ContextRequest` carries no per-candidate source; review-service owner must add it. |
| 5 | MEDIUM: malformed anchors raised `KeyError`/`AttributeError`, CLI traceback | Reproduced | **Fixed.** Exact structure/type validation → `backup_failed`. |
| 6 | LOW-MEDIUM: "ranking parity 24/24" circular | Confirmed (scripted retriever) | **Fixed in evidence and wording.** Added a measured parity test with the real packaged RapidFuzz retriever; this report now states the 24/24 is by construction. |
| minor | `inspect_history` raised `ValueError` | Confirmed | **Fixed** → `memory_invalid_input`. |
| minor | Restored memory store keeps the same `store_uuid` | Confirmed (by design: a restore is the same store) | **Documented** in `docs/maintenance/restore.md`; no code change. |

## Limitations (honest)

- Fictional data only: one packaged fictional namespace and authored episodes. No public or real-person data, no reviewer study, no E5 run.
- The context provider cannot see per-candidate evidence `source`, only entity IDs and the retriever descriptor; the namespace is therefore taken from the **registered snapshot fingerprint**. A retriever whose fingerprint does not cover its evidence source labels (reproduced with a mis-labelled scripted retriever) can show history from the snapshot's namespace for another source's reused ID; publication refuses such evidence, but lookup cannot until `ContextRequest` carries `candidate_sources` (BLOCKED). The packaged RapidFuzz retriever is not affected: its fingerprint digests each entity's `source`.
- Blocked (quarantined) outbox intents are never retried automatically; there is no operator command yet to resolve them (for example, re-review with a fresh submission ID).
- Applying retention trusts the store/system clock; a wrong system clock still moves the cutoff.
- `check_freshness` is advisory: an interface must call it just before `submit_review`. Between that call and acceptance the memory can change.
- The outbox derives from the ledger rather than being committed in the same ledger transaction as the final audit; idempotent rescans cover loss, but publication is not triggered automatically — someone must run `Publisher.publish()` (or a scheduler/interface must).
- `Publisher.enqueue` reads every page of `list_cases`; fine for fictional sizes, linear in workspace size.
- Owner-only permissions are applied on POSIX only; on Windows nothing changes ACLs. No encryption at rest.
- Retention cannot delete v2 ledger content (append-only, no payload/receipt split). Deletion is not secure erasure.
- Anchors are limited tamper evidence; stored on the same disk they are only local receipts.

## BLOCKED items (owner → next action)

| Item | Owner | Next action |
| --- | --- | --- |
| Revalidate memory key revisions/expiry **under the acceptance lock** and reject stale unaccepted forms with a linked fresh case (Step 20 item 5) | Review-service owner (`review/service.py`, `validation.py`) | Add a `submission_validators` (or provider `validate_fresh(packet)`) hook called inside `submit_review` before `record_submission`; this step supplies `check_freshness`-equivalent logic to plug in. |
| Commit final-audit and publication intent in one ledger transaction (Step 20 item 6) | Review-service owner | Optional outbox table in the v2 schema (`review-workspace-v2` → new version) or accept the current derived-outbox design in an ADR. |
| Source registry / refresh classification (unchanged/revised/removed/merged/split IDs), reviewed continuity mappings, migrations (`case_memory/{source_registry,continuity,migrations}.py`, Step 21 items 1–2, 5) | Data owner + workflow | Not in this assignment's file list; requires approved public-data refresh permissions first. |
| Ledger-side retention: separate v2 content payloads from minimal audit receipts (Step 21 item 4) | Review-service owner | Design a v3 ledger with payload tables deletable under tombstones while hashes stay verifiable; until then ledger cases are reported `blocked_append_only`. |
| Encrypted storage, key recovery, Windows ACLs (Step 21 item 6) | Reliability/deployment owner | Choose a deployment target and approved encryption mechanism before any real data. |
| Independent anchor custody (off-machine, signed/timestamped) | Reliability owner | Decide custodian/medium; current anchors are local receipts. |
| Same "stale non-hot journal" issue in v2 read-only opens | Review-service owner | Mirror `case_memory.store._settle_journal` in writable `ReviewService` sessions and add a small-transaction crash test. |
| Lookup-time source check (adversarial finding 4) | Review-service owner (`review/contracts.py`, `service.py`) | Add `candidate_sources: tuple[str, ...]` (evidence `source` per candidate, ranking order) to `ContextRequest`; the provider already returns `source_mismatch` when it is present. Then add an evaluation episode with a mis-labelled retriever. |
| Operator resolution of blocked publications | Workflow owner | Decide the procedure (e.g. re-review with a fresh submission ID, then publish); add a command that lists `publication_blocked` quarantine rows. |
| Reviewer usefulness/anchoring measurement | Step 22 owner | Counterbalanced study; this step's evaluation is software-only. |
| `legacy.py` (v1 → memory import) | Workflow owner | Not assigned here; needs an explicit import manifest and origin classification. |

## Shared-file requests

- `pyproject.toml` (INTEGRATE engineer): consider adding the new owned files to `[tool.ruff] include` and confirm `schema.sql` under `case_memory/` enters the wheel (hatch includes package data under `src/watchlist`; not verified with a wheel build in this step). No new dependency is needed (standard library + existing `portalocker`, `langgraph` via the v2 service).
- `README.md` / `Makefile` / CI (INTEGRATE engineer): link `docs/data-lifecycle.md`, `docs/threat-model.md`, `docs/maintenance/restore.md`; add the evaluation command. The full memory evaluation takes ~100 s; CI may prefer the 5-episode adversarial subset that `tests/test_case_memory_evaluation.py` runs.
- `review/service.py` (review-service owner): see BLOCKED rows 1, 2 and 7.
- `review/contracts.py` (review-service owner): add each candidate's evidence `source` to `ContextRequest` as `candidate_sources` (adversarial finding 4).

## Next step

Step 22 (memory usefulness study and feedback labels) can consume `Annotation` records, but must keep display-only memory effects separate from model effects and must extend the copy inventory in `docs/data-lifecycle.md` before any label leaves the review stores.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](review-p1-final.md) and [its validation record](review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
