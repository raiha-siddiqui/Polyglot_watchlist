# Review API (v2) — research only

> **Limitation.** This API returns *possible record matches* from a **fictional** reference dataset for **human review**. Similarity scores are not identity probabilities. Nothing it returns establishes identity, wrongdoing, sanctions status or clearance; `none_selected` is not a clearance. Review outcomes are only `candidate_for_follow_up`, `none_selected` and `unresolved`, recorded by an authenticated human reviewer. The API never selects an outcome and never turns an error or timeout into a result. Every case, list, audit and reference-record response carries this notice in a `limitation` field, and every HTTP response carries an `X-Research-Notice` header.

Completion Step 24. Package: `src/watchlist/api/`. Backend: the shared v2 `ReviewService` ([Decision 0007](adr/0007-review-service-v2.md)); the API adds transport concerns only (authentication, bounds, a write queue, error mapping and headers) and does not reimplement review logic. Evidence: [completion-24](progress/completion-24.md). Machine-readable contract: [`openapi.json`](openapi.json).

## Run locally

Requires the `api` and `agent` extras (FastAPI, uvicorn, httpx, LangGraph, portalocker).

```sh
# 1. Create a v2 workspace once (the API never creates or migrates one).
python -B -m watchlist.review v2 init --workspace ../data/review/v2-local

# 2. Issue keys. The token is printed once; configure only the entry (a hash).
python -B -m watchlist.api new-key --actor analyst-1 --role reviewer
#   {"entry": "analyst-1:reviewer:<sha256>", "token": "<keep this secret>"}

# 3. Serve on loopback only.
export WATCHLIST_API_KEYS=analyst-1:reviewer:SHA256_HEX_FROM_STEP_2   # or WATCHLIST_API_KEYS_FILE=path
python -B -m watchlist.api serve --workspace ../data/review/v2-local \
    --config config/review-v2.json --host 127.0.0.1 --port 8000
```

`--workspace` and `--config` are both required (no default config path is valid in both a source checkout and an installed package). `serve` refuses any non-loopback `--host`: external hosting needs TLS, a deployment review and Step 28 approval first. Interactive `/docs`, `/redoc` and `/openapi.json` are disabled (the Swagger page would load scripts from a CDN). Regenerate the committed schema with:

```sh
python -B -m watchlist.api openapi > docs/openapi.json
```

`tests/test_api.py` fails if the committed file differs from the generated one.

`hash-key` reads a key from stdin and prints its SHA-256, for keys made elsewhere.

### Settings (environment)

| Variable | Default | Meaning |
| --- | --- | --- |
| `WATCHLIST_API_KEYS` | — | `actor:role:sha256hex` entries separated by `,`, `;` or newlines |
| `WATCHLIST_API_KEYS_FILE` | — | UTF-8 file (a leading byte-order mark, as written by Windows PowerShell 5, is accepted) with one entry per line, `#` comments allowed; set only one of the two |
| `WATCHLIST_API_WRITE_QUEUE_MAX` | 8 | writes allowed to wait behind the running one; the next gets `429` |
| `WATCHLIST_API_WRITE_WAIT_SECONDS` | 30 | how long one HTTP request waits for its write (HTTP wait, not an inference deadline) |

Fixed limits: request body 64 KiB (`413`), name 200 code points, reason 1,000 characters, list pages 1–50 items (default 50). The review config may be stricter (`config/review-v2.json`: `actor_max_chars` 120). In Python, `create_app(settings, *, service=None, context_providers=())` builds the app; tests and other layers inject a `ReviewService` (and, for Step 20, context providers belong to that service).

## Authentication and roles

`Authorization: Bearer <key>`. The server stores only SHA-256 hashes and compares the presented key's hash with **every** configured hash using `hmac.compare_digest`. Keys shorter than 16 or longer than 512 characters, or non-ASCII, are refused without hashing.

| Role | Allowed |
| --- | --- |
| `viewer` | `GET` cases, list, audit, audit verification, reference records |
| `reviewer` | everything a viewer can do, plus start, review, recover and cancel |

The **actor label comes from the key**: it is recorded as the case creator, reviewer or canceller. No request body accepts an `actor` (unknown fields are `422`). The label is attribution for whoever holds the key; it is not proof of who is typing. Any reviewer may act on any case (shared queue); per-case ownership is not enforced (see completion-24). `GET /health` needs no key.

## Endpoints

| Method and path | Role | Body / query | Service call |
| --- | --- | --- | --- |
| `GET /ready` | viewer | — | coalesced read-only readiness check |
| `GET /metrics` | viewer | — | redacted process metrics |
| `GET /health` | none | — | none |
| `POST /v2/cases` | reviewer | `{name, origin, request_id?}` | `start_case(name, origin=, actor=key, request_id=)` |
| `GET /v2/cases` | viewer | `?limit=1..50&offset=&origin=` | `list_cases` |
| `GET /v2/cases/{case_id}` | viewer | — | `get_case` |
| `POST /v2/cases/{case_id}/review` | reviewer | `{submission_id, revision, packet_sha256, outcome, candidate_entity_id, reason}` | `get_case` + `submit_review` |
| `POST /v2/cases/{case_id}/recover` | reviewer | — | `recover` |
| `POST /v2/cases/{case_id}/cancel` | reviewer | `{reason}` | `cancel_case(actor=key)` |
| `GET /v2/cases/{case_id}/audit` | viewer | — | `audit` |
| `GET /v2/cases/{case_id}/audit/verify` | viewer | `?anchor_count=&anchor_head_sha256=` (both or neither) | `verify_audit` |
| `GET /v2/entities/{entity_id}` | viewer | — | packaged fictional record (no review state) |

Case responses are `{case, revision, limitation}`, where `case` is exactly `CaseView.to_dict()` and `revision` is the SHA-256 of that view. Lists are `{cases, limit, offset, next_offset, limitation}` with `CaseSummary` rows (no names, evidence or notes). Successful calls return `200`, including an idempotent replay.

### Starting a case

Always send a fresh `request_id` (lowercase UUID). Retrying the same request (same name, origin and key actor) returns the same case, recovering it if needed; another request with that ID is `409 request_conflict`. Without `request_id`, a failure after the case exists says `retryable: false` and `details.next_action: "recover"` with `details.case_id`, because a blind retry would create a second case. The same applies when the HTTP wait expires on a start without `request_id`: the answer is `503 operation_pending` with `retryable: false`, `details.next_action: "list_cases"` and no `Retry-After` (the case may still be created; list cases to find it and recover it if it stays incomplete). With a `request_id`, that `503` is retryable.

### Reviewing

Send the `revision` and `case.packet_sha256` from the response the reviewer looked at, a fresh `submission_id`, an outcome and a reason. `candidate_entity_id` must be one of the shortlisted candidates for `candidate_for_follow_up` and `null` otherwise. The review's origin is the case's own origin. The API checks `revision` against the current case before calling the service (a changed case is `409 stale_submission`); the service then binds the form to the saved packet and accepts **exactly one** review per case. Retrying the identical form with the same `submission_id` returns the current case (`200`), even after completion; the same ID with different content is `409 submission_conflict`. If a response is lost, read `GET /v2/cases/{id}` before doing anything else; never send a second form with a new ID to "retry".

### Example (fictional)

```sh
curl -s -X POST http://127.0.0.1:8000/v2/cases \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"name": "Luma Vesper", "origin": "test", "request_id": "6f1c1c0e-5d0a-4a52-9a3e-3b1f6f0e2a11"}'
```

## Errors

Every error body is exactly `{code, message, retryable, details}`; branch on `code`. `5xx` messages are fixed texts (no exception strings, paths or tracebacks), and `details` keeps only an allow-list of keys (`case_id`, `status`, `next_action`, `expected_packet_sha256`, `allowed`, `missing`, `unexpected`, `request_id`, `provider`, `cause`, `case_origin`, `submission_origin`, `errors`, `limit`, `retry_after_seconds`). Validation errors list `{loc, type, msg}` without echoing the input. Retryable `429`/`503` responses carry `Retry-After: 1`; non-retryable ones do not.

| HTTP | `code` |
| --- | --- |
| 400 | `bad_request` (invalid `Content-Length` header, refused before the body is read) |
| 401 | `unauthenticated` (with `WWW-Authenticate: Bearer`) |
| 403 | `forbidden` (role), `read_only` (service opened read-only) |
| 404 | `case_not_found`, `entity_not_found`, `not_found` (route) |
| 405 | `method_not_allowed` |
| 409 | `stale_submission`, `submission_conflict`, `request_conflict`, `invalid_state`, `recovery_required`, `incompatible_version`, `retriever_mismatch` |
| 413 | `body_too_large` (> 64 KiB, by header or by counting a chunked body) |
| 422 | `invalid_input` (malformed JSON including invalid UTF-8 and nesting too deep to parse, schema, unknown fields, duplicate JSON keys, NaN, bounds, non-canonical UUIDs, service validation) |
| 429 | `queue_full` |
| 500 | `storage_inconsistent`, `reentrant_call`, `internal_error` |
| 503 | `workspace_busy`, `workspace_invalid`, `retrieval_failed`, `context_provider_failed`, `operation_pending`, `service_unavailable` |

Order of checks: body size (`413`) → malformed JSON (`422`) → authentication (`401`) → role (`403`) → schema (`422`) → queue (`429`) → service.

## Concurrency model

- One worker thread per app runs all writes of that process in order (retrieval included). At most `WATCHLIST_API_WRITE_QUEUE_MAX` more wait; the next is refused at once with `429` and has no effect.
- A request waits `WATCHLIST_API_WRITE_WAIT_SECONDS` for its write. If that expires the client gets `503 operation_pending`, but the operation is never cancelled half-way; it finishes and is visible through `GET`. The error is retryable when the request carries a `request_id` or `submission_id` (or is a recover/cancel), and retrying with the same ID cannot duplicate it. A case start **without** `request_id` gets `retryable: false` and `details.next_action: "list_cases"`, because a retry would create a second case.
- Reads run in the server thread pool. Every service call takes the workspace file lock with the configured bounded wait (`lock_timeout_seconds`, 5 s), so a slow retriever can make reads return `503 workspace_busy`.
- No lock is held between requests. Several API processes (or the CLI) may share a workspace; the file lock and the service's `UNIQUE` constraints still allow exactly one accepted review per case.

## Security headers

`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`, `Cross-Origin-Resource-Policy: same-origin`, `X-Research-Notice`. No CORS middleware is installed, so browsers on other origins cannot read responses. `debug=False`; FastAPI's native telemetry is disabled; uvicorn is started without a `Server` header and without proxy-header trust.

## Not implemented (see completion-24)

The asynchronous `202` + operation-polling design of plan item 4, the reference's unversioned `/screen`, `/review/{id}`, `/audit/{id}` aliases, per-case ownership, TLS/external auth, and request rate limits other than the write queue.

Operational endpoints `/ready` and `/metrics` require a viewer or reviewer key.
`/health` is the public liveness endpoint. HTTP request IDs are generated by the
server regardless of any caller-supplied `X-Request-ID`; the JSON `request_id`
field still controls case-start idempotency. The bundled launcher disables
uvicorn access logs so URL paths and query strings do not become log records.

For cases with reviewer memory, submit with the same provider configured.
Changed, expired or corrected memory returns `409 stale_submission` with
`next_action: start_case`; the old packet remains frozen. Replaying an already
accepted form remains idempotent even after memory changes. Missing providers
return `503 context_provider_failed` without recording a review.
