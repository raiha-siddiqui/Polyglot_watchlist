# Reviewer guide — Streamlit interface (research only)

> **Limitation.** This interface shows *possible record matches* from a **fictional** reference dataset for **human review**. Similarity scores are raw and uncalibrated; they are not identity probabilities. Nothing shown establishes identity, wrongdoing, sanctions status or clearance. `none_selected` is **not** a clearance. Only you, the human reviewer, record an outcome, and the only outcomes are `candidate_for_follow_up`, `none_selected` and `unresolved`. The notice is shown at the top of every page.

Completion Step 25. Package: `src/watchlist/ui/`. The interface is a thin client of the review API ([api.md](api.md)), which wraps the one shared v2 `ReviewService` ([Decision 0007](adr/0007-review-service-v2.md)). Evidence: [completion-25](progress/completion-25.md).

## 1. Start the API and the interface (loopback only)

Requires the `api`, `agent` and `ui` extras. Use a temporary workspace; all data is fictional.

```sh
python -B -m watchlist.review v2 init --workspace ../data/review/v2-local
python -B -m watchlist.api new-key --actor fictional-reviewer --role reviewer
#   {"entry": "fictional-reviewer:reviewer:<sha256>", "token": "<keep secret>"}
export WATCHLIST_API_KEYS=fictional-reviewer:reviewer:<sha256>
python -B -m watchlist.api serve --workspace ../data/review/v2-local \
    --config config/review-v2.json --host 127.0.0.1 --port 8765

# second terminal
export WATCHLIST_API_URL=http://127.0.0.1:8765
# Enter your private token in the UI sign-in form; do not export it to the server.
streamlit run src/watchlist/ui/app.py --server.address 127.0.0.1 --server.port 8766 \
    --server.headless true --browser.gatherUsageStats false --server.allowedHosts localhost --server.allowedHosts 127.0.0.1
```

Open `http://127.0.0.1:8766`. The sidebar shows the API URL and `API key: configured`; the key itself is never displayed. Plain `http` is accepted only for loopback hosts; any other host needs `https` (and external hosting is not approved, see Step 28). If the configuration is invalid the page shows the error code `client_config_invalid` and the research notice, and nothing else.

Before screening, enter your own API key in the password field and press **Sign in**.
The API validates it before the session opens. A server-side `WATCHLIST_API_KEY`
is ignored by the UI. **Sign out** closes the client and clears that session's
cases, forms and evidence. Bearer keys identify the issued actor label; they do
not verify that the key holder is a particular human. Keep the UI on loopback,
keep the Host allowlist, and use one private key per reviewer. Browser automation,
accessibility and human usability remain unverified in this review.

## 2. Screen a name

1. Page **Screen a name**. Type a fictional name, for example the Arabic-script alias `سيرا لومين` of the fictional record `syn-007`.
2. Choose the **origin label**: `simulated_demo` (default), `test` or `human` (a person reviewing fictional data). The case's origin is fixed at start and every review of it carries the same origin.
3. Press **Screen name** (or Enter). The form sends one fresh `request_id`. If the response is lost (timeout, unreachable API) and you press the button again with the same name and origin, the same `request_id` is reused, so the API returns the same case instead of creating a second one. Changing the name or origin starts a new request ID.

## 3. Read the case

The **Current case** page shows, from top to bottom:

- **Case header** — screened name and normalized form (in `dir="auto"` elements, so Arabic renders right-to-left), case ID, origin (labelled fictional data), creation time, the API key's actor label, status and next action.
- **Possible record matches** — rank, entity ID, record name, the **matched alias in its original script** (the alias that actually matched, e.g. `سيرا لومين` rather than the Latin `Sira Lumen`), the **uncalibrated similarity score** with its `score_kind` (e.g. `normalized_levenshtein_similarity_x100`), and provenance (source, synthetic flag, attribution, provenance record or "none recorded"). An empty list says explicitly that this is not a clearance.
- **Prior annotations (reviewer memory)** — only if a memory provider was attached when the case was prepared. Each entry is a **dated earlier reviewer opinion** about the same exact record ID (date, earlier case, origin, outcome, relation, interpretation, opinion text, actor label), plus omission counts and a "reviewers differed" flag. They are context, not identity facts, and do not decide this review. If memory is unavailable (e.g. `unregistered_snapshot`) the panel says so and that this is not an empty history. Without a provider it says nothing is known about earlier reviews.
- **Other display-only context** — other providers' data, shown as JSON text.

All record text (names, aliases, reasons, notes, explanations, actor labels, provenance) is HTML-escaped before display and is never passed to Markdown with HTML enabled.

## 4. Record a review

1. **Outcome** — no outcome is preselected. Choose one deliberately:
   - `candidate_for_follow_up` — one listed record deserves further follow-up (not an identity finding). A **record selector** appears only for this outcome; pick the record by rank and ID.
   - `none_selected` — you selected no listed record. NOT a clearance.
   - `unresolved` — you cannot decide from this evidence.
2. **Reason (required)** — your own words, at most 1,000 characters. Do not paste real personal data.
3. The caption under the form shows what the form is bound to: the case **revision** and **packet digest** you are looking at and the form's **submission ID** (a UUID generated once per form).
4. Press **Submit review**. On success the page shows the recorded review (outcome, selected record, reason, actor label, time) and the template evidence explanation. The case becomes `completed`; one case accepts exactly one review.

**Double clicks and lost responses.** A rerun or second click resends the *same* form with the *same* submission ID; the API answers with the already-recorded review and never records a second one. If the answer is lost (`api_timeout`, `operation_pending`, `queue_full`, `workspace_busy`), the page tells you to **Reload case** first; if the review was recorded you will see it, otherwise pressing Submit again resends the same form safely.

**Stale forms (409).** If the case changed after you opened the form (another reviewer recorded a review, or the case was cancelled), the API refuses the form with `stale_submission`, `submission_conflict` or `invalid_state`. The page then:

- keeps your notes in the reason box,
- disables **Submit review** for that form,
- asks you to **Reload case**. After reloading, a case that is still awaiting review gets a **new form** (new submission ID, outcome cleared, your notes carried over) bound to the new revision; a case that is already completed or cancelled shows the recorded result and your notes as **"Your unsent notes (not submitted)"**.

A stale form is never silently re-bound to new evidence.

## 5. Other actions

- **Recover case** appears when the case needs recovery (for example after `retrieval_failed`). Recovery finishes interrupted work from durable state and never invents a review.
- **Cancel this case (not a review outcome)** — an expander with a required reason, available before a review is accepted.
- **Load audit and verify** — loads the hash-chained audit events (sequence, type, time, hashes; payloads in an expander as JSON text) and runs the local verification. "Audit chain verified locally" is tamper evidence for this workspace, not authentication of who acted. The view shows the case revision and UTC time it was loaded for; it is cleared whenever the case changes (review, cancel, recover, reload), so press the button again to see the current chain.
- **Case list** — summaries only (no names, evidence or notes), filter by origin (changing the filter returns to the first page), 50 per page, then **Open case**.

## 6. Error codes you may see

Errors show a short guidance sentence, the stable code, the HTTP status (or "no response") and the API's message. Errors are never shown as results: a failure or timeout is not "no match".

| Code | Meaning for you |
| --- | --- |
| `stale_submission`, `submission_conflict`, `invalid_state` | the form no longer matches the case; reload |
| `api_timeout`, `operation_pending` | outcome unknown; reload before anything else |
| `queue_full`, `workspace_busy` | busy; nothing recorded; retry shortly |
| `api_unreachable` | the API is not running or not reachable |
| `unauthenticated`, `forbidden` | key refused, or a viewer key tried to write |
| `retrieval_failed`, `context_provider_failed` | no result inferred; recover the case later |
| `client_config_invalid` | API URL or session key invalid |

## 7. Validation status

The [review receipt](progress/review-p1-final.md) records Linux AppTest and API checks. These are programmatic UI checks; no real browser session, accessibility assessment or human usability study was performed during this review. The previous browser walkthrough and scores are not asserted as current evidence.

If the API reports that reviewer memory changed or expired, start a fresh case; reloading the old frozen packet cannot refresh its history. The UI preserves unsent notes. Review the new evidence before reusing any notes.
