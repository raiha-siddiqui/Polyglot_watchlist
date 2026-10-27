# Implementation guide and approval gates

Updated 2026-10-24 during approved Completion Step 06; status lines refreshed 2026-10-27 in Completion Step 29 (see [Status after Completion Step 29](#status-after-completion-step-29)). This is the living status guide. Historical `docs/progress/phase-*.md` and completed `completion-*.md` reports remain unchanged records of what was authorized, executed and observed at the time.

The [Project 1 proposal](reference/internship-2093-portfolio-plan.html) supplies the research ambition. The [completion plan](project-completion-plan.md) supplies the current dependency order and closure criteria. Neither document proves that a source license, model claim, free compute tier, SDK interface or deployment entitlement is current. Project 2 remains excluded.

## Two numbering systems

The original Phase 0–10 sequence organized the first prototype. The later Completion Steps 00–32 preserve, repair and finish that prototype. **Completion Step 06 is engineering reproducibility; historical Phase 6 is calibration.** Approval of one does not implement or authorize the other.

| Historical phase | Actual implemented scope |
| --- | --- |
| 0–1 | Design, packaged fictional entities/aliases, normalization, RapidFuzz matching, tests and JSON CLI. |
| 2–3 | Limited public-source ingestion/SQLite/provenance and initial baseline evaluation. |
| 4 | One pinned pretrained E5-small experiment using local exact search. |
| 5A | One pretrained MiniLM reranking pilot on frozen E5 candidates; no project fine-tuning. |
| 7A | Local LangGraph human-review pause/resume, saved evidence, deterministic explanations and audit. |
| 5B/5C, 6, broader 8–10 | Real-data training, calibration and delivery remain unfinished. Fictional-only mechanics now exist for training/calibration (Completion Steps 13–17), memory (20–21) and local interfaces (24–27); see the Step 29 status table. Plans are not completion evidence. |

The user-approved review workflow keeps E5 as the incumbent, MiniLM disabled in that path, and every successful retrieval subject to a human annotation. Research training/calibration are future work; automatic identity-match/clear decisions remain outside the current policy.

## Completed preservation and repairs

| Completion step | Delivered result | Evidence |
| --- | --- | --- |
| 00 | Preserved source/runtime/data/model evidence and original review-store state before changing fingerprinted code. | [Preservation report](progress/completion-00.md), [backup guide](maintenance/baseline-and-backup.md). |
| 01 | Separate inert historical archive verification/inspection, distinct from execution compatibility. | [Step 01](progress/completion-01.md). |
| 02 | Strict input validation, explicit initialization, nonmutating inspection and rejection paths. | [Step 02](progress/completion-02.md). |
| 03 | SQLite ownership/schema recognition, v3 source-content verification and independent rebuild into new paths. | [Step 03](progress/completion-03.md), [migration guide](maintenance/source-database-migration.md). |
| 04 | Case/checkpoint reconciliation, supported crash recovery, saved-evidence reuse and bounded cooperative locks. | [Step 04](progress/completion-04.md), [recovery matrix](maintenance/recovery-state-matrix.md). |
| 05 | Source/query/view ancestry, independent label axes, script sets and correct full/shortlist metric contracts. | [Step 05](progress/completion-05.md). |

Existing files, saved benchmark outputs and historical case fingerprints were preserved rather than rewritten to fit new behavior. Original source v2 databases remain archival inputs; normal source-backed work selects the rebuilt `data/source-v3/` snapshots explicitly. Old evaluation fixtures remain exposed regression evidence even when rebuilt with current contracts.

## Current engineering foundation: Completion Step 06

**Goal:** make the engineering foundation reproducible and expose access/resource blockers early. A reproducible setup means another eligible environment can build/install/run from declared inputs and retained evidence; it does not mean every platform is already validated.

**Scope:** Python/runtime declaration, dependency/build metadata and lock checks, package contents/outside-repository execution, scoped lint/pre-commit, deterministic fictional CI/evaluation, secret scanning, dependency/license inventory, incremental Makefile targets, these living documents, and the external-access ledger. The exact preflight is preserved at the end of [Step 05](progress/completion-05.md).

Only CPython 3.14.0 on macOS ARM64 has executed runtime evidence. The active metadata range is now `>=3.14,<3.15`; Python 3.11–3.13 are outside the narrowed declaration. The resolved lock has 113 entries and passes its offline consistency check, with the original selected runtime versions preserved. The prepared Linux 3.14 CI needs an actual approved remote run before it can be called verified. Windows is not a tested target.

Use the [Step 06 report](progress/completion-06.md) for acceptance status and executed checks. After the initial offline-only work and exact RapidFuzz wheel exception, the user authorized bounded tooling/dependency-metadata acquisition and read-only GitHub verification within 150 MB. This does not authorize hosted CI execution, source/model acquisition, source/model terms or account checks, Git operations or deployment. The local foundation uses uv 0.13.5, a dedicated QA environment/cache, core-package checks outside the repository, and a 157-query fictional smoke check. Actual Ruff checks passed, and the reviewed source scan reported zero unresolved findings while retaining the non-secret findings separately. CI has a prepared bootstrap and reviewed action pins; hosted execution remains separately approved work. See the [access/resource ledger](access-and-resource-ledger.md) for evidence, owners and closure artifacts.

The bounded tooling approval does not extend to other network acquisition, API keys, costs, Git operations, publication, or deployment. Documentation can record an unresolved external prerequisite without performing the access. The coordinator's Step 06 report records exact executed commands, actual results, limits and the next bounded preflight.

## Remaining completion sequence

These are work packages in the current plan, not newly approved implementation or promises of a favorable experimental result.

| Completion steps | Intended outcome and required evidence |
| --- | --- |
| 07–09 | Current source-permission decisions, bounded acquisition, reviewed multilingual corpus, duplicate/group adjudication and fresh disjoint release manifests. |
| 10–12 | Preprocessing/baseline experiments, hybrid retrieval and one justified vector-index implementation compared with exact-search evidence. |
| 13–16 | Reviewed training mechanics, actual bi-encoder/cross-encoder experiments and frozen model-selection evidence. No-gain or regression results are valid outcomes. |
| 17–18 | Independent calibration/threshold research and an approved frontier-LLM comparison protocol with controlled data transfer, cost and latency accounting. |
| 19–23 | Reusable review service, long-term reviewer history, lifecycle/recovery, feedback studies and bounded evidence-grounded LLM capability. |
| 24–27 | FastAPI, reviewer UI, MCP, authentication/authorization, observability, operational and security checks. |
| 28–29 | Freeze the chosen protocol, obtain independent final confirmation, reproduce artifacts and prepare the release candidate. |
| 30–31 | Actual permitted fictional hosting and Azure/Microsoft 365 integrations with account/tenant/budget evidence and teardown. Local manifests alone do not complete these steps. |
| 32 | Truthful report, error analysis, demonstration, learner explanation and separately approved release. |

A capability blocked by source rights, compute, credentials or tenant access remains unfinished until its required evidence exists. It must retain an accountable resolution owner and a concrete next deliverable; “optional” wording or a limitation paragraph cannot substitute for work the completion plan requires. Final confirmation labels must remain unused until the approved freeze/confirmation step.

## Status after Completion Step 29

Status as of 2026-10-27. Every "mechanics" entry was run on fictional, authored or generated data only. Those entries are software-behaviour evidence, not matching-quality, calibration or reviewer-benefit results.

| Completion steps | Status | Evidence |
| --- | --- | --- |
| 07 | Closed under the approved three-source feasibility scope. | [07 approved](progress/completion-07-approved.md) |
| 08 | **Open.** Acquisition, readiness and the review hand-off are done. Genuine owner review and an accepted corpus release are BLOCKED. | [08B-02RC](progress/completion-08b-02rc.md), [SOLO-E](progress/completion-08-solo-e.md) |
| 09 | **Not started** (BLOCKED on 08). | — |
| 10–12 | Mechanics on fictional data: preprocessing, baselines, hybrid retrieval, vector index. Real-data runs are BLOCKED on 08/09. | [10](progress/completion-10.md), [11–12](progress/completion-11-12.md) |
| 13, 17 | Mechanics: a bounded fictional training pilot, calibration and threshold selection. Real fitting is BLOCKED. | [13–17](progress/completion-13-17.md) |
| 14–16 | **Not started** (BLOCKED on 08 and on verified local weights). | — |
| 18 | Harness only, replay mode. The live comparison is BLOCKED on provider and budget approval. | [23](progress/completion-23.md) |
| 19 | v2 review service implemented; the v1 migration and E5 parity items are still open. | [19](progress/completion-19.md) |
| 20–21 | Exact-ID memory, outbox, lifecycle and backup implemented on fictional data. The items listed in that report remain BLOCKED. | [20–21](progress/completion-20-21.md) |
| 22 | **Not started** (needs real reviewers). | — |
| 23 | Offline mechanics complete. The live provider run is BLOCKED. | [23](progress/completion-23.md) |
| 24–27 | API, UI, MCP and observability implemented on loopback with fictional data. External hosting, TLS, a real identity provider and a third-party MCP client are BLOCKED. | [24](progress/completion-24.md), [25](progress/completion-25.md), [26](progress/completion-26.md), [27](progress/completion-27.md) |
| 28 | **Not started** (BLOCKED on 08/09/14–16). | — |
| 29 | Partly done. Wiring, CI jobs, Makefile targets, container recipe, notices and the reproduction guide exist, and local Windows runs are recorded. The project code is MIT-licensed. The container build and hosted CI run are BLOCKED. | [29](progress/completion-29.md), [reproduction](reproduction.md) |
| 30–32 | **Not started** (BLOCKED on approvals). | — |

**Windows.** The v2 modules and the completion-step modules have run locally on Windows (CPython 3.14). The historical v1 agent still uses POSIX file locks and is not a Windows target.

## Working and teaching contract

Before each approved phase, state the goal, why it matters, exact files, dependencies, expected commands, tests, risks and acceptance criteria. During it, explain meaningful operations before running them. After it, record actual changes, function/line explanations, commands/results, example input/output, limitations and a 5W1H recap, then stop for the next explicit approval. Existing authorization continues within that exact approved phase; it does not automatically open later phases.

The project owner approves scope/resources and human judgments. The coordinator integrates contracts and evidence; designated workers own bounded files to avoid conflicting changes. New libraries, data/models, costs, credentials, network access, unrelated edits, Git operations and public exposure require the applicable explicit approval. Never store secrets in the repository or invent results to satisfy an acceptance table.

## 5W1H recap

- **What:** a current map of implemented prototype capabilities, completed repairs and outstanding completion work.
- **Why:** prevent historical phase names, stale status text or a proposal from being mistaken for delivered evidence.
- **When:** update after approved work and use the next preflight before starting a later step.
- **Where:** living guides/cards describe the present; immutable historical reports retain the execution history.
- **Who:** the learner/project owner approves; the coordinator and assigned engineers implement and document; humans adjudicate labels and reviews.
- **How:** complete small testable scopes, retain provenance, verify commands and claims, surface blockers with owners, and stop at each approval gate.
