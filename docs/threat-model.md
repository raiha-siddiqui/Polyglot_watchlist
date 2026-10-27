# Threat model — v2 review stores, reviewer memory, backups and anchors

Date: 2026-10-26. Scope: the local, single-machine research prototype (Completion Steps 19–21) on fictional data. This is a working threat model for the current code, not a security certification. It names what each control does and what it does **not** do.

## Assets

1. Integrity of review history: what evidence a reviewer saw (packet digest) and what they recorded (submission, final audit receipt).
2. Correctness of memory links: a prior annotation must only appear under the exact record it was about.
3. Honesty of presentation: memory is context, never identity, wrongdoing, sanctions status or clearance; demo/test history never appears as human history.
4. Availability and recoverability of the stores (backup/restore).
5. Confidentiality of reviewer notes and query text (fictional today; sensitive if real data is ever approved).

## Trust boundaries and actors

| Actor | Trusted for | Not trusted for |
| --- | --- | --- |
| Local OS account running the services | running code, holding locks, writing files | — (a privileged local attacker can rewrite everything) |
| HTTP/UI caller | holder of an issued key; API derives actor and role from it | human identity; bodies cannot override the actor, and origin is declared provenance |
| Local CLI/MCP process | operator-configured actor/permissions | human identity; MCP refuses human-origin writes |
| Reviewer free text, alias text, earlier notes | nothing — always data | instructions, SQL, identifiers, namespace registration |
| Configuration files (`config/memory.json`) | namespace/snapshot allowlist, policies | — they are reviewed project files |
| Backup and anchor media | holding copies | proving authenticity on their own |

## Threats and controls

| Threat | Control in code | Residual risk / limit |
| --- | --- | --- |
| Similar or identical names link different records (e.g. `syn-001`/`syn-012` "Luma Vesper", look-alike "Lumo Vespar") | Keys are exact `(namespace, entity ID, snapshot)`; `validate_identifier` rejects whitespace/name-like text; no name search exists. Tested with adversarial fixtures and the fictional evaluation harness (see the current review receipt for executed results). | Correctness depends on the retriever reporting the right entity IDs and on the snapshot registry being right. |
| The same ID reused by another source, or a changed snapshot, inherits history | Snapshot fingerprint is part of the key; snapshots map to exactly one namespace; each namespace has **exactly one** evidence `source` label (config rejects more, because `record_key` omits the source); evidence `source` must match or publication is quarantined. For the packaged RapidFuzz retriever the fingerprint digests each entity's `source`, so relabelled data gets another (unregistered) fingerprint. The provider returns `source_mismatch` with no cards when a request exposes `candidate_sources`. Other snapshots appear only in explicit inspection labelled "continuity not established". | `ContextRequest` now carries source labels from frozen candidate evidence; absent or foreign bindings fail closed. A malicious trusted adapter can still lie about both source and snapshot. No reviewed cross-snapshot continuity mapping exists yet (BLOCKED). |
| Memory changes ranking or outcomes | Provider sees only `ContextRequest`; the v2 service freezes its JSON into `packet.context` and never lets it alter candidates. Parity holds by construction; it is also measured once with the real packaged RapidFuzz retriever (memory on vs off, same query). | Scripted parity checks establish a contract, not retrieval quality; the integration demo separately compares the packaged retriever with memory enabled and disabled. |
| Stale history shown after correction/expiry | Withdrawal, supersession and expiry filter the default panel; key revisions let `check_freshness` detect changes, including formerly empty keys. | Acceptance rechecks under the review lock and holds the memory lock through durable acceptance. Missing providers fail closed; already accepted submissions replay before the guard. |
| Demo/test/unknown annotations presented as human | Origin stored per annotation; `display_origins` partitions; `unknown` never shown; origin printed on every card. | Origin is declared by the interface, not proven. |
| Duplicate or lost publication after a crash | Outbox intent per case (unique), single-transaction publish, deterministic annotation ID, tombstone check. Real `os._exit` crash tests at `after_enqueue`, inside the publish transaction and `after_publish_commit`. | Exactly-once execution is not claimed; effects are idempotent. |
| One bad outbox entry blocks all later publication (poison pill), e.g. a client `submission_id` reused in two workspaces | `enqueue` quarantines a submission ID already used by another case (`duplicate_submission_id`) before writing an intent. `drain` rolls back an unpublishable intent, quarantines it as `publication_blocked` (own transaction + chained event) and continues. Blocked intents are counted separately (`blocked` in reports, `blocked_intents` in `verify()`, `blocked_publications` + warning in the panel). | Blocked intents are not retried automatically; an operator must resolve them. |
| A copied/restored workspace republishes cases under a new label | `publication_intents.case_id` is globally unique; mismatch is quarantined as `case_published_from_other_workspace`. | — |
| Edited memory rows or audit rows | Append-only triggers; payload digests; hash-chained `memory_events`; `verify()` recomputes digests, chain and key revisions; schema signature check rejects dropped triggers. | **Not tamper-proof.** Whoever can write the file can recompute all hashes. |
| Truncation/rewind (restore of an old copy, deleted tail, removed case) | `export_anchor` records every case chain head, a catalogue digest and the memory event head; `verify_anchor` detects shorter chains, changed heads, missing cases and edited anchors. | Limited tamper evidence only; an anchor on the same writable disk is a local receipt. Keep anchors on separate media/custodian. An attacker who rewrites stores **and** the anchor is not detected. No remote timestamping/signing exists. |
| Prompt injection via notes ("ignore previous instructions…") | Notes are stored and displayed as JSON-escaped data; no LLM, tool or SQL interpretation; parameterized SQL everywhere. | Human readers can still be misled by a note's content (anchoring); Step 22 must measure this. |
| Corrupt/foreign/unavailable memory shown as "no history" | Identity, version, schema and integrity checks; unavailable/corrupt raises (`context_provider_failed` with `cause`); unregistered snapshot returns an explicit status. | — |
| Backup overwrites live data or restores silently corrupt data | Backups and restores go only to **new** directories outside the sources; manifest with file SHA-256 + logical digest; verification before and after restore; deep audit check of every case through the read-only service. | Activation of a restored copy is a manual operator step. |
| Inconsistent backup generation | Both locks (review, then memory) held during the copy; SQLite online-backup API; hot journals refused. | A writer that ignores the cooperative lock is not stopped (POSIX advisory locks). |
| Path escape / symlink tricks in backup, restore, retention | Destinations must be new, non-symlink and not overlap sources; retention deletes only manifest-bearing direct children of the backups directory. | — |
| Over-retention of notes | Retention plan/apply drill with tombstones and verified manifest; backup expiry. | Ledger copies are append-only (BLOCKED); deletion is not secure erasure. |
| Premature deletion through a future "now" | `plan --now` may forecast; `apply_retention` and `retention.apply` refuse a plan dated after the applying clock + 300 s (`retention_invalid`). The deletion manifest is created before anything is deleted, so an unusable manifest path deletes nothing. | A wrong system clock still moves the cutoff; the store clock is trusted. |
| Malformed anchor file crashes verification | `verify_anchor` checks the exact structure and types first and raises `backup_failed`. | — |
| Disclosure of notes to other local users | On POSIX, new store/backup directories are `0700` and files `0600`. | **On Windows no ACL change is made**; operators must place stores in a per-user directory. No encryption at rest or key recovery is implemented (BLOCKED for any real data). |

## Explicit non-goals of the current code

No proof of reviewer identity, no encryption at rest, no remote or signed anchors, no secure deletion and no multi-host deployment. The API authenticates issued bearer keys; the UI requires a separate key per session. `/ready` and `/metrics` require viewer access, while `/health` is public. The documented UI launchers restrict allowed hosts and host exposure to loopback. HTTP correlation IDs are server-generated; HTTP/MCP errors share a public-detail policy. The API launcher disables raw access logging. Containers were inspected but not built or run during this review. Any real-person data requires these to be revisited first.
