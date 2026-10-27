# Completion Step 25 — Streamlit reviewer interface over the review API

Date: 2026-10-26. **Status: reviewer interface implemented and tested on fictional data only, loopback only. Step 25 is not fully closed:** a browser end-to-end suite, reconnect/session-expiry and concurrent-tab browser runs, accessibility (keyboard/contrast/small screen) checks and a reviewer usability session remain open or **BLOCKED** (see the end). All numbers below come from commands actually run in this checkout on the Windows development machine (`.venv` Python 3.14, Streamlit 1.66.1, httpx 0.28.1). They show software behaviour; they are not matching-quality, usability or reviewer-benefit results.

## Goal (5W1H)

- **What:** a Streamlit reviewer interface (`src/watchlist/ui/`) to screen a name, read possible record matches with evidence and provenance, see prior-annotation memory, record one human review, and inspect the case list and audit log.
- **Why:** the plan asks for a reviewer-usable workflow on top of the Step 24 API, without a second copy of review logic.
- **Who:** a human reviewer with an API key (actor label from the key). The interface never selects an outcome.
- **When:** after the API is running; one browser session per reviewer.
- **Where:** `streamlit run src/watchlist/ui/app.py --server.address 127.0.0.1` on the local machine, talking to `python -B -m watchlist.api serve` on loopback.
- **How:** `client.py` is a small typed `httpx` client for the API; `components.py` holds pure, escaped HTML builders; `app.py` holds layout and per-session form state. Every operation is an API call; the API calls the shared v2 `ReviewService` ([Decision 0007](../adr/0007-review-service-v2.md)).

## Files

All new and owned by this step; no other file was modified.

| File | Explanation |
| --- | --- |
| `src/watchlist/ui/__init__.py` | Package docstring; imports no Streamlit. |
| `src/watchlist/ui/client.py` | `ClientConfig(base_url, api_key)` (frozen; key hidden from `repr`; `from_env` reads `WATCHLIST_API_URL` (default `http://127.0.0.1:8000`) and `WATCHLIST_API_KEY`; plain `http` only for loopback hosts; URLs with credentials/query/fragment refused; invalid keys refused without echo). `WatchlistClient(config, *, http=None, timeout=40.0)` with `health`, `start_case`, `list_cases`, `get_case`, `submit_review`, `recover`, `cancel`, `audit`, `verify_audit`, `get_entity`. Default `httpx.Client` uses `trust_env=False` and `follow_redirects=False`, so the key goes only to the configured API. IDs are checked against the API's patterns before a URL is built. `ReviewForm` (frozen) is the exact review body. `ApiClientError(code, message, status, retryable, details)` keeps the server's stable codes; client-side codes are `client_config_invalid`, `invalid_input`, `api_unreachable`, `api_timeout`, `bad_response`, `http_error`. A 2xx response without a `limitation` string is `bad_response`. No automatic retries. `new_uuid()`. |
| `src/watchlist/ui/components.py` | Pure helpers: `RESEARCH_NOTICE`, `SCORE_LABEL`, `OUTCOME_LABELS` (exactly the three outcomes; `none_selected` labelled "NOT a clearance"), `esc` (HTML escape incl. quotes), `bidi` (escaped text in a `dir="auto"` element), `md_plain` (backslash-escapes Markdown/HTML/LaTeX/`:directive[` punctuation for alert boxes), `candidate_rows` (joins candidates to the evidence row of the **matched alias ID**, so the alias is shown in its original script), `candidates_html`, `case_header_html`, `review_html`, `review_choice_errors`, `memory_panel`/`memory_html` (`packet.context['case_memory']` as dated prior annotations; `unregistered_snapshot` etc. shown as "not an empty history"), `other_context`, `audit_rows`, `error_text`. |
| `src/watchlist/ui/app.py` | Pages **Screen a name**, **Current case**, **Case list**; research notice at the top of every run (also on configuration errors). Actions run in Streamlit callbacks before the page body. Record text reaches the page only through `st.html` with escaped strings, or through `st.dataframe`/`st.json`/`st.text_area` (text rendering); never `st.markdown(..., unsafe_allow_html=True)`. Audit views record the case revision and UTC time they were loaded for, are dropped whenever the case on screen is replaced (screen, open, reload, submit, recover, cancel), and are flagged if their revision differs from the case on screen. Changing the case-list origin filter resets the page offset to 0. Test seam: session key `_watchlist_ui_client` (otherwise a client is built from the environment). |
| `tests/test_ui_support.py` | Fixtures (collects no tests): `live_api` = real `ReviewService` in `tmp_path` + real `create_app` + FastAPI `TestClient` injected into `WatchlistClient`; `app_test` runs the real `app.py` with `AppTest.from_file`. |
| `tests/test_ui_client.py` | 32 tests (with parametrisation): env config, 6 refused and 2 accepted URLs, 4 invalid keys not echoed, key absent from `repr`, bearer header and exact bodies, no key on `/health`, missing key refused before any request, 6 path/ID injections refused before any request, structured error preserved, 5 malformed responses, unreachable/timeout retryable with reload guidance, default client ignores proxies and redirects. |
| `tests/test_ui_components.py` | 21 tests: escaping, `bidi`, `md_plain`, candidate join and original-script alias choice by `matched_alias_id`, malicious alias escaped in table and header, empty result wording, review/explanation escaping, outcome labels, 9 form-check cases, memory panel (ok, unavailable, absent, malformed), audit rows, error text. |
| `tests/test_ui_app.py` | 23 `AppTest` tests over the live API: notice on every page and key never shown; bad configuration; **screen → review (follow-up, chosen record) → audit verify → case list → reopen**; `none_selected` needs a reason and has no record selector; follow-up without a record refused client-side; form submission ID stable across reruns/reloads; lost screen response retried with the same `request_id` → one case; lost review response + resend of the same form → one review; lost review response + reload shows the recorded review (no "unsent notes"); stale form (another reviewer completed the case) keeps notes, disables submit, reload shows the other review and the unsent notes; stale-while-awaiting reload gives a fresh form (new ID, outcome cleared, notes carried) that then succeeds; malicious name/alias/reason escaped everywhere and no Markdown element allows HTML; Arabic query with the real RapidFuzz retriever renders the screened name and matched alias in `dir="auto"` elements with `syn-007` ranked first; prior annotations shown as dated opinions with escaped text; memory unavailable is "not an empty history"; empty result not a clearance; viewer key gets `forbidden` and no case is created; unreachable API on two pages; retrieval failure → recover; cancel needs a reason and is not an outcome; a loaded audit view is dropped when the case changes and reloading shows the new event count; an audit loaded for an older revision is flagged; changing the origin filter resets the page offset. |
| `docs/reviewer-guide.md` | Step-by-step fictional walkthrough, error codes, recorded browser run. |
| `docs/progress/completion-25.md` | This report. |

## Public API

```python
from watchlist.ui.client import ClientConfig, WatchlistClient, ReviewForm, ApiClientError, new_uuid
client = WatchlistClient(ClientConfig.from_env())        # or ClientConfig(url, key), http=<httpx.Client>
case = client.start_case("Luma Vesper", origin="test", request_id=new_uuid())
form = ReviewForm(new_uuid(), case["revision"], case["case"]["packet_sha256"],
                  "none_selected", None, "Fictional reason.")
client.submit_review(case["case"]["case_id"], form)

from watchlist.ui import components   # pure HTML/text helpers (no Streamlit)
```

Run: `streamlit run src/watchlist/ui/app.py --server.address 127.0.0.1` with `WATCHLIST_API_URL` and `WATCHLIST_API_KEY` set.

## Commands actually run and results

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider \
    tests/test_ui_client.py tests/test_ui_components.py tests/test_ui_app.py
# 76 passed in 79.05s   (32 client + 21 components + 23 app)  -- after the review fixes below
# (before the review fixes: 73 passed in 82.85s, 32 + 21 + 20)
.tools/qa/Scripts/python.exe -m ruff format --no-force-exclude --config pyproject.toml src/watchlist/ui/*.py tests/test_ui_*.py
.tools/qa/Scripts/python.exe -m ruff check  --no-force-exclude --config pyproject.toml src/watchlist/ui/*.py tests/test_ui_*.py
# All checks passed!
```

The first full run failed one test (`test_arabic_text_renders_right_to_left`): the table showed the Latin alias `Sira Lumen` although the matched alias was `سيرا لومين`, because the first linked evidence row was used. Fixed by choosing the evidence row whose `alias_id` equals the candidate's `matched_alias_id`; a unit test was added.

**Live browser smoke (manual, built-in browser pane).** Temporary workspace and a fresh fictional reviewer key in the session scratch directory (not in the repo); `python -B -m watchlist.api serve ... --host 127.0.0.1 --port 8765` and `streamlit run src/watchlist/ui/app.py --server.address 127.0.0.1 --server.port 8766 --server.headless true`. Observed: Arabic query `سيرا لومين` → 5 candidates, `syn-007` first (score 100.00, alias in Arabic script), `dir="auto"` present in the rendered DOM after Streamlit's HTML sanitizer; follow-up review recorded with a **double click** on Submit → the API log has exactly one review `POST` (200); case list → open case → audit "verified locally: 5 events"; screening `<img src=x onerror=document.title='XSS'>Luma` displayed the text literally, created no `<img>` and did not change the title. First retrieval took about 4 s (case created 17:16:18, evidence 17:16:22 UTC) and the first case-list render about 10 s (cold start). Both servers were stopped afterwards.

**Usability defect found and fixed.** After a successful submit the page also showed the submitted reason as "Your unsent notes (not submitted)". The panel is now hidden once the form was accepted (also when the accepted review's submission ID equals the form's after a lost response); covered by `test_screen_review_audit_walkthrough` and `test_lost_review_response_then_reload_shows_recorded_review`.

**Adversarial review fixes.** Two confirmed minor defects were fixed:

1. *Old "verified" audit result after the case changed (medium).* `_on_audit` cached the audit per case and nothing cleared it, so after a submit the page still said "verified locally: 3 events" while the API had 5. Now `_show_case` drops the cached audit for that case, the cached view stores the revision and load time (shown in a caption), and a mismatching revision shows a warning. Regression tests: `test_audit_view_is_dropped_when_the_case_changes`, `test_audit_from_an_older_revision_is_flagged`.
2. *Case-list offset not reset on filter change (low).* The origin `selectbox` now has `on_change=_on_list_page, args=(0,)`. Regression test: `test_origin_filter_change_resets_page_offset`.

Both regression tests for the defects were run against the code with the fix temporarily removed and failed (`2 failed`), then passed with the fix restored.

## Design decisions

- **Form binding.** One form per case response: `{submission_id, revision, packet_sha256}` stored in session state. Reruns and double clicks resend the same submission ID (API replay → no second review). A new revision gets a new form; a stale form is never revived, even if a reload shows an unchanged revision.
- **Screening idempotency.** One `request_id` per (name, origin) until success, so a retry after a lost response returns the same case.
- **Stale handling.** `stale_submission`, `submission_conflict` and `invalid_state` mark the form stale: notes kept, submit disabled, reload required. Retryable/unknown-outcome errors ask for a reload before resending the same form.
- **Escaping.** HTML only via `st.html` (no Markdown processing; Streamlit sanitizes it as a second layer) with `esc`/`bidi`; alert boxes use `md_plain`. Selectors show only rank and entity ID; radio labels are fixed constants.
- **Origin choices.** The UI offers `simulated_demo` (default), `test` and `human`; `unknown` only as a list filter.
- **Plan deviation.** The plan's file name `tests/test_ui_contracts.py` and `tests/e2e/` were replaced by the assigned `tests/test_ui_*.py`; there is no browser E2E suite (no approved browser tooling in the project). "Restart/resume" is covered by reopening cases from the list and API persistence, not by a dedicated browser restart test.

## Limitations and open items

| Item | Status | Owner | Next action |
| --- | --- | --- | --- |
| Automated browser E2E suite (plan item 5), reconnect/session expiry, concurrent tabs | not implemented; one manual browser smoke only | interfaces + QA | approve browser tooling (e.g. Playwright) and add `tests/e2e/` |
| Keyboard navigation, contrast, small-screen layout, screen readers | not tested | interfaces + human reviewer | run an accessibility pass; fix issues found |
| Reviewer usability session | **BLOCKED** — needs a human reviewer | project owner | run a fictional task session; record defects |
| Linked fresh case for stale forms (plan item 3) | partial: stale forms reload the same case; no "start linked case" action | interfaces + service owner | add a link field to the service if wanted |
| Real memory provider through the UI | tested with an authored `case_memory` context of the documented shape, not with `MemoryContextProvider` + published annotations | interfaces | add a test with a real memory store once the API exposes provider wiring |
| Reference-record view (`GET /v2/entities/{id}`) | client method only; not shown on a page | interfaces | add a record panel if reviewers need more than the candidate row |
| External hosting / TLS / real identity | **BLOCKED** — loopback only; key is shared per reviewer | project owner + Step 28 | approve hosting design |
| Real (non-fictional) data | **BLOCKED** | data owner (Step 08) | none in the UI |
| Linux run of these tests | not run here (Windows only) | QA/CI owner | run on Linux CI |
| CI/lint scope and `ui` extra wiring in README/Makefile | not changed (not owned) | INTEGRATE engineer | add `src/watchlist/ui/` and `tests/test_ui_*.py` to the ruff include list / CI, and link `docs/reviewer-guide.md` from README |

No dependency change was needed: `streamlit` (ui extra) and `httpx` were already declared and installed.

All outputs remain possible record matches for research and human review over fictional records. Nothing here establishes identity, wrongdoing, sanctions status or clearance.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](review-p1-final.md) and [its validation record](review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
