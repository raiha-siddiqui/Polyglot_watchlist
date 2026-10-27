# Runbook — recovery required (`recovery_required`, `preparing`, `finalizing`)

Research prototype over fictional data. Recovery finishes interrupted **software** work from durable records. It never chooses, infers or changes a review outcome; only a human reviewer records `candidate_for_follow_up`, `none_selected` or `unresolved`.

## Symptoms

- A case shows `"recovery_required": true`, `"next_action": "recover"`, status `preparing` or `finalizing`.
- API `409 {"code": "recovery_required"}` (for example "operation interrupted (…); inspect the case and run recover").
- `GET /ready` → `503 not_ready`, `details.cause = "recovery_required"` (a hot SQLite journal: see case B).

## Case A — one interrupted case

Cause: a process stopped between durable steps (crash windows are listed in [Decision 0007](../adr/0007-review-service-v2.md)): after the case row, after evidence, after the packet, after an accepted review, or before the final checkpoint.

1. Inspect it (read-only):
   `python -B -m watchlist.review v2 status --workspace <ws> --case-id <id>`
   or `GET /v2/cases/{id}` with a viewer key.
2. Run the single legitimate continuation (reviewer key / operator):
   `python -B -m watchlist.review v2 recover --workspace <ws> --case-id <id>`
   or `POST /v2/cases/{id}/recover`.
   - `preparing` → finishes retrieval/packet; evidence already saved is reused (no second retrieval). The case ends in `awaiting_review` and still needs a human review.
   - `finalizing` → completes explanation and final audit with the **already accepted** review.
3. Verify the audit chain: `python -B -m watchlist.review v2 verify --workspace <ws> --case-id <id>` and check `"ok": true` in the JSON (see the exit-code note in the [audit runbook](audit-verification-failure.md)).
4. If recovery returns `incompatible_version` / `retriever_mismatch`, the case was saved under another workflow or retriever identity. Do not edit the config to force it; cancel the unfinished case (`v2 cancel --actor <label> --reason "<text>"`) and start a new one, or run the matching version.
5. If the status is `inconsistent`, stop: that is not an interruption. Go to the [audit verification failure runbook](audit-verification-failure.md).

## Case B — hot SQLite journal (workspace-wide)

Cause: a process died **inside** a SQLite transaction and left a `review.sqlite-journal`/`-wal` sidecar. Read-only opens (including `/ready` and read-only CLI/MCP servers) refuse with `recovery_required` because read-only SQLite cannot roll it back.

1. Stop all processes on the workspace.
2. Take a file-level copy of the whole workspace directory **including the sidecar** to a safe place (evidence; the backup command cannot run on a workspace it cannot open).
3. Open it **writable** once so SQLite rolls the journal back: start the API (`create_app` opens the workspace read-write) or run a writable CLI command such as `v2 recover` for an affected case. Read-only commands (`v2 status`, `v2 list`, `v2 verify`, `/ready`) cannot do it.
4. Confirm the sidecar is gone and `GET /ready` → `200`; then check affected cases as in case A.

This path follows Decision 0007 and the service code; it was not drilled separately in Step 27 (the v2 crash-window tests cover the service behaviour).

## Never

- Never delete `-journal`/`-wal` files by hand; they may contain the only copy of committed changes (WAL) or the rollback data (journal).
- Never "fix" a case by editing rows. The audit chain will (correctly) report it as tampered.
- Never treat a failed or interrupted case as `none_selected`.
