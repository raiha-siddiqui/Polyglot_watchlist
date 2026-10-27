# Operations guide — review API observability, limits and failure handling

Date: 2026-10-26 (Completion Step 27). Applies to `src/watchlist/observability.py` and the hooks it adds to `src/watchlist/api/app.py` / `settings.py`.

> **Research-only notice.** This prototype suggests *possible* reference-record matches over **fictional** data for human review. Nothing here (a metric, a span, a log line or an error) is an identity, wrongdoing, sanctions-status or clearance finding. Review outcomes are only `candidate_for_follow_up`, `none_selected` or `unresolved`, recorded by a person.

Everything below is local: no collector, no exporter, no network call and no third-party observability package. All numbers quoted here were measured on one Windows 11 development machine (Python 3.14.9, 22 logical CPUs) on fictional data; see [completion-27](progress/completion-27.md). They are not capacity or production claims.

## 1. What the API now emits

| Signal | Where | How to read it |
| --- | --- | --- |
| `X-Request-ID` response header | every HTTP response, including `401`, `404`, `413`, `422`, `429`, `500`, `503` | A correlation label. A client may send its own (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`); anything else — CR/LF, spaces, non-ASCII, bidi characters, over 64 characters, a value contained in an `Authorization`, `Proxy-Authorization`, `Cookie` or `X-Api-Key` header, or a value that is itself a valid API key (checked against the configured key hashes, so a key sent by mistake as the request ID is never echoed, logged or written as `trace_id`) — is replaced by a random 32-hex ID. It is **not** the review service's idempotency `request_id` body field. |
| Span records | JSON lines in `WATCHLIST_TRACE_PATH` (optional) | One line per finished operation: `ts`, `trace_id` (= request ID), `span_id`, `parent_span_id`, `operation`, `duration_ms`, `outcome` (`ok` or a stable error code), redacted `attrs`, `pid`. |
| Metrics | `GET /metrics` (viewer key), Prometheus text format 0.0.4 | In-process counters/gauges/histograms; reset on restart. |
| Logs | Python `logging` under `watchlist.*`; JSON lines when `WATCHLIST_LOG_FORMAT=json` | Each record: `ts`, `level`, `logger`, `message`, `request_id`, optional redacted `fields`, `exc_type` (type name only, never a traceback). |
| Liveness | `GET /health` (no key) | The process answers. Never touches the workspace; exempt from load shedding. |
| Readiness | `GET /ready` (no key) | The workspace files are complete and it opens **read-only** with **one** lock attempt (no waiting). `200 {"status":"ready","checks":{"workspace_read_only_open":"ok"}}`; if the lock is held by a running operation, `200` with `"busy"` (a held lock is normal work, not unreadiness; requests that cannot get it within their own timeout still get `503 workspace_busy`); otherwise `503 not_ready` with `details.cause` = the service error code (`workspace_invalid`, `recovery_required`, `incompatible_version`, …). Concurrent probes share one check and a healthy result is reused for 1 s (`READY_CACHE_SECONDS`), so a probe flood makes about one lock attempt per second; failures are never cached. No paths are returned. |

`/ready` and `/metrics` are deliberately excluded from the published OpenAPI document (`docs/openapi.json` is owned by the API step and pinned by tests); they are documented here.

### Operation names

* `http <METHOD> <route template>` — one per request, e.g. `http POST /v2/cases/{case_id}/review`. Unmatched paths use `unrouted`, so an attack string in a URL never becomes a metric label or span name.
* `review.<operation>` — one per `ReviewService` call (`start_case`, `get_case`, `list_cases`, `submit_review`, `recover`, `cancel_case`, `audit`, `verify_audit`), recorded by a transparent proxy that forwards arguments and results unchanged. Writes run in the write-queue thread with the request's context copied, so their `parent_span_id` is the HTTP span.

### A real fictional trace (captured 2026-10-26)

`POST /v2/cases` with `X-Request-ID: fictional-demo-start` and the name "Luma Vespr" over the packaged fictional entities produced (abridged, one JSON object per line in the file):

```json
{"operation":"review.start_case","outcome":"ok","duration_ms":4513.94,"trace_id":"fictional-demo-start","parent_span_id":"3b3fb33e9bf747d7","attrs":{"actor":"fictional-analyst","case_id":"ed84f7c6-…","name":"hmac-sha256:39adc19d5cca63d3;chars=10","origin":"test","status":"awaiting_review"}}
{"operation":"http POST /v2/cases","outcome":"ok","duration_ms":4535.919,"trace_id":"fictional-demo-start","span_id":"3b3fb33e9bf747d7","parent_span_id":null,"attrs":{"method":"POST","route":"/v2/cases","status":200}}
{"operation":"review.get_case","outcome":"case_not_found","duration_ms":57.065,"trace_id":"fictional-demo-missing", …}
{"operation":"http GET /v2/cases","outcome":"unauthenticated","duration_ms":2.382, …}
```

Meaning: the first case start of a fresh process took about 4.5 s, almost all inside the service (lazy retriever and workflow-graph construction); later starts take about 0.3 s (§4). The name appears only as a keyed fingerprint and its length. A missing case and a missing key are visible as stable outcome codes without any request content.

## 2. Redaction policy

Implemented by `watchlist.observability.Redactor` and applied to every span attribute and every structured log field. Interpolated log arguments are additionally neutralized by `LogContextFilter`, which `create_app` installs on the `watchlist.api` logger whatever the log format.

| Data | Treatment |
| --- | --- |
| Secrets: field names `authorization`, `cookie`, `password`, `secret`, `token`, `bearer`, `credentials`, `api_key`/`apikey`/`x-api-key`, and any name ending in `key`, `keys`, `token(s)`, `secret`, `password` | Replaced by `[redacted]`. Never hashed (a hash of a guessable secret can be brute-forced). |
| Personal or free text: any field name with a `_`/`-`/`.`-separated part that **contains** `name`, `alias`, `reason`, `quer`, `text`, `note`, `comment` or `body` — e.g. `name`, `display_name`, `entity_name`, `normalized_name`, `matched_alias`, `original_text`, `query_text`, `cancel_reason`, `reviewer_notes` (matching is deliberately broad: an operational field such as `hostname` is fingerprinted too). A whole `CaseView.to_dict()` passed through the redactor contains no name (regression-tested). | `hmac-sha256:<first 16 hex>;chars=<length>`. The HMAC key is 32 random bytes per process (per `create_app`), so equal text can be correlated within one run but not across restarts, and a dictionary of names cannot reverse it. |
| Case IDs in spans | Kept if they are UUIDs; anything else becomes `invalid:hmac-sha256:…` (SQL-like or traversal strings are never written). |
| Credentials inside free text (query strings, messages) | The value after a `=` or `:` that follows `api_key`, `x-api-key`, `apikey`, `token`, `access_token`, `refresh_token`, `secret` or `password` is replaced by `[redacted]` (a value starting with `%` or `{` is a format placeholder and kept). |
| Any other string | `Bearer …`/`Basic …` sequences replaced; control, format (`Cf`, incl. zero-width and bidi), line/paragraph-separator characters escaped as `\uXXXX`; truncated to 256 code points with `...[+N chars]`. JSON output is ASCII-only. |
| Paths | Only the final component (local user names are not written). |
| Exceptions | Exception **type** only in `watchlist.*` records. An unexpected exception in a request is answered with the generic `500 internal_error` **inside the app** (`UnexpectedErrorMiddleware`) and not re-raised, so uvicorn does not print `Exception in ASGI application`, the traceback or the message (verified with a real uvicorn server on 127.0.0.1 in `test_real_uvicorn_logs_no_exception_text_for_an_unexpected_500`). Before this fix Starlette re-raised after sending the 500 and uvicorn logged the full message and traceback as plain text. |

What is **not** covered: logs of other libraries, in particular uvicorn's own `uvicorn.error` startup/shutdown lines and the uvicorn **access log**, which are plain text, not JSON, and print client addresses, request paths and query strings (never put secrets or names in URLs; `serve` does not route them through `JsonFormatter` — a request for the API owner, see completion-27); an exception raised *outside* the request (lifespan, server internals) still reaches uvicorn's logger; the review database itself (which by design stores names and reasons); and anything an operator copies out of the system.

## 3. Configuration

| Setting / environment variable | Default | Notes |
| --- | --- | --- |
| `max_in_flight` / `WATCHLIST_API_MAX_IN_FLIGHT` | 64 | Requests handled at once (1–10000). More get `429 too_many_requests` with `Retry-After: 1` and do no work. `/health` is exempt. A request is counted only **after its body has fully arrived**, so clients that open uploads and stall cannot fill the slots. |
| `body_timeout_seconds` / `WATCHLIST_API_BODY_TIMEOUT_SECONDS` | 10 | One deadline (0 < t ≤ 300 s) for receiving the whole request body; a stalled or dripping body gets `408 request_timeout` (retryable). Connections that never send headers are uvicorn's concern (its `timeout_keep_alive`/`limit_concurrency`), not this setting. |
| `write_queue_max` / `WATCHLIST_API_WRITE_QUEUE_MAX` | 8 | Unchanged from Step 24: writes waiting behind the running one; more get `429 queue_full`. |
| `trace_path` / `WATCHLIST_TRACE_PATH` | unset | Span file. The directory must exist (never created implicitly). Unset: spans still feed metrics, nothing is written. |
| `trace_max_bytes` | 50 MiB | At the limit the file is renamed to `<name>.1` (replacing the previous one). At most about twice this size is kept. |
| `log_format` / `WATCHLIST_LOG_FORMAT` | `text` | `json` installs the redacting JSON handler on the `watchlist` logger (stderr, level INFO). |

Use one trace file per process. Appends from several processes into one file are not coordinated.

A trace-sink failure (missing directory, read-only volume, full disk) never fails a request: the record is dropped, `watchlist_trace_records_dropped_total` increases and one warning is logged per process.

## 4. Metrics reference and what to watch

| Metric | Type | Labels |
| --- | --- | --- |
| `watchlist_http_requests_total` | counter | `method`, `route`, `status`, `code` |
| `watchlist_http_request_duration_seconds` | histogram | `method`, `route` |
| `watchlist_http_requests_in_flight` | gauge | — |
| `watchlist_spans_total` | counter | `operation`, `outcome` |
| `watchlist_span_duration_seconds` | histogram | `operation` |
| `watchlist_write_queue_outstanding`, `watchlist_write_queue_limit` | gauge | — |
| `watchlist_trace_records_written_total`, `watchlist_trace_records_dropped_total` | counter | — |

Buckets (seconds): 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, +Inf. Each metric keeps at most 500 label sets; further ones are folded into `_overflow`.

Signals worth a look (no alerting is configured; there is no monitoring system in this project):

* `code="workspace_busy"` or `outcome="workspace_busy"` growing → [workspace busy runbook](runbooks/workspace-busy.md).
* `code="recovery_required"` or cases with `recovery_required: true` → [recovery-required runbook](runbooks/recovery-required.md).
* `code="storage_inconsistent"` (500) → stop writes and follow the [audit verification failure runbook](runbooks/audit-verification-failure.md); see also the read-only-disk note in §5.
* `code="queue_full"` / `too_many_requests` → load above the single-process capacity below.
* `watchlist_trace_records_dropped_total` > 0 → the trace volume is unavailable.

### Measured single-process behaviour (local fictional measurements)

`python -B scripts/load_smoke.py` (in-process `TestClient`, RapidFuzz over packaged fictional entities, one warm-up case before measuring), 2026-10-26:

| Run | Phase | Status counts | p50 ms | p95 ms | max ms | Throughput |
| --- | --- | --- | --- | --- | --- | --- |
| 40 req, concurrency 1 | start_case | 200×40 | 324.41 | 402.01 | 490.66 | 2.96/s |
| | get_case | 200×40 | 80.15 | 120.43 | 126.47 | 12.69/s |
| 40 req, concurrency 8 | start_case | 200×40 | 2383.34 | 2593.18 | 2616.66 | 3.32/s |
| | get_case | 200×40 | 399.82 | 1779.61 | 2096.87 | 11.46/s |
| 100 req, concurrency 32 | start_case | 200×9, 429 `queue_full`×91 | 249.78 | 2261.41 | 3728.35 | — |
| | get_case | 200×84, 503 `workspace_busy`×16 | 2362.35 | 5397.96 | 5593.32 | — |
| 100 req, concurrency 32, `max_in_flight=8` | start_case | 200×8, 429 `too_many_requests`×92 | 163.71 | 1749.19 | 3237.86 | — |
| | get_case | 200×8, 429 `too_many_requests`×92 | 242.86 | 1311.27 | 1853.85 | — |
| re-run after the review fixes (body deadline, cap inside the body limit, catch-all 500), 40 req, concurrency 8 | start_case | 200×40 | 2393.89 | 2748.72 | 2759.86 | 3.24/s |
| | get_case | 200×40 | 275.12 | 1783.82 | 2930.65 | 12.12/s |
| same, 20 req, concurrency 4 | start_case | 200×20 | 1180.31 | 1287.19 | 1306.43 | 3.33/s |
| | get_case | 200×20 | 152.24 | 1066.17 | 1340.70 | 12.79/s |

No run produced a `500`. Interpretation:

* All workspace access is serialized by one exclusive lock, so concurrency adds queueing, not throughput: about 3 case starts/s and about 12 case reads/s on this machine.
* With 32 concurrent readers and the default 5 s lock timeout, some reads starve on the lock (polling, no fairness) and return retryable `503 workspace_busy`. Setting `WATCHLIST_API_MAX_IN_FLIGHT` near the real concurrency need (8 here) sheds the excess immediately as `429` instead of making clients wait up to 5 s.
* The supported deployment for this prototype is therefore **one process, a handful of concurrent users**. Higher numbers need a different storage design, not tuning.

## 5. Failure behaviour (tested in `tests/test_load_failure.py` and `tests/test_security_hardening.py`)

| Situation | What the client sees | Notes |
| --- | --- | --- |
| More than `max_in_flight` requests | `429 too_many_requests`, `Retry-After: 1` | No work done. Counted only once the body has arrived. |
| Body not received within `body_timeout_seconds` | `408 request_timeout` (retryable) | Stalled uploads never hold an in-flight slot (regression-tested with 8 stalled bodies and `max_in_flight=4`). |
| `/ready` flooded without a key | probes answer `200` from the shared/cached check | About one workspace lock attempt per second whatever the probe rate. |
| Write queue full | `429 queue_full`, `Retry-After: 1` | Step 24 behaviour. |
| Workspace lock held by another process (or a long write in this one) | requests that cannot get the lock within their timeout: `503 workspace_busy` (retryable, `Retry-After: 1`); `/ready` → `200` with `checks.workspace_read_only_open = "busy"` | Tested with a real second process holding the lock and with a write held inside retrieval. A lock that stays `busy` for minutes is diagnosed from the `workspace_busy` metrics, not from `/ready`. |
| Database file read-only (permissions, read-only volume) | writes: today `500 storage_inconsistent` (fixed message, no details); reads and `/ready` keep working; no partial case is left | The 500 is the review service's current mapping of any non-lock SQLite error. It is misleading for a read-only or full disk; reported as a finding for the service owner. The test pins only the safety properties (structured 500/503, empty details, no path, never success), so the owner's fix does not break it. |
| Database missing/renamed | `/ready` → `503 not_ready`, cause `workspace_invalid`; `/health` still `200` | |
| Trace file unwritable / disk full | requests succeed; drop counter increases | |
| Body over 64 KiB | `413 body_too_large` | By `Content-Length` and by counting chunked bodies. |

Graceful shutdown (by code reading; not separately drilled in this step): on Ctrl+C or POSIX SIGTERM uvicorn stops accepting connections, and the API lifespan then waits for running and queued writes to finish (`WriteQueue.shutdown(wait=True)`); a write is never cancelled half-way. On Windows, `Popen.terminate()`/Task Manager is a hard kill (`TerminateProcess`). A hard kill can leave a case in `preparing`/`finalizing`; see the [recovery-required runbook](runbooks/recovery-required.md). Committed records survive (the Step 24 restart test and the v2 crash-window tests cover this).

## 6. Runbooks

* [Workspace busy](runbooks/workspace-busy.md)
* [Recovery required](runbooks/recovery-required.md)
* [Audit verification failure](runbooks/audit-verification-failure.md)
* [Restore from backup](runbooks/restore-from-backup.md)

## 7. Known limits

* Metrics are per process and reset on restart; there is no scraper, dashboard or alerting in this project.
* The redaction key is per process; fingerprints cannot be compared across restarts (by design).
* Spans are local files with no retention job beyond the single rotation; delete old `.1` files with the normal log hygiene of the host.
* `/ready` opens the workspace (≈ tens of ms under the lock) at most about once per second; a healthy result can therefore be up to 1 s stale. `busy` is reported as ready, so a lock stuck for a long time is not visible on `/ready`; watch `workspace_busy` counts instead.
* Many concurrent reads still contend for the single exclusive workspace lock (20 ms polling, no fairness). Under heavy in-process CPU load (e.g. 30 client threads flooding *any* endpoint, even `/health`, in the same interpreter) some authenticated reads exceed the 5 s lock timeout and return retryable `503 workspace_busy`; measured in completion-27. This is the review service's lock design, not readiness.
* The in-flight limit is per process and counts requests, not their cost.
* Tracing system choice (plan item 1 proposes local Phoenix or LangSmith) is **not** made here; this stdlib JSONL format is the interim, dependency-free record. See completion-27 for the open decision.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](progress/review-p1-final.md) and [its validation record](progress/review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
