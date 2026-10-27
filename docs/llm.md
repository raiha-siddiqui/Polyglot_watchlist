# Grounded LLM helpers (`watchlist.llm`)

Status: offline mechanics implemented (Completion Step 23 plus the Step 18 harness), hardened after an adversarial review. **Live provider use is BLOCKED** until the owner approves a provider/model revision, the transmitted fields, retention, a monetary budget and a request cap. No provider has been called; no LLM result exists.

## What and why

- **What:** optional helpers that (1) turn a messy free-text search request into structured fields, (2) select cited facts about a frozen candidate packet, and (3) compare an LLM shortlist judge with the retrieval baseline on equal evidence.
- **Why:** the reference project asks for generative parsing and explanations, but this is a human-review research prototype. Model output must never establish identity, wrongdoing, sanctions status or clearance, and must never change scores, candidates or review outcomes.
- **Who decides:** only a human reviewer records `candidate_for_follow_up`, `none_selected` or `unresolved`. Nothing in `watchlist.llm` reads or writes those outcomes.

## Safety boundary

| Boundary | How it is enforced |
| --- | --- |
| Untrusted data | Requests, names, aliases and source fields are JSON-encoded with ASCII escapes inside `<untrusted_request>` / `<frozen_evidence>` tags; `<`, `>` and `&` are escaped so data cannot close the tag; bidi/zero-width controls are visible as `\uXXXX`. Templates are frozen and hashed (`prompts.PROMPT_SHA256`; current IDs `parse-grounded-v2`, `explain-claims-v2`, `judge-shortlist-v2`). |
| Hidden characters | Every format (`Cf`) character except ZWNJ/ZWJ, control/surrogate/private/unassigned code points, tag characters (U+E0000–E007F), variation selectors, U+034F, Hangul fillers and similar invisible marks are refused in extracted fields. A name containing them is never returned by `retrieval_name`; the query's `warnings` list them. |
| Parsing | Every field must quote a **whole-word** span of the input (not inside a longer word; all occurrences are checked). Values equal the quote (`exact`), except `entity_type`, which maps a stated keyword (`entity_type_keyword`). Names and aliases need at least 2 letters, at most 12 words and no structural punctuation (`;:<>{}[]|=@#\"` and backtick/newline/tab). No reformatted, translated or guessed dates/identifiers. **Every model-extracted field, including the name, is in `requires_confirmation`.** `retrieval_name(query)` returns `None` for a model-extracted name until the caller passes `confirmed={'name'}` after a person accepted it. A deterministic parse (the request as typed) is usable directly. |
| Explanations | The model only selects claims of five fixed types and cites packet IDs. **Claims have no free-text field**; any extra key rejects the output. Citations must exist and be of the right kind; values must equal packet values exactly. The model must cover the core facts: `candidate_count`, and `candidate_rank`, `similarity_score` and `matched_alias` for every candidate (`alias_source` is optional). Every sentence is rendered by code from cited values, with untrusted strings (including `score_kind`) JSON-quoted. |
| Packets | `build_evidence` applies the review-v1 link rules: unique string evidence references, no cross-entity or orphan evidence, no repeated alias ID, and exactly one winning evidence item whose text equals `matched_alias`. Violations are `ValueError` (caller error). |
| Fallback | Any client error, refusal/truncation stop reason, oversized request, invalid JSON, schema mismatch, ungrounded span, missing core claim or value mismatch rejects the whole output. Parsing falls back to name-only (whole trimmed request); explanation falls back to the deterministic `evidence-claims-v2` claim set. The reason is recorded. |
| State | Packets are deep-copied; results are separate objects. Reviewer reasons, memory notes and audit fields are never sent to the model. `ExplanationResult.to_dict()` contains no model-written text. |
| Provider | `AnthropicClient` (official `anthropic` SDK, lazy import, default model `claude-opus-5-5`, effort `low`) refuses construction unless `config/llm.json` approvals are complete *and* `allow_live=True`. Key only from `ANTHROPIC_API_KEY`, never stored or logged. No tools, no streaming, no telemetry, `max_retries` 0, timeout. Before every call it refuses a rendered request over `max_request_chars` (16,000) and any call whose **worst-case cost** (UTF-8 bytes of the request + 64 input tokens, plus the capped `max_tokens`, at the configured prices) would take spending past `budget_usd`. The worst case is charged up front and replaced by reported usage; if usage is missing or malformed the worst case stays charged. Server-side refusal fallbacks are deliberately not enabled: a different fallback model would silently change the compared model. |
| Receipts | `check_receipt` type-checks every field (token counts nonnegative integers or null, latency/cost finite nonnegative or null, known `evidence_kind`) and rechecks the response SHA-256. That hash is an **integrity check, not authentication**: anyone who edits a receipt can recompute it. Live runs record the receipts file hash in the report; pass it to `replay --receipts-sha256` to pin the file. |

Grounding validators check exact matches; they do not prove that a selected set of facts is the most relevant one. Authoritative text is template-rendered, so model wording never reaches a reader.

## Public API

```python
from watchlist.llm.client import (LLMClient, FakeClient, ReplayClient, RecordingClient, AnthropicClient,
                                  load_config, check_receipt, make_receipt, call_model)
from watchlist.llm.parsing import parse_request, deterministic_parse, needs_model, retrieval_name
from watchlist.llm.explanations import explain_packet, build_evidence, deterministic_claims, render_claim
from watchlist.llm.validation import (validate_parse, validate_claims, strict_json, disallowed_controls,
                                      aligned_spans, required_claims)
from watchlist.llm import comparison  # preflight / replay / run CLI

parse_request(text, client=None, config=None) -> StructuredQuery
retrieval_name(query, confirmed=()) -> str | None
explain_packet(packet, client=None, config=None) -> ExplanationResult
```

`packet` uses the review-v1 `candidates`/`evidence` shape (`watchlist.agent.state`). Without a client, both functions are fully deterministic.

## Commands

```sh
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_llm_contracts.py tests/test_explanation_grounding.py tests/test_prompt_injection.py tests/test_llm_comparison.py
# Step 23 evaluation over a cases file [{case_id, task: parse|explain, input}]:
.venv/Scripts/python.exe -B -m watchlist.llm.validation evaluate --config config/llm.json --cases CASES.json --receipts RECEIPTS.jsonl --output NEW_DIR
# Step 18 harness:
.venv/Scripts/python.exe -B -m watchlist.llm.comparison preflight --config eval/configs/llm-comparison.json
.venv/Scripts/python.exe -B -m watchlist.llm.comparison replay --config eval/configs/llm-comparison.json --receipts RECEIPTS.jsonl [--receipts-sha256 HEX] --output NEW_DIR
# Refused unless every approval is recorded and the flag is passed (same for `validation evaluate --allow-live`):
.venv/Scripts/python.exe -B -m watchlist.llm.comparison run --config eval/configs/llm-comparison.json --output NEW_DIR --allow-live
```

Output directories must not exist. Replay reports are built before the directory is created, so a bad receipts file leaves nothing behind. Live runs create the directory first (no call is made if that fails) and append each paid receipt to `receipts.jsonl` as soon as it returns, so a later failure never loses paid responses.

Comparison report statuses: `BLOCKED_no_llm_outputs`, `fixture_only_not_an_llm_result`, `incomplete_with_recorded_llm_errors`, `regression_only_not_a_comparison_result` (every run over the packaged fictional reference) and `complete_on_this_partition` (reachable only with a non-fictional reference, which is not implemented). Evaluation report statuses: `BLOCKED_no_llm_outputs`, `fixture_only_not_an_llm_result`, `measured_on_provider_receipts`.

## Step 18 comparison design (offline harness)

1. The RapidFuzz baseline shortlist (top 5) is frozen per query.
2. The judge sees exactly that shortlist, ordered by entity ID, with each record's display name and **all** aliases in alias-ID order. Baseline scores, ranks and the baseline's chosen alias are not shown (`include_baseline_scores: false`), so the judge cannot copy the baseline and cannot be credited for a record retrieval missed.
3. The judge must return a full permutation plus an `abstain` flag; confidence values are rejected (self-reported confidence is not calibrated).
4. Retrieval misses, absent queries, invalid outputs and errors are counted separately with explicit denominators; nothing is imputed.
5. Tokens, cost (configured list price, an estimate), cost per 1,000 queries and LLM latency come only from receipts. Missing values stay `null`.
6. `partition` must be allowed for the reference set (`packaged-synthetic-v1` allows only `fictional-regression-only`); `regression_only` follows from the reference, not from a label. A live run also needs `live.approved_partition` equal to `partition`. `preflight` reports the largest request and the worst-case total cost and blocks when it exceeds the budget.

The current config uses 12 authored fictional queries over the packaged synthetic records. They are regression checks of the mechanics only. The real experiment needs the frozen development protocol, the reviewed corpus and an approved provider run. Not implemented: the hybrid uncertainty-band arm, the E5/fine-tuned shortlist source and baseline-side latency.

## To unblock a live run (owner actions)

1. Verify the current model ID, terms, retention and price. Update `pricing_usd_per_mtok` if it changed.
2. In `config/llm.json`, set `provider_approved`, `data_transfer_approved` and `retention_approved`, a positive `budget_usd` at least the `preflight` worst case, `max_requests_per_run` and `live_enabled`, and record `approved_by`/`approved_at`.
3. In `eval/configs/llm-comparison.json`, set `live.enabled` and `live.approved_partition`.
4. Export `ANTHROPIC_API_KEY` in the shell only, run `preflight`, then `run --allow-live`. Keep the generated `receipts.jsonl` immutable, note `receipts.file_sha256` from the report, and use `replay --receipts-sha256` afterwards.
