# Backup, restore and anchor runbook (v2 review workspace + memory store)

Date: 2026-10-26. Applies to `src/watchlist/review/backup.py` and `src/watchlist/review/retention.py`. Fictional data only so far. See [Decision 0007](../adr/0007-review-service-v2.md) for the workspace format, the [threat model](../threat-model.md) for limits and the older [baseline-and-backup notes](baseline-and-backup.md) for v1 artifacts.

## Principles

- **Never restore in place.** Backups and restores always write into a new (missing or empty) directory outside the source trees. Activation — pointing services at the restored directory — is a deliberate operator step after verification.
- **Quiesce writers.** `create_backup` holds the review workspace lock, then the memory store lock (the same order every component uses) for the whole copy, so the set is one coherent generation. Services wait (bounded) or report `workspace_busy` meanwhile.
- **Verify twice.** The backup manifest records each file's SHA-256, a logical content digest and row counts. `verify_backup` checks them plus owned-schema identity and SQLite integrity; `restore_backup` verifies the set before copying and the restored copy afterwards.
- **Anchors are limited tamper evidence**, not tamper-proofing. Keep them on separate media or with a separate custodian.

## Commands

All commands are local and offline. Paths are examples.

```sh
# 1. Backup (new directory only)
python -m watchlist.review.backup create --workspace data/review/v2-local \
    --memory data/review/v2-memory --destination data/review-archives/2026-10-26T1200

# 2. Verify a backup set
python -m watchlist.review.backup verify --backup data/review-archives/2026-10-26T1200

# 3. Restore drill: restore to a temporary directory, verify, remove it
python -m watchlist.review.backup restore --backup data/review-archives/2026-10-26T1200 \
    --dry-run --review-config config/review-v2.json

# 4. Real restore into a NEW directory, with deep audit verification
python -m watchlist.review.backup restore --backup data/review-archives/2026-10-26T1200 \
    --destination data/review-archives/restored-2026-10-26 --review-config config/review-v2.json

# 5. Export an anchor (exclusive create; copy it off this disk)
python -m watchlist.review.backup anchor --workspace data/review/v2-local \
    --review-config config/review-v2.json --memory data/review/v2-memory \
    --memory-config config/memory.json --anchor data/review-archives/anchors/2026-10-26.json

# 6. Later: verify current stores against that anchor
python -m watchlist.review.backup verify-anchor --workspace data/review/v2-local \
    --review-config config/review-v2.json --memory data/review/v2-memory \
    --memory-config config/memory.json --anchor data/review-archives/anchors/2026-10-26.json
```

Each command prints JSON and exits 0 on success, 1 when a verification reports issues, 2 on a structured error (`{"error": code, ...}`).

## Restore procedure

1. Stop or drain interfaces that write to the live workspace (they would otherwise keep writing to the old generation).
2. Run `verify` on the chosen backup set. Stop if `ok` is false; pick an older set.
3. Run `restore --dry-run` and record `elapsed_seconds` (the measured drill time).
4. Run `restore --destination NEW_DIR --review-config ...`. The report lists `case_count` and any issue; `ok` must be true.
5. Run `verify-anchor` against the newest anchor kept elsewhere, using the restored directories. A restored *older* generation **will** report a shorter chain / missing cases relative to a newer anchor; that is expected and documents exactly what was lost.
6. Repoint interfaces to `NEW_DIR/review` and `NEW_DIR/memory`. Keep the old directories untouched until the restored generation has been used successfully; then retire them with the retention procedure.
7. Run `Publisher.publish()` against the restored workspace: completed cases already in the restored memory are no-ops; cases missing from memory are published once. Cases tombstoned by retention are never republished.

## Rollback and mixed generations

- A review backup and a memory backup taken in **one** `create` call are one generation. Mixing a review copy from one set with a memory copy from another can show memory entries for cases the ledger does not contain (or vice versa). `verify_anchor` and `Publisher` make the mismatch visible (missing cases, quarantined republish attempts); nothing is repaired automatically.
- Missing or corrupt member files fail `verify_backup` before any restore work; a set without `backup-manifest.json` is incomplete by definition (the manifest is written last).
- A restored memory store keeps the **same `store_uuid`** as the store it was backed up from. The two copies therefore have the same identity, and an anchor verifies against either copy if their event chains agree up to the anchored position. Anchors show chain position, not which physical copy is live; record which directory was activated in the operator log, and retire the old copy rather than running both.
- Malformed anchor files (wrong keys or types) are rejected with `backup_failed` before any comparison.
- There are no schema migrations yet (`review-workspace-v2`, `case-memory-store-v1`). A future migration must restore into a new directory, migrate there and verify before activation; this runbook's steps 2–6 apply unchanged.

## Fixture validation

The restore drill in `tests/test_review_backup.py` restores a 3-case workspace plus a 2-annotation memory store into a new directory, verifies every audit chain through the read-only service, and confirms the restored pending case can be reviewed there while the live case is unchanged. The recovery time it measures is printed in the report as `elapsed_seconds`; current executed test results are recorded in the [review receipt](../progress/review-p1-final.md); no recovery-time guarantee is claimed.

## Retention of backups

`python -m watchlist.review.retention plan --memory-config config/memory.json --memory DIR --backups BACKUPS_DIR` lists backup sets older than `backup_retention_days` (30 by default) and prints `planned_at` and `plan_id`. `apply` needs `--now` equal to that `planned_at` and `--confirm-plan-id` equal to the plan it recomputes at that time. A plan dated in the future (a forecast made with `plan --now FUTURE`) is refused with `retention_invalid` and deletes nothing. `apply` creates the `--manifest` file exclusively **before** deleting anything (a missing directory or existing file fails with `backup_failed` and deletes nothing), deletes only manifest-bearing direct children of the backups directory and then writes the deletion manifest (or, if a deletion step fails, a failure record). Filesystem deletion is not secure erasure.
