# Runbook — restore from backup

Research prototype over fictional data. The detailed procedure, principles and anchor handling are in [docs/maintenance/restore.md](../maintenance/restore.md); this page is the short operator checklist for the local research stack.

## When

- Audit verification failed and the cause is not an I/O problem ([audit runbook](audit-verification-failure.md)).
- The workspace volume was lost or corrupted.
- A rollback to a known-good generation is required.

## Checklist

1. **Stop writers** to the live workspace (API, MCP server, UI, CLI scripts). Readiness of the old workspace no longer matters.
2. **Choose a set** and verify it:
   `python -B -m watchlist.review.backup verify --backup <backup dir>` → `"ok": true`. If not, take the next older set.
3. **Dry run** (restores to a temporary directory, verifies, removes it):
   `python -B -m watchlist.review.backup restore --backup <backup dir> --dry-run --review-config config/review-v2.json`
   Record `elapsed_seconds` and `case_count`.
4. **Restore into a NEW directory** (never in place):
   `python -B -m watchlist.review.backup restore --backup <backup dir> --destination <new dir> --review-config config/review-v2.json`
   `"ok": true` and `"issues": []` are required. With `--review-config` every case's audit chain is verified through the read-only service.
5. **Measure the loss window**: run `verify-anchor` against the newest anchor kept elsewhere using the restored directory; cases or events it reports as missing are what was lost since the backup. Record them. Do not claim zero loss without this check.
6. **Activate**: start the API on `<new dir>/review` (`python -B -m watchlist.api serve --workspace <new dir>/review --config config/review-v2.json …`) and check authenticated `GET /ready` → `200`, then spot-check `GET /v2/cases` and one `…/audit/verify`.
7. Keep the old workspace untouched until the restored one has been used successfully; retire it later with the retention procedure.

## Validation boundary

The [review receipt](../progress/review-p1-final.md) records the executed backup/restore tests. Historical scratch-drill timings were removed because this review did not reproduce them. No cross-machine, native-Windows or container-volume restore was performed. Measure the recovery and loss windows for the actual generation; do not infer them from fixture tests.
