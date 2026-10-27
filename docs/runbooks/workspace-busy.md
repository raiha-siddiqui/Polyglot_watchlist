# Runbook — workspace busy (`503 workspace_busy`)

Research prototype over fictional data. Nothing in this procedure establishes identity, wrongdoing, sanctions status or clearance; it restores a software service.

## Symptoms

- API responses `503` with `{"code": "workspace_busy", "retryable": true}` and `Retry-After: 1`.
- Authenticated `GET /ready` → `200` with `checks.workspace_read_only_open = "busy"` (a held lock is reported as ready, not as a failure); `GET /health` still `200`. Use the `workspace_busy` counters in authenticated `GET /metrics` to see whether it persists. Readiness and metrics require at least the viewer role.
- `/metrics`: `watchlist_http_requests_total{…,code="workspace_busy"}` or `watchlist_spans_total{…,outcome="workspace_busy"}` increasing.
- CLI: `{"error": "workspace_busy", ...}`.

## What it means

Every review operation holds one exclusive, cross-process lock on `<workspace>/writer.lock` (portalocker: `msvcrt` on Windows, `flock` on POSIX). A request waits up to `operations.lock_timeout_seconds` (5 s in `config/review-v2.json`; `/ready` tries once without waiting and reports `busy`) and then gives up with this error. **Nothing was changed** by the refused request. The operating system releases the lock when the holder exits or crashes; the file itself stays and must stay.

Common holders: a backup (`python -m watchlist.review.backup create` holds the lock for the whole copy), a long CLI command, a second API/MCP/UI process on the same workspace, or many concurrent reads in one process. The cooperative lock has no fairness guarantee; no reader-count or latency benchmark is asserted here. See [access and signals](../operations.md#access-and-signals).

## Steps

1. **Retry first.** Clients should retry after `Retry-After` with the *same* `request_id` / `submission_id`, which makes retries idempotent. Do not invent a result: a busy workspace is not "no match".
2. **Is it load?** Check `watchlist_http_requests_in_flight` and the `code="workspace_busy"` rate. If it rises with traffic, lower `WATCHLIST_API_MAX_IN_FLIGHT` (for example to 8) so excess requests get an immediate `429 too_many_requests` instead of waiting on the lock, and restart the API.
3. **Is another process holding it?** List processes that use the workspace path:
   - Windows (PowerShell): `Get-CimInstance Win32_Process | Where-Object CommandLine -Match 'watchlist' | Select-Object ProcessId, CommandLine`
   - Linux: `ps -eo pid,args | grep -i watchlist` (and `lsof <workspace>/writer.lock` if available).
   A running backup is expected to block writers; wait for it to finish (its JSON report prints when done).
4. **Stuck holder.** If a process is hung (no progress, no CPU), stop it normally first (Ctrl+C / `kill -TERM`), then forcibly only if needed. The lock is released when the process exits. Afterwards check affected cases: `python -B -m watchlist.review v2 list --workspace <ws>` and follow the [recovery-required runbook](recovery-required.md) for any case not `awaiting_review`/`completed`/`cancelled`.
5. **Confirm:** `GET /ready` → `200` with `"workspace_read_only_open": "ok"` (not `busy`), and new requests no longer return `workspace_busy`.

## Never

- Never delete or replace `writer.lock` to "unlock" a workspace; it would let two writers interleave.
- Never copy the database while it is in use; use the backup command, which takes the lock.
- Never raise `lock_timeout_seconds` to hide sustained contention; it only makes clients wait longer.
