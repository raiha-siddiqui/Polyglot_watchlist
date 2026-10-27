# Data lifecycle — v2 review cases, reviewer memory and backups

Date: 2026-10-26. Scope: Completion Steps 20–21 on **fictional data only**. This page describes what the code in `src/watchlist/case_memory/` and `src/watchlist/review/{retention,backup}.py` does today and what it does not do. It complements the acquisition-side [data retention policy](data-retention.md); it does not replace it, and it authorizes no deletion of existing project artifacts.

## Terms (5W1H)

- **Annotation** — what: one completed v2 review projected into memory (outcome, reviewer opinion, actor label, origin, dates, workflow version, packet and final-audit hashes, and the exact reference-record IDs the case displayed). Who writes it: only `Publisher`, from a case that the v2 `ReviewService` reports as `completed` and whose audit chain verifies. It is a dated **opinion about an earlier query**, never an identity fact.
- **Exact key** — `(registered namespace, source entity ID, snapshot fingerprint)`. Names, alias text, query text and name hashes are never keys.
- **Visibility window** — 90 days by default (`config/memory.json` → `policy.visibility_days`). After it, an annotation is *suppressed* from new review panels (`now >= reviewed_at + 90 days`). This is a pilot freshness choice, **not deletion and not a legal retention period**.
- **Withdrawal / supersession** — appended corrections that change *future visibility*. The original row is never rewritten and stays available to explicit inspection.
- **Retention deletion** — removal of memory content after `retention.annotation_retention_days` (365 by default) by an explicit plan/apply drill, leaving a minimal tombstone.

## Inventory of content copies

| Copy | Where | Contains free text? | What Steps 20–21 do with it |
| --- | --- | --- | --- |
| v2 case ledger: header, evidence, packet, submission, cancellation, audit events | `review.sqlite` in the review workspace | yes: query name, reviewer reason, actor label, alias text | **Not deleted.** Append-only by design (Decision 0007). `retention plan` reports cases older than the retention period as `blocked_append_only`. |
| LangGraph checkpoints | same `review.sqlite` | yes (state copies) | Not deleted per case; deleted only with a whole ledger generation. |
| Frozen memory contexts | `packet.context.case_memory` inside later cases' packets | yes: earlier reviewer opinions | Not deleted (part of the append-only ledger). Listed in every deletion manifest under `copies_not_deleted`. |
| Memory annotations, record rows, outbox payloads, corrections | `memory.sqlite` in the memory store | yes: reason, actor label, correction reason | Deleted by `apply_retention`; tombstone + chained event remain. |
| Memory events, tombstones, quarantine rows, key revisions | `memory.sqlite` | **no** — IDs, digests, dates, outcome/origin codes only | Retained (needed for audit and replay safety). Stable IDs and hashes remain linkable to the ledger. |
| Backup sets | any directory created by `create_backup` | yes (full copies of both stores) | Deleted after `retention.backup_retention_days` (30 by default) by `retention apply`, only if the set has a valid manifest and is a direct child of the backups directory. |
| Anchor files | operator-chosen path | no (case IDs, counts, hashes) | Never deleted by the tool; owner responsibility. |
| Deletion manifests | operator-chosen path | no (IDs, digests, counts) | Kept as evidence of a drill. |
| Exports, traces, labels, trained derivatives | outside these stores | possibly | Not tracked here. Step 22 (feedback/labels) must extend this inventory before any label leaves the review stores. |

Deleting a memory annotation therefore does **not** erase the reviewer note from the system: the ledger, its checkpoints, later packets and older backups still hold copies until those generations are retired. The deletion manifest says so explicitly.

## Lifecycle of one annotation

1. A reviewer submits a v2 review; the case reaches `completed` with a final audit receipt.
2. `Publisher.enqueue` reads it through the read-only `ReviewService`, verifies the audit chain, projects it, and writes one **outbox intent** (`publication_intents`).
3. `Publisher.drain` publishes the intent in one transaction (annotation + record rows + `annotation_published` event + key revisions). An intent that cannot be published (for example a `submission_id` already used by a case from another workspace) is quarantined as `publication_blocked` and skipped; later intents still publish. A reused submission ID is normally caught earlier, at `enqueue` (`duplicate_submission_id`, no intent written).
4. New cases with `MemoryContextProvider` show the annotation for exactly the keys it covers, if its origin is displayable for the new case's origin, it is not corrected and it is inside the visibility window.
5. Optional: `withdraw` or `supersede` appends a correction; key revisions increase so frozen contexts that showed (or did not show) those keys are detectably stale (`check_freshness`).
6. After 365 days `plan_retention` lists it; `apply_retention` (or `python -m watchlist.review.retention apply --now PLANNED_AT --confirm-plan-id ...`) deletes its memory content in one transaction, writes a tombstone and verifies the result. The publisher never republishes a tombstoned case. A plan computed for a future `now` is only a forecast: applying it is refused (`retention_invalid`) until the clock reaches the plan time (300 s skew allowed). The deletion manifest file is created before any deletion.

## Origins

Every annotation carries the case origin (`human`, `simulated_demo`, `test`, `unknown`). `policy.display_origins` decides what a new case may see: a `human` case sees only `human` history; a `simulated_demo` case sees human and demo history; a `test` case sees human, demo and test history; `unknown` is never displayed automatically (configuration that tries is rejected). Simulated origin is shown on every card.

## What is not claimed

- Expiry and withdrawal are not deletion. Deletion is SQLite row removal and filesystem removal, **not secure erasure** of SSD blocks, snapshots, OS caches or backups held elsewhere.
- v2 does not yet separate content payloads from minimal audit receipts, so per-case deletion of ledger content is impossible without breaking verification. See the BLOCKED items in [completion-20-21](progress/completion-20-21.md).
- All data handled so far is fictional. Real-person or public-derived memory requires a separately approved purpose, terms review and retention decision first.

Review acceptance now takes the workspace lock, then the memory-store lock,
rechecks the frozen key revisions and shown cards' eligibility, and holds both
through the durable submission. A changed policy also invalidates the form.
Accepted submissions replay without consulting later memory. Context requests
carry frozen evidence-source labels; absent or foreign labels suppress memory
cards instead of treating a reused ID as the same record.
