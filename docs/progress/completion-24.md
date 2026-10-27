# Completion Step 24 — FastAPI over the shared v2 ReviewService

Date: 2026-10-26. **Status: API mechanics implemented and tested on fictional data only, loopback only.** All numbers below come from commands actually run in this checkout on the Windows development machine (`.venv` Python 3.14, FastAPI 0.162.0, Starlette 1.10.0, Pydantic 2.14.1, httpx 0.28.1, uvicorn 0.54.0). They show software behavior; they are not matching-quality, reviewer-benefit or load results. Several plan items remain open or **BLOCKED**; see the end of this report.

## Goal (5W1H)

- **What:** a typed, authenticated HTTP API (`src/watchlist/api/`) that exposes the v2 review service: start, list, read, review, recover, cancel, audit, audit verification and fictional reference records.
- **Why:** Steps 25–26 and external clients need one transport over the **same** service the CLI uses ([Decision 0007](../adr/0007-review-service-v2.md)). Review rules stay in `ReviewService`; the API adds only authentication, bounds, a write queue, error mapping and headers.
- **Who:** an operator issues API keys; a key's actor label is the attributed reviewer. Only a human reviewer records `candidate_for_follow_up`, `none_selected` or `unresolved`; the API never selects one and never turns an error or timeout into a result.
- **When:** a reviewer starts a case for a name, reads the possible record matches, then submits one review bound to the revision and packet digest they saw.
- **Where:** a local process bound to `127.0.0.1`, one v2 workspace directory, the packaged fictional entities.
- **How:** `create_app(settings, *, service=None, context_providers=())` wraps one `ReviewService`; every endpoint is a direct service call, writes go through a single bounded worker, and errors map from stable `ReviewError.code` values.

## Files

All new; no shared file was edited.

| File | Explanation |
| --- | --- |
| `src/watchlist/api/__init__.py` | Package docstring and exports: `create_app`, `ApiSettings`, `SettingsError`, `case_revision`, `openapi_document`, `API_VERSION` (`review-api-v2.0`). |
| `src/watchlist/api/auth.py` | `ApiKey(actor, role, sha256)` (hash hidden from `repr`), `Principal.can(role)`, `hash_key`, `new_key` (random `secrets.token_urlsafe(32)`), `parse_key_entry`/`parse_key_entries` (`actor:role:sha256hex`, `#` comments, unique hashes, errors never echo hashes), `KeyStore.authenticate` (hashes the token and runs `hmac.compare_digest` against **every** configured hash; tokens outside 16–512 ASCII characters are refused without hashing). Roles `viewer` and `reviewer` (reviewer includes viewer). |
| `src/watchlist/api/settings.py` | Frozen `ApiSettings(workspace, config_path, api_keys, max_body_bytes=65536, write_queue_max=8, write_wait_seconds=30.0)` with bounds checks; `from_env` reads `WATCHLIST_API_KEYS` **or** `WATCHLIST_API_KEYS_FILE` (both set is an error; the file is read as `utf-8-sig`, so a Windows byte-order mark is accepted), `WATCHLIST_API_WRITE_QUEUE_MAX`, `WATCHLIST_API_WRITE_WAIT_SECONDS`. `PAGE_MAX = 50`. |
| `src/watchlist/api/errors.py` | `REVIEW_STATUS` (every `ReviewError.code` → HTTP), `ApiError` for transport errors, `error_body` (`{code, message, retryable, details}`), `review_error_body` (fixed public messages for 5xx, detail allow-list `SAFE_DETAIL_KEYS`), `SECURITY_HEADERS`. |
| `src/watchlist/api/schemas.py` | Strict request models (`StartCaseRequest`, `ReviewRequest`, `CancelRequest`; `extra="forbid"`, `strict=True`, no `actor` field anywhere). Response models that mirror every v2 contract's `to_dict()` field-for-field (`CONTRACT_MODELS` pairs, checked by a drift test) plus envelopes `CaseResponse {case, revision, limitation}`, `CaseListResponse`, `AuditResponse`, `VerificationResponse`, `EntityResponse`, `HealthResponse`, `ErrorResponse`. Limits: name 200 code points, reason 1,000 characters. |
| `src/watchlist/api/app.py` | `create_app`, `openapi_document`, `case_revision(view)` (SHA-256 of the canonical case view), `WriteQueue` (one worker thread; refuses with `429 queue_full` when more than `write_queue_max` writes wait), `_Runtime.write` (HTTP wait via `asyncio.wait_for(shield(...))`; on expiry `503 operation_pending`, the operation still completes; retryable only when the write is retry-safe, so a start without `request_id` gets `retryable: false` + `next_action: list_cases`), pure-ASGI `BodyLimitMiddleware` (413 by `Content-Length` **and** by counting chunked bodies) and `SecurityHeadersMiddleware`, exception handlers (no input echo, no tracebacks; FastAPI's bare body-parse `400` is mapped to `422 invalid_input`), and the ten routes. FastAPI `debug=False`, `/docs`, `/redoc`, `/openapi.json` disabled, native telemetry explicitly disabled. |
| `src/watchlist/api/__main__.py` | `serve` (`--workspace` and `--config` both required; refuses non-loopback hosts; reads keys from the environment; opens, never creates, the workspace; `uvicorn.run(..., server_header=False, proxy_headers=False)`), `openapi`, `new-key`, `hash-key`. `is_loopback(host)`. |
| `tests/test_api.py` | 52 tests: health/notice/headers, no CORS, docs disabled, structured 404/405, start-case contract and key actor, 9 invalid start bodies, code-point limit, malformed/duplicate-key/NaN JSON, invalid-UTF-8 and too-deeply-nested bodies → 422 (not 400) on two routes, 413 (sized and chunked), `request_id` idempotency and conflict, get/list/pagination/origin filter and bad queries, unknown/malformed IDs on every case route, review completion/replay/conflict/stale, stale revision and packet, 10 invalid review forms, `none_selected`/`unresolved`, recover/cancel/review-after-cancel, cancel after review, audit and anchored verification, entity record and parity with review evidence (real RapidFuzz retriever), retrieval failure 503 + recovery, workspace busy 503, incompatible case 409 + cancel, generic 500, withheld 5xx text/details, code→status coverage, read-only service, model/contract drift, committed OpenAPI equality, `create_app` argument rules, no workspace creation, queue release after errors. |
| `tests/test_api_auth.py` | 65 tests: entry parsing (valid, 9 malformed, duplicates), `new_key` round trip, constant-time comparison count, roles, environment settings (inline, file, 8 invalid cases), hashes hidden in `repr`, the auth matrix (9 routes × 8 bad headers → 401 with no state change; 5 reads × 2 roles → 200; 4 writes: viewer → 403 with no state change, reviewer → 200), case-insensitive scheme, auth before body validation, actor taken from the key for start/review/cancel, `request_id` reused by another key → 409, tokens and hashes absent from logs, CLI `new-key`/`hash-key`, `serve` refusals, `serve` without `--config` → usage error, keys file with a UTF-8 byte-order mark loads, 8 loopback cases, `openapi` command in a fresh process equals the committed file. |
| `tests/test_api_concurrency.py` | 11 tests: two concurrent different submissions (one app) → exactly one 200 and one 409; the same race with the API's revision pre-check bypassed (service proxy returning a pre-review snapshot) so both forms reach `submit_review` and the **service's** one-review-per-case guard rejects the loser; the same across **two apps** on one workspace (real file-lock contention); concurrent identical retry → both 200, one review; concurrent start with one `request_id` on two apps → one case; full queue → 429 with `Retry-After`; default queue admits 8 waiting writes; HTTP-wait expiry with `request_id` → retryable 503 then a retry returns the same case with one retrieval; HTTP-wait expiry **without** `request_id` → non-retryable 503 with `next_action: list_cases`, no `Retry-After`, and exactly one case after the work finishes; restart with new apps keeps cases and allows completion; a real `python -m watchlist.api serve` process on a free loopback port, terminated and restarted on the same workspace, still serves the case. |
| `docs/api.md` | User-facing API guide: limitation, run commands, settings, auth, endpoints, review semantics, error table, concurrency model, headers, gaps. |
| `docs/openapi.json` | Generated by `python -B -m watchlist.api openapi > docs/openapi.json` (sorted keys, LF). Two tests fail if it drifts. |
| `docs/progress/completion-24.md` | This report. |

## Endpoint → service mapping

| Route | Role | Service call |
| --- | --- | --- |
| `GET /health` | none | — |
| `POST /v2/cases` `{name, origin, request_id?}` | reviewer | `start_case(name, origin=, actor=<key actor>, request_id=)` |
| `GET /v2/cases?limit=1..50&offset=&origin=` | viewer | `list_cases` |
| `GET /v2/cases/{case_id}` | viewer | `get_case` |
| `POST /v2/cases/{case_id}/review` `{submission_id, revision, packet_sha256, outcome, candidate_entity_id, reason}` | reviewer | `get_case` (revision check, skipped for a replay of the accepted `submission_id`) + `submit_review` with `actor=<key actor>`, `origin=<case origin>` |
| `POST /v2/cases/{case_id}/recover` | reviewer | `recover` |
| `POST /v2/cases/{case_id}/cancel` `{reason}` | reviewer | `cancel_case(actor=<key actor>)` |
| `GET /v2/cases/{case_id}/audit` | viewer | `audit` |
| `GET /v2/cases/{case_id}/audit/verify?anchor_count=&anchor_head_sha256=` | viewer | `verify_audit` |
| `GET /v2/entities/{entity_id}` | viewer | packaged fictional record; aliases built with the same `_evidence_for` helper the RapidFuzz retriever uses, so evidence IDs, attribution and `reference_sha256` match review packets (tested) |

Error mapping: `invalid_input` 422; `case_not_found`/`entity_not_found`/route 404; `stale_submission`, `submission_conflict`, `request_conflict`, `invalid_state`, `recovery_required`, `incompatible_version`, `retriever_mismatch` 409; `read_only` 403; `unauthenticated` 401; `forbidden` 403; `body_too_large` 413; `queue_full` 429; `workspace_busy`, `workspace_invalid`, `retrieval_failed`, `context_provider_failed`, `operation_pending`, `service_unavailable` 503; `storage_inconsistent`, `reentrant_call`, `internal_error` 500. This follows Decision 0007's proposal except that `workspace_invalid` is 503 (an unavailable component, as the Step 24 spec asks) and `read_only` is 403.

## Commands actually run and results

```sh
HF_HUB_OFFLINE=1 .venv/Scripts/python.exe -B -m watchlist.api openapi > docs/openapi.json
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_api.py tests/test_api_auth.py tests/test_api_concurrency.py
```
Final result after the review fixes below: **128 passed in 161.97 s, 0 failed** (`--collect-only` per file: 52 + 65 + 11). Before those fixes the suite was 122 passed in 161.13 s (50 + 63 + 9); the reviewer independently reran it at 122 passed in 147.96 s. The regenerated `docs/openapi.json` was byte-identical to the previous one (the fixes changed no schema). Most of the time is workspace initialization per test and the two real server start-ups.

```sh
.tools/qa/Scripts/python.exe -m ruff format --no-force-exclude --config pyproject.toml <the 10 owned .py files>
.tools/qa/Scripts/python.exe -m ruff check  --no-force-exclude --config pyproject.toml <the 10 owned .py files>
.tools/qa/Scripts/detect-secrets.exe scan <the 10 owned .py files> docs/api.md docs/openapi.json
```
Ruff: `All checks passed!` Detect-secrets: `{}` (no findings) after renaming a test variable and referring to environment-variable names through constants; the first scan flagged 4 keyword-shaped false positives (fictional placeholders, no real keys).

Hand check of check order (one-off script): malformed JSON without a key → `422 invalid_input`; duplicate-key JSON without a key → `401`; valid JSON without a key → `401`.

### Repository checks I ran but that do not pass on this machine

`tests/test_project_checks.py` + `tests/test_packaging.py` (with `QA_PYTHON=.tools/qa/Scripts/python.exe`): 9 failed, 42 passed, 6 errors. The causes shown are environmental and not in Step 24 files: Windows `WinError 1314` (no symlink privilege) in 4 symlink tests, `uv` offline `build`/`lock --check` exiting 2 (`uv --version` here reports `0.8.16`; the project pins `required-version = "==0.13.5"`), and the real secret scanner failing on its temporary fixtures. I did not change those tests, so I did not investigate further; the owner of the QA environment should rerun them on Linux CI. **My new files are not in `[tool.ruff] include`**; see shared-file requests.

## Adversarial review follow-up

An adversarial reviewer confirmed one medium and three low issues. Outcomes:

| # | Issue | Outcome | Regression test |
| --- | --- | --- | --- |
| 1 (medium) | A case start **without** `request_id` whose HTTP wait expired returned `503 operation_pending` with `retryable: true` and `Retry-After`, overriding the service's own non-retryable contract; a retry created a second case (reproduced by the reviewer: 2 cases listed). | **Fixed.** `_Runtime.write(..., retry_safe=...)`; `start_case` passes `retry_safe=body.request_id is not None`. The unsafe path now returns `503 operation_pending`, `retryable: false`, `details.next_action: "list_cases"`, no `Retry-After`. `request_id` stays optional because the Step 24 spec says so; `docs/api.md` still tells clients to always send one. | `test_http_wait_expiry_without_request_id_is_not_retryable` |
| 2 (low) | Invalid-UTF-8 and too-deeply-nested JSON bodies returned FastAPI's bare `400 bad_request`, not `422`. | **Fixed.** `_on_http_error` maps a `400` (only raised by FastAPI's body parser; the invalid `Content-Length` 400 comes from the middleware) to `422 invalid_input` without echoing input. Docs corrected. | `test_unparseable_bodies_are_422_not_400` (2 bodies × 2 routes) |
| 3 (low, Windows) | A keys file with a UTF-8 byte-order mark failed startup with a misleading actor-format error. | **Fixed.** Read with `encoding="utf-8-sig"`. | `test_keys_file_with_utf8_byte_order_mark_loads` |
| 4 (low) | `serve`'s default `--config` was computed from `parents[3]` of the module, valid only in a source checkout. | **Fixed.** `--config` is now required; `DEFAULT_CONFIG` removed. The documented command already passed it. | `test_serve_requires_explicit_config`; existing `serve` refusal test now passes `--config` |
| note | The race tests could not show that the **service** guard (rather than the API's revision pre-check) rejects a second review. | **Test added.** | `test_service_guard_rejects_second_review_when_precheck_is_bypassed` (asserts both forms reached `submit_review` and the loser's message is the service's "already has an accepted review") |
| note | Reads can wait behind a slow write's workspace lock (`503 workspace_busy` after 5 s). | Unchanged; documented, and listed below as a prerequisite for E5 through the API. | — |

## Example (fictional, from the tests)

`POST /v2/cases {"name": "Luma Vesper", "origin": "test"}` with a reviewer key → `200`, `case.status = "awaiting_review"`, `case.header.actor = "fictional-reviewer"`, candidates `syn-001`, `syn-012`, `limitation` = the service `LIMITATION` text, header `X-Research-Notice: research-only; ...`. Adding `"actor": "someone-else"` → `422 invalid_input` with `details.errors[0].type = "extra_forbidden"` and without echoing the value.

## Design decisions and deviations from the plan

- **Synchronous `200` instead of `202` + operation polling** (plan item 4). Retrieval over the fictional set is fast; idempotency comes from `request_id`/`submission_id`, and a long write gives `503 operation_pending` while still finishing. A durable accepted-operation table would need service-level storage (shared change; not done).
- **Versioned `/v2/...` routes only.** The reference's `/screen`, `/review/{id}`, `/audit/{id}` and any `/match` alias are not provided.
- **`revision`** is the SHA-256 of `CaseView.to_dict()`, not a counter: any durable change makes an old form stale.
- **No per-case ownership**: any reviewer key may act on any case (shared queue). The plan's "foreign" action rule is therefore not implemented beyond roles; a `request_id` reused by a different key does conflict (409).
- **One write worker per process** serializes writes; reads use the thread pool and the service's bounded file-lock wait, so a slow retriever can produce `503 workspace_busy` for reads.
- `GET /v2/entities/{id}` imports the private helper `watchlist.review.nodes._evidence_for` to avoid duplicating the alias-evidence rules.

## Limitations and open items

| Item | Status | Owner | Next action |
| --- | --- | --- | --- |
| External hosting, TLS, real identity provider | **BLOCKED** — `serve` refuses non-loopback hosts | project owner + Step 28 | approve hosting design; add TLS/auth proxy and re-test |
| Real (non-fictional) data | **BLOCKED** — only packaged fictional entities | data owner (Step 08) | no change in the API; the service retriever config decides |
| Async 202 + durable operation records + polling | not implemented | interfaces + service owner | add an operations table to the v2 service, then expose `GET /v2/operations/{id}` |
| Per-case ownership / reviewer assignment | not implemented | project owner | decide the policy; enforce in API or service |
| Rate limiting beyond the write queue | not implemented | interfaces | add per-key request limits if hosting is approved |
| Load/latency measurements | not measured | interfaces | measure before choosing final queue/timeout values |
| E5 retriever through the API | untested (fake + RapidFuzz only); case views would expose the E5 descriptor's local `cache_path` to viewers; a slow E5 write would hold the workspace lock and make reads return `503 workspace_busy` | interfaces | redact the path and measure lock hold times before enabling E5 |
| Linux run of these tests | not run here (Windows only) | QA/CI owner | run on Linux CI after the ruff/CI include update |
| UI (Step 25), MCP (Step 26), memory provider (Step 20) | separate steps | their owners | use `create_app(..., service=...)` or the service directly; providers go to the service |

All outputs remain possible record matches for research and human review over fictional records. Nothing here establishes identity, wrongdoing, sanctions status or clearance.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](review-p1-final.md) and [its validation record](review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
