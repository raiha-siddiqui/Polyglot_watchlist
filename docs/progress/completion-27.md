# Completion Step 27 — observability, operations and adversarial hardening

Date: 2026-10-26 (revised the same day after an adversarial review; see [Review follow-up](#adversarial-review-follow-up)). **Status: local, dependency-free observability and the requested hardening tests are implemented and pass on fictional data; the five review findings are fixed or re-scoped; Step 27 acceptance is only partly closed.** The tracing-system choice (Phoenix/LangSmith), MCP/UI/LLM-wide instrumentation, a threat-model extension and several drills remain open or **BLOCKED** (table at the end). All numbers below come from commands actually run in this checkout on the Windows 11 development machine (Python 3.14.9, 22 logical CPUs, FastAPI 0.162.0, Starlette 1.10.0). They describe software behaviour on fictional data; they are not capacity, matching-quality or production claims.

## Goal (5W1H)

- **What:** structured, redacted JSON logs; request IDs on every API response; span records (operation, duration, outcome code) as JSON lines; in-process Prometheus-format metrics at `GET /metrics`; liveness (`/health`) vs readiness (`/ready`); per-process load shedding; adversarial, load and failure tests; a load smoke script; an operations guide and four runbooks.
- **Why:** plan Step 27 — "make failure diagnosable and recovery measurable before release".
- **Who:** an operator reads metrics, spans and logs and follows the runbooks; reviewers and clients see only `X-Request-ID` and structured errors. No observability signal feeds back into review logic or outcomes.
- **When/where:** inside the local API process; spans go to a local file chosen by the operator; nothing is sent anywhere.
- **How:** a new standard-library module `src/watchlist/observability.py`, wired into the existing API through middleware and a transparent proxy around the shared v2 `ReviewService` ([Decision 0007](../adr/0007-review-service-v2.md)). No review logic was reimplemented or changed.

## Files

New (owned by this step):

| File | Explanation |
| --- | --- |
| `src/watchlist/observability.py` | `Redactor` (documented policy: secret fields → `[redacted]`; any key with a part containing `name`/`alias`/`reason`/`quer`/`text`/`note`/`comment`/`body` (`HASHED_KEY_STEMS`, e.g. `display_name`, `matched_alias`, `cancel_reason`) → `hmac-sha256:<16 hex>;chars=n` with a random per-process key; other strings: bearer/basic credentials and `api_key=`/`x-api-key:`/`token=`/`secret=`/`password=` values removed, `Cc`/`Cf`/bidi/line-separator characters escaped as `\uXXXX`, 256-code-point truncation; paths → final component). Request IDs: `accept_request_id` (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`), `new_request_id`, `current_request_id`, `request_id_scope`. Logging: `LogContextFilter` (adds `request_id`, neutralizes interpolated arguments for any formatter), `JsonFormatter` (ASCII-only one-line JSON, redacted `fields`, exception type only), `install_log_filter`, `configure_json_logging` (idempotent). Metrics: `Counter`, `Gauge` (optionally callback-based), `Histogram`, `MetricsRegistry.render()` (Prometheus text 0.0.4, label escaping, 500-series cap with `_overflow`). Spans: `Tracer`/`Span` (JSONL, parent/child via context variables, outcome = `ok` or the error's stable `code`, rotation to `<name>.1`, sink `OSError` → dropped + counted + one warning), `read_trace`. `Observability` bundle, `InstrumentedService` proxy (8 service operations), `ObservabilityMiddleware` (pure ASGI: request ID, request span, HTTP metrics labelled by route template; an incoming ID found in an `Authorization`/`Proxy-Authorization`/`Cookie`/`X-Api-Key` header or accepted by the optional `is_credential` check is replaced). |
| `tests/test_observability.py` | 73 tests (47 original + 26 from the review follow-up: prefixed/suffixed personal keys, operational keys kept, a real `CaseView.to_dict()` through the redactor, `key=value` secrets in free text, API key sent only as `X-Request-ID`, `ReadinessProbe` caching, body-timeout settings): request-ID acceptance (13 cases), redaction policy, scrub/escape/truncate, JSON formatter, filter, idempotent setup, exact Prometheus rendering, metric validation/overflow, nested spans and outcomes, rotation, sink failure, proxy transparency, settings, API integration (echo/generation of `X-Request-ID` on 2xx–5xx, credential never echoed as ID, request and service spans share a trace across the write-queue thread, `/metrics` auth and content, `/health`, `/ready` 200/503, schema unchanged, JSON log setting). |
| `tests/test_security_hardening.py` | 26 tests (23 pass, 3 non-strict `xfail` documenting a finding for another owner; follow-up added stalled bodies vs the in-flight cap, the 408 body deadline and a real-uvicorn no-traceback check): oversized bodies (sized/chunked 413, 48,000-character name, 5,000-digit query, 10,000-character request ID); bidi/zero-width names; 10 SQL-like/path-traversal/NUL/CRLF/overlong case IDs against 7 routes with workspace file set and audit chain unchanged; raw-ASGI header injection (CRLF in `X-Request-ID` and `Authorization`, duplicates, non-ASCII); log injection; secrets, names and reasons absent from server logs, JSON logs, trace file and metrics. |
| `tests/test_load_failure.py` | 10 tests (follow-up added `/ready` = 200 while a write holds the lock, and a `/ready` flood vs authenticated reads): 20 concurrent writes with `write_queue_max=2` → exactly 3×200 + 17×429; 12 concurrent reads with `max_in_flight=4` → 4×200 + 8×429 while `/health` stays 200; 120 mixed concurrent requests → only 200/429/503-`workspace_busy`, never 500; lock held by a real second process → 503 `workspace_busy` retryable + `/ready` 200 `busy`, recovery after release; read-only database file (pins only the safety properties); trace directory missing; disk-full trace sink; load script smoke. |
| `scripts/load_smoke.py` | `run(requests, concurrency, …)` / CLI: in-process `TestClient`, new temporary workspace with the real RapidFuzz retriever over packaged fictional entities, deterministic one-letter-deletion names, warm-up excluded, `start_case` then `get_case` phases, nearest-rank p50/p95, max, mean, status/error-code counts, throughput. API key generated in memory, never printed. Exit 1 only on an unstructured 500. |
| `docs/operations.md` | Signals, real captured trace and its meaning, redaction policy, settings, metrics reference, measured single-process behaviour, failure table, limits. |
| `docs/runbooks/workspace-busy.md`, `recovery-required.md`, `audit-verification-failure.md`, `restore-from-backup.md` | Symptoms → meaning → steps → "never" lists, with the drills below. |
| this report | |

Modified (only to add middleware/hooks, as allowed):

| File | Change |
| --- | --- |
| `src/watchlist/api/settings.py` | New `ApiSettings` fields `max_in_flight=64`, `trace_path=None`, `trace_max_bytes=50 MiB`, `log_format="text"`, `body_timeout_seconds=10.0` (0 < t ≤ 300) with validation; `from_env` reads `WATCHLIST_API_MAX_IN_FLIGHT`, `WATCHLIST_TRACE_PATH`, `WATCHLIST_LOG_FORMAT`, `WATCHLIST_API_BODY_TIMEOUT_SECONDS`. Existing defaults unchanged. |
| `src/watchlist/api/app.py` | `InFlightLimitMiddleware` (`429 too_many_requests`, `Retry-After: 1`, `/health` exempt); `UnexpectedErrorMiddleware` (generic 500 answered and the exception stopped inside the app, so uvicorn never logs its message/traceback); `BodyLimitMiddleware` gained one body deadline (`408 request_timeout`); middleware order Observability → SecurityHeaders → UnexpectedError → BodyLimit → InFlightLimit (a request counts against the cap only after its body arrived); `ReadinessProbe` (coalesced, 1 s cache of healthy results, failures never cached); `create_app` builds a per-app `Observability`, wraps the service in `InstrumentedService`, installs the log filter, optional JSON logging, write-queue gauges; `_Runtime.write` runs the queued callable in a copy of the request context (request ID/parent span reach the worker thread); `_on_unexpected` adds `X-Request-ID` and logs it; `GET /ready` (no key; read-only open with **one** lock attempt; a held lock → `200` with check `busy`; other failures → `503 not_ready` with `details.cause`) and `GET /metrics` (viewer), both `include_in_schema=False` so `docs/openapi.json` is unchanged. |

No other file was touched. No dependency was added.

## Public API

```python
from watchlist.observability import (
    Observability, Redactor, Tracer, MetricsRegistry, InstrumentedService,
    ObservabilityMiddleware, JsonFormatter, LogContextFilter,
    configure_json_logging, install_log_filter, accept_request_id,
    new_request_id, current_request_id, request_id_scope, outcome_code, read_trace,
    PROMETHEUS_CONTENT_TYPE, REQUEST_ID_HEADER, REDACTED, HASHED_KEY_STEMS,
)
Redactor.is_hashed_field(name) -> bool          # added in the review follow-up
ObservabilityMiddleware(app, observability, *, is_credential=None)
from watchlist.api.app import ReadinessProbe, READY_CACHE_SECONDS
app = create_app(ApiSettings(api_keys=..., trace_path=Path("trace.jsonl"),
                             max_in_flight=8, log_format="json",
                             body_timeout_seconds=10.0), service=service)
app.state.observability.metrics.render()
app.state.readiness.runs   # lock attempts made by /ready
```

HTTP: `GET /health` (unchanged), `GET /ready` (`checks.workspace_read_only_open` = `ok` or `busy`), `GET /metrics` (viewer), `X-Request-ID` on every response. New error codes: `too_many_requests` (429, retryable), `request_timeout` (408, retryable) and `not_ready` (503).

## Commands actually run and results

```sh
cd D:\Projects\raiha\Polyglot_watchlist
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_observability.py tests/test_security_hardening.py tests/test_load_failure.py tests/test_api.py tests/test_api_auth.py tests/test_api_concurrency.py
# final state after the review follow-up:
# 234 passed, 3 xfailed in 196.56s (0:03:16)
# (first version, before the follow-up: 203 passed, 3 xfailed in 214.00s)
```

Per file (`--collect-only`, final): 73 + 26 + 10 = 109 Step 27 tests (106 passed, 3 xfailed in 79.22 s when run alone); the three Step 24 API files are 128 and pass unchanged (baseline before any Step 27 edit: 128 passed in 133.05 s). The 3 `xfail`s are the bidi-mark finding below.

The Streamlit UI and MCP suites, which talk to `create_app` or the same service, were also run after the follow-up: `tests/test_ui_app.py tests/test_ui_client.py tests/test_ui_components.py tests/test_mcp_tools.py tests/test_mcp_server.py` → 115 passed in 99.89 s (first version: UI suites only, 76 passed in 81.58 s).

`tests/test_packaging.py tests/test_project_checks.py`: 36 passed, 5 failed, 16 errors — the same environment conditions recorded in [completion-26](completion-26.md) (`uv` 0.8.16 vs required 0.13.5; Windows symlink privilege). Not caused by this step.

```sh
.tools/qa/Scripts/python.exe -m ruff format --no-force-exclude --config pyproject.toml <7 owned .py files>
.tools/qa/Scripts/python.exe -m ruff check  --no-force-exclude --config pyproject.toml <7 owned .py files>
# All checks passed! (re-run after the follow-up; 7 files already formatted)
.tools/qa/Scripts/detect-secrets.exe scan <owned code and docs>
# results: {} (re-run after the follow-up; two keyword false positives in new
# doc/test text were reworded, nothing real)
```

### Load smoke (local in-process fictional measurements)

`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -B scripts/load_smoke.py …`, each run on a new temporary workspace, one warm-up case excluded:

| Arguments | Phase | Status counts | p50 ms | p95 ms | max ms | Throughput |
| --- | --- | --- | --- | --- | --- | --- |
| `--requests 40 --concurrency 1` | start_case | 200×40 | 324.41 | 402.01 | 490.66 | 2.96/s |
| | get_case | 200×40 | 80.15 | 120.43 | 126.47 | 12.69/s |
| `--requests 40 --concurrency 8` | start_case | 200×40 | 2383.34 | 2593.18 | 2616.66 | 3.32/s |
| | get_case | 200×40 | 399.82 | 1779.61 | 2096.87 | 11.46/s |
| `--requests 100 --concurrency 32` | start_case | 200×9, 429 `queue_full`×91 | 249.78 | 2261.41 | 3728.35 | 26.71/s (mostly fast 429s) |
| | get_case | 200×84, 503 `workspace_busy`×16 | 2362.35 | 5397.96 | 5593.32 | 10.39/s |
| `--requests 100 --concurrency 32 --max-in-flight 8` | start_case | 200×8, 429 `too_many_requests`×92 | 163.71 | 1749.19 | 3237.86 | 30.71/s |
| | get_case | 200×8, 429 `too_many_requests`×92 | 242.86 | 1311.27 | 1853.85 | 52.73/s |

Re-run after the review follow-up (body deadline, cap moved inside the body limit, catch-all 500), same machine, other engineers' processes running concurrently:

| Arguments | Phase | Status counts | p50 ms | p95 ms | max ms | Throughput |
| --- | --- | --- | --- | --- | --- | --- |
| `--requests 40 --concurrency 8` | start_case | 200×40 | 2393.89 | 2748.72 | 2759.86 | 3.24/s |
| | get_case | 200×40 | 275.12 | 1783.82 | 2930.65 | 12.12/s |
| `--requests 20 --concurrency 4` | start_case | 200×20 | 1180.31 | 1287.19 | 1306.43 | 3.33/s |
| | get_case | 200×20 | 152.24 | 1066.17 | 1340.70 | 12.79/s |

Zero `500`s and zero dropped trace records in every run (all exited 0). An earlier run of the 32-concurrency configuration (before `internal_errors_500` was split out of the exit check) gave 60×200 + 40×503 for reads, so the 503 share varies run to run. Reading: one exclusive workspace lock serializes all access, so the process sustains about 3 case starts/s and about 12 reads/s here regardless of client concurrency; above that, the API sheds load with structured 429/503 instead of failing.

### Captured fictional trace

A start (`X-Request-ID: fictional-demo-start`, name "Luma Vespr", real RapidFuzz retriever), a review (`unresolved`), a missing case and a wrong key produced 8 span records; abridged in [operations §1](../operations.md#a-real-fictional-trace-captured-2026-10-26). The service span `review.start_case` (4513.94 ms, first call of the process) is the child of `http POST /v2/cases` (4535.919 ms); `review.submit_review` (214.77 ms) and its pre-check `review.get_case` (67.401 ms) are children of the review request; the missing case is `outcome: case_not_found` on both levels; the wrong key is `http GET /v2/cases` → `unauthenticated` (2.382 ms). The name appears only as `hmac-sha256:…;chars=10`; the reason text and keys appear nowhere. Matching JSON log lines carried the same request IDs.

### Drills

- **Backup/restore** (scratch directory outside the repo; 3 fictional cases via `v2 init`/`v2 start`): `backup create` exit 0 in 4.75 s wall; `verify` ok; `restore --destination … --review-config config/review-v2.json` ok, `case_count` 3, no issues, `elapsed_seconds` 17.06 (21.24 s wall); `restore --dry-run` ok, `elapsed_seconds` 16.21. Loss window: none in this drill (no writes after the backup); not a general zero-loss claim.
- **Audit tamper** (on a copy of the restored workspace): editing one `audit_events.payload_json` with the no-update trigger dropped and recreated → `v2 verify` `ok: false` with `audit event 1: event hash mismatch` and `audit: case_created payload differs from saved record`; dropping the trigger without recreating it → `workspace_invalid` (schema differs). The untouched copy verified `ok: true`.
- **Workspace locked by another process**, **read-only database**, **missing trace directory**, **disk-full trace sink**: automated in `tests/test_load_failure.py` (results above).

## Findings for other owners (not fixed here; files not owned)

1. **Implicit bidi marks accepted in names** — `src/watchlist/review/validation.py` `_BIDI_CONTROLS` lists U+202A–U+202E and U+2066–U+2069 but not U+200E (LRM), U+200F (RLM) or U+061C (ALM). Because names allow `Cf` characters, `"Luma\u200eVesper"` is accepted (HTTP 200) and stored. Observability escapes them, but displays may reorder text. Three **non-strict** `xfail` tests document it; when it is fixed they XPASS without failing (then remove the marks). Owner: review service. U+200B (zero-width space) is also accepted inside names; ZWJ/ZWNJ are needed by some scripts, ZWSP probably is not — a policy decision for the same owner.
2. **Read-only/full disk reported as `storage_inconsistent` (500)** — `storage.sqlite_failure` maps every non-lock SQLite `OperationalError` (including "attempt to write a readonly database" and, by the same code path, "database or disk is full") to `StorageIntegrityError`, whose public message says records are inconsistent. Suggest mapping `SQLITE_READONLY`/`SQLITE_FULL`/`SQLITE_IOERR` to `workspace_invalid` (503) with an I/O cause. Owner: review service. The Step 27 test now pins only the safety properties (structured 500/503, empty details, no path, never success), so the owner's change does not break a file they do not own; `tests/test_api.py` (API owner) still pins `storage_inconsistent` for an injected `StorageIntegrityError`, which is unaffected.
3. **`python -m watchlist.review v2 verify` exits 0 when `ok` is false.** Scripts must parse JSON. Suggest exit 1 like `backup verify-anchor`. Owner: review CLI.
4. **Read starvation under the exclusive lock** — 32 concurrent readers in one process can exceed the 5 s lock timeout (polling, no fairness). Mitigated operationally by `WATCHLIST_API_MAX_IN_FLIGHT`; a fair or shared-read lock is a storage-design question. Owner: review service / reliability. New evidence from the review follow-up: in the in-process `TestClient` harness, 30 threads flooding **`/health`** (which never touches the workspace) while 8 threads read one case gave 8/16 and 7/16 reads `503 workspace_busy` — the same as a `/ready` flood after the fix (6/16, 9/16; readiness made 17 lock attempts in 18 s). The client threads share the server's interpreter, so CPU/GIL saturation stretches lock hold times past the 5 s timeout. With 8 flood threads all 18 reads were 200 for both endpoints.

## Shared-file requests

- **INTEGRATE engineer:** Makefile targets `make test-observability` / `make security` / `make test-failure` running the three new test files, and optionally `make load-smoke` (`python -B scripts/load_smoke.py --requests 40 --concurrency 8`); a README pointer to `docs/operations.md` and `docs/runbooks/`. No dependency changes needed (stdlib only).
- **API owner (docs):** `docs/api.md` could mention `/ready`, `/metrics`, `X-Request-ID`, `429 too_many_requests` and the three new environment variables. If `/ready` and `/metrics` should appear in `docs/openapi.json`, drop `include_in_schema=False` and regenerate the committed file together (two Step 24 tests pin it).
- **`python -m watchlist.api serve`** (API owner): JSON logging and traces already work through the environment variables via `ApiSettings.from_env`. Requests: (a) pass a uvicorn `log_config` that routes `uvicorn.error`/`uvicorn.access` through `watchlist.observability.JsonFormatter` when `WATCHLIST_LOG_FORMAT=json` (today they stay plain text; request exceptions no longer reach them, but startup/lifespan errors and the access log's paths/query strings do), or offer `--no-access-log`; (b) set uvicorn `limit_concurrency` and keep `timeout_keep_alive` modest, because connections that never finish their **headers** are below the ASGI app and outside `body_timeout_seconds`.
- **`src/watchlist/api/errors.py`** (API owner, optional): `SAFE_DETAIL_KEYS` has no `timeout_seconds`, so `408 request_timeout` returns empty `details`; add it if clients should see the deadline.
- **Threat model** (`docs/threat-model.md`, other owner): add API-transport threats covered here (oversized bodies, header/log injection, hostile identifiers, overload) and the bidi-mark gap.

## Adversarial review follow-up

An adversarial reviewer reported five issues and one side note against the first version (75 passed, 3 xfailed at the time). Each was reproduced with the reviewer's probe scripts before fixing and re-run after.

| # | Finding | Verified? | Outcome | Regression tests |
| --- | --- | --- | --- | --- |
| 1 | HIGH: unauthenticated `/ready` took the workspace lock on every call (0.5 s wait), reported `not_ready` during any slow write, and contended with real requests | Yes: during a held write `/ready` was `503 workspace_busy` after 0.52 s | **Fixed.** One lock attempt with no waiting; a held lock → `200` with check `busy`; probes coalesced (thread lock) and healthy results cached 1 s; failures never cached. After: `/ready` during a held write → `200 busy` in 0.01 s; a 30-thread flood made 17 lock attempts in 18 s. Residual: authenticated reads under a 30-thread in-process flood still see some `503 workspace_busy`, but equally under a `/health` flood, so that is CPU saturation plus the unfair exclusive lock (finding 4 above), not readiness. | `test_ready_stays_200_while_a_write_holds_the_lock`, `test_ready_flood_does_not_make_authenticated_reads_fail` (8 flood threads, 22/22 reads 200, lock attempts ≤ elapsed/1 s + 2 and < probes/10), `test_readiness_probe_coalesces_and_caches_only_healthy_results`, updated `test_workspace_locked_by_another_process_is_503_retryable` |
| 2 | HIGH: in-flight cap counted requests still receiving a body, so stalled unauthenticated uploads filled it | Yes: 4 stalled `POST`s with `max_in_flight=4` → authenticated `GET` `429` | **Fixed.** Cap moved inside the body limit (counted after the body arrived); one deadline for the whole body (`body_timeout_seconds`, default 10 s) → `408 request_timeout`. After: same probe → `200`. | `test_stalled_bodies_do_not_occupy_request_slots` (8 stalled vs cap 4), `test_body_that_never_arrives_gets_408_within_the_deadline`, settings tests |
| 3 | MEDIUM: redaction matched exact field names only; `display_name`, `matched_alias`, `cancel_reason`, `query_text` … and `api_key=` in free text passed through | Yes: a real `CaseView.to_dict()` kept "Luma Vesper" | **Fixed.** Key parts containing a stem are hashed (`HASHED_KEY_STEMS`); `key=value`/`key: value` credentials scrubbed in all strings (format placeholders kept). After: no name in a redacted case view; all listed keys fingerprinted. | `test_prefixed_and_suffixed_personal_keys_are_fingerprinted` (13 keys), `test_operational_keys_stay_readable`, `test_real_case_view_through_the_redactor_has_no_name`, `test_scrub_removes_key_value_secrets_in_free_text` |
| 4 | LOW-MEDIUM: real uvicorn logged the traceback and message of every unexpected 500, contradicting operations §2 | Yes: probe printed `RuntimeError: boom name=… reason=…` | **Fixed in the app** (`UnexpectedErrorMiddleware` answers the 500 and does not re-raise) and **doc corrected** (§2 now names what is still outside the policy: uvicorn startup/lifespan lines and the access log). After: the probe shows only the JSON `watchlist.api` line and the access line. | `test_real_uvicorn_logs_no_exception_text_for_an_unexpected_500` (real uvicorn on 127.0.0.1); `asgi_request` no longer swallows a re-raise |
| 5 | LOW: strict xfails and a test pinning `500 storage_inconsistent` would break when other owners fix my findings | Yes (by reading) | **Fixed.** xfails are non-strict; the read-only-database test asserts structured 500/503 with empty details, no path, never success. | the two edited tests |
| note | API key sent only as `X-Request-ID` was echoed and written as `trace_id` | Yes | **Fixed.** The middleware also checks `Proxy-Authorization`, `Cookie`, `X-Api-Key` and, via `is_credential`, whether the value is a configured API key (SHA-256 compare through `KeyStore.authenticate`). | `test_api_key_sent_only_as_request_id_is_not_echoed_or_traced` |

Behaviour changes visible to operators: `/ready` no longer returns `503 workspace_busy`; it can be up to 1 s stale when healthy; a stuck lock is seen through `workspace_busy` metrics instead. The `workspace-busy` runbook and `docs/operations.md` were updated accordingly.

## Limitations and BLOCKED items

| Item | Status | Owner | Next action |
| --- | --- | --- | --- |
| Plan item 1: choose a tracing system (local Phoenix vs LangSmith) | **BLOCKED** (decision + dependency approval) | user / project owner | Decide; an exporter can read the JSONL span format or replace `Tracer`. LangSmith needs an API key and network, which this project forbids without approval. |
| Instrumentation of LangGraph nodes, retriever, MCP server, Streamlit UI, LLM calls | Not done | reliability (Step 27 follow-up) with interface owners | The proxy covers service operations for the API only; the MCP/UI processes can wrap their service with `Observability().instrument(service)`; node-level spans need hooks in `review/nodes.py` (not owned). |
| Plan item 4: threat-model extension; malicious serialization, symlink, stale replay, dependency/secret/license checks across API/UI/MCP/LLM | Partly covered | security owner | This step covers API transport attacks only. Stale replay and auth are covered by Step 24 tests; path/symlink by Step 19/20 tests. |
| Plan item 5 drills: crash/restart with recovery time, partial publication, expired credentials, provider failure, whole-bundle rollback | Not done | reliability | Backup/restore and audit-tamper drills were run (above); others need the memory publisher and release bundle. |
| Retention of trace files | Single rotation only | operator | Host log hygiene; no retention job. |
| Multi-process deployment | Unsupported | — | Measured capacity is single-process and lock-bound. |
| Linux run | Not run in this step | INTEGRATE / CI | Code uses no POSIX- or Windows-only calls; the read-only-database test skips when the OS user can still write (e.g. root). |

Everything ran on packaged fictional entities and authored fixture records. No metric, span, log line or error is an identity, wrongdoing, sanctions-status or clearance finding.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](review-p1-final.md) and [its validation record](review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
