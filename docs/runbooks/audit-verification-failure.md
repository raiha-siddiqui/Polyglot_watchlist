# Runbook — audit verification failure (`"ok": false`, `inconsistent`, `storage_inconsistent`)

Research prototype over fictional data. The audit chain is **limited tamper evidence** kept by the same owner on the same disk; it detects accidental damage and naive edits, not a careful owner who rewrites both records and hashes. An independent anchor (see [backup and anchors](../maintenance/restore.md)) kept on other media extends this.

## Symptoms

- `GET /v2/cases/{id}/audit/verify` or `python -B -m watchlist.review v2 verify` returns `"ok": false` with `issues`, for example:
  - `audit event 1: event hash mismatch`
  - `audit: case_created payload differs from saved record`
- A case status `inconsistent`; writes to it are refused.
- API `500 {"code": "storage_inconsistent"}` ("Saved review records are inconsistent; nothing was repaired.").
- `python -m watchlist.review.backup verify-anchor` reports issues (missing cases, shorter chain, different head).

**Exit codes:** `v2 verify` prints the result and exits `1` when `"ok": false`. `backup verify-anchor` also exits `1` on issues. Preserve the JSON issues as well as checking the exit status.

## Verification evidence

The [review receipt](../progress/review-p1-final.md) records executed CLI, service and backup tests, including a failing audit/anchor exit regression. Historical manual drill counts are not reasserted here. A schema change can be refused before chain verification; keep that distinct from a valid schema containing inconsistent records.

## Steps

1. **Stop writes** to the workspace (stop API/MCP/UI writers). Reads are safe.
2. **Preserve evidence**: take a backup set now (`python -m watchlist.review.backup create --workspace <ws> --destination <new dir>`); if the backup refuses, copy the stopped workspace directory as files. Record time, operator and the `issues` text.
3. **Check the storage layer.** Read-only, full-disk and I/O failures map to workspace availability errors; inconsistent saved records use `storage_inconsistent` (see [failure handling](../operations.md#limits-and-failure-handling)). Check free space and permissions, then re-run verification after resolving availability failures. Do not assume that resolving an I/O problem also repairs a chain problem.
4. **Scope it**: verify every case (`v2 list`, then `v2 verify` per case) and, if you keep anchors, `backup verify-anchor` against the newest anchor stored elsewhere.
5. **Decide** (project owner): restore the newest backup whose verification passes, using the [restore runbook](restore-from-backup.md). A restored older generation will show cases missing relative to a newer anchor; that list is the loss window and must be recorded. Cases whose reviews were lost are re-reviewed by a human; nothing is re-created automatically.
6. Never repair rows or re-hash the chain to make verification pass. That converts tamper evidence into a forgery.

## Owner / escalation

Project owner (user) decides between restore and continued investigation. The review-service owner is informed of any `inconsistent` case that is not explained by an edit or an I/O failure.
