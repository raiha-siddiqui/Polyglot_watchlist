# Polyglot Watchlist — research name matching

> Companion project: **Follow the Money**, a research agent that traces blockchain payments and can resolve sanctioned-wallet owner names through this project's review service, lives in its own repository.

A local multilingual candidate-retrieval prototype with a resumable human-review workflow. It suggests possible reference-record matches; it does not establish identity, wrongdoing, current sanctions status, or clearance. It is not a live screening/compliance service and is not affiliated with INTERPOL.

The default demonstration uses invented names. All existing fictional, public-subset, and bilingual evaluation examples have been inspected and are **regression-only evidence**. A regression check asks whether software changes preserve expected behavior on known examples; it does not measure performance on unseen data.

## Current state

> **Review follow-up (2026-10-27).** An independent adversarial review of the v2 stack fixed security and robustness issues: the reviewer UI now requires each reviewer to sign in with their own API key (no shared server token), Streamlit binds to loopback with restricted hosts, readiness and metrics need authentication, request IDs are server-generated, and error payloads no longer expose paths or SQLite details. Some figures reported in the step reports below could not be reproduced and were withdrawn. See [the review report](docs/progress/review-p1-final.md); where it disagrees with older text, it is current.

**Updated in Completion Step 29 (2026-10-27).** On top of the earlier work there is now a **v2 stack, implemented and tested on fictional data only**:

- one versioned review service, which every interface uses;
- three thin interfaces over it: a REST API, a reviewer UI and MCP tools;
- opt-in reviewer memory, which shows dated earlier annotations about exact record IDs;
- mechanics for preprocessing, baselines, hybrid retrieval, training, calibration and a grounded LLM harness.

No component makes an automatic match or clear decision. A human records `candidate_for_follow_up`, `none_selected` or `unresolved`, and every interface shows the research-only limitation notice. Real-data experiments, live LLM calls and hosting remain **blocked**; see [Blocked items](#blocked-items). The project code is released under the [MIT License](LICENSE). Exact test counts and commands are in the [Step 29 report](docs/progress/completion-29.md), and the [reproduction guide](docs/reproduction.md) gives the Linux/WSL and Windows commands.

| v2 capability (fictional data) | Implemented behavior | Evidence |
| --- | --- | --- |
| Review service v2 | `watchlist.review.service.ReviewService`, the one backend for the CLI, API and MCP. It provides: typed versioned contracts, a LangGraph human-review interrupt, a hash-chained audit, portable locks, idempotent and stale-safe submissions, crash recovery and context-provider hooks. | [Step 19](docs/progress/completion-19.md), [ADR 0007](docs/adr/0007-review-service-v2.md) |
| Reviewer memory | `watchlist.case_memory`, keyed by exact ID and snapshot only, never by name. It adds a replay-safe outbox, corrections, retention, and backup/restore with anchors. Its context is display-only and frozen into the packet digest. Opt-in through `--memory-store`/`--memory-config`. | [Steps 20–21](docs/progress/completion-20-21.md), [data lifecycle](docs/data-lifecycle.md) |
| REST API | FastAPI on loopback only, with hashed bearer keys (viewer/reviewer) and structured errors. Writes go through a bounded queue, and the OpenAPI contract is in [`docs/openapi.json`](docs/openapi.json). | [Step 24](docs/progress/completion-24.md), [API guide](docs/api.md) |
| Reviewer UI | Streamlit over the API. It shows escaped and bidirectional-safe evidence, a separate prior-annotation panel and audit verification. | [Step 25](docs/progress/completion-25.md), [reviewer guide](docs/reviewer-guide.md) |
| MCP tools | stdio server with `screen_name`, `get_case`, `list_cases`, `get_entity`, `get_audit` and `verify_audit`. There is deliberately no tool that submits a review. | [Step 26](docs/progress/completion-26.md), [MCP guide](docs/mcp.md) |
| Operations and hardening | Readiness and metrics, redacted JSON logs and traces, body and in-flight limits, runbooks, and a threat model. | [Step 27](docs/progress/completion-27.md), [operations](docs/operations.md), [threat model](docs/threat-model.md) |
| Preprocessing and baselines | Traced script/transliteration profiles, structured input and evidence reconciliation. The baseline matrix, hybrid RRF and a USearch HNSW index are compared with exact search. | [Step 10](docs/progress/completion-10.md), [Steps 11–12](docs/progress/completion-11-12.md) |
| Training and calibration mechanics | A bounded char-n-gram training pilot, versioned model artifacts, Platt/isotonic calibration and threshold-policy selection, all on generated fictional names. | [Steps 13–17](docs/progress/completion-13-17.md) |
| Grounded LLM harness | Parsing, explanations, validation, prompt-injection tests and a comparison harness, tested with fake or replayed replies only. No provider has been called. | [Step 23](docs/progress/completion-23.md), [LLM notes](docs/llm.md) |
| Release wiring | Makefile targets, CI (Linux job plus a Windows portability job), a container recipe, `THIRD_PARTY_NOTICES.md` and an end-to-end integration test. | [Step 29](docs/progress/completion-29.md), [reproduction](docs/reproduction.md) |

### Run the v2 stack (fictional)

The commands below assume Linux/WSL with the environment from the [reproduction guide](docs/reproduction.md). On Windows, use the direct `python` commands from that guide.

```sh
M="make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python"
$M demo                  # API -> human review -> memory -> prior-annotation context -> MCP read, into data/demo/run-<UTC>/
$M test-v2               # v2 stack and completion-step tests
$M eval-fictional        # fictional Steps 10-21 evaluations into a new data/eval-fictional/run-<UTC>/

$M init-v2 WORKSPACE=data/review/v2-local
$M memory-init MEMORY_STORE=data/review/memory-local          # optional
.venv/bin/python -B -m watchlist.api new-key --actor demo-reviewer --role reviewer
export WATCHLIST_API_KEYS='<entry>'                              # hash entry only
$M serve-api WORKSPACE=data/review/v2-local MEMORY_STORE=data/review/memory-local
$M serve-ui                                                     # http://127.0.0.1:8501; sign in with your own token
$M memory-publish WORKSPACE=data/review/v2-local MEMORY_STORE=data/review/memory-local
$M serve-mcp WORKSPACE=data/review/v2-local MCP_ORIGIN=test     # stdio, for a local MCP client
```

How these commands behave:
- **Memory is optional.** It is attached only when `MEMORY_STORE` is set, and it is opened read-only. Publishing completed cases is the separate `memory-publish` step.
- **Memory is display-only.** It never changes candidates, ranks or scores.
- **Nothing listens beyond this machine.** The API binds loopback only.

**Container recipe.** `Dockerfile` and `docker-compose.yml` hold a non-root container recipe. It has not been built or run yet; see [Blocked items](#blocked-items).

### Blocked items

Each item below stays unfinished until its evidence exists. None of them is solved by a limitation paragraph.

| Item | Why it is blocked | Owner | Next action |
| --- | --- | --- | --- |
| Step 08 reviewed corpus and genuine human review | The real multilingual corpus, the owner review (SOLO-E) and the accepted release do not exist yet. | Project owner (reviewer) with the data owner | Complete the genuine SOLO-E owner review of the 40-case handoff, then adjudicate and publish an accepted corpus release. |
| Real experiments: Steps 09, 14–16, 22, 28 | These depend on the Step 08 corpus. Fine-tuning also needs verified local E5/MiniLM weights. Step 22 needs real reviewers. | Coordinator with the project owner | After Step 08: freeze partitions and views (09). Approve the fine-tuning budget and run it (14–15). Select the pipeline on development (16). Run the memory-usefulness study (22). Freeze, then confirm once (28). |
| Live LLM: Step 18 comparison and Step 23 live run | No provider, model revision, data-transfer scope, retention, budget or request cap has been approved. No key exists. | Project owner | Approve those items. Then run `python -m watchlist.llm.comparison preflight`, followed by the approved `run --allow-live`. |
| Hugging Face Space (Step 30) | Account, publication and resource approval are missing, and no container build has been verified. | Interfaces/release, with the project owner as publisher | Approve network access for an image build. Build and smoke-test the container locally, then request publication approval. |
| Azure and Microsoft 365 (Step 31) | Tenant, budget and deployment approval are missing. There is no Postgres backend and no authenticated HTTP MCP. | Interfaces/release, with the tenant administrator | Obtain tenant and budget approval. Build and test the Postgres ledger and authenticated MCP locally first. |
| Container build and hosted CI run | No container runtime build was approved, so the image has not been built. The Linux and Windows CI jobs have not been run on hosted runners. | Project owner (approval) and the integration engineer | Approve a bounded image build (`podman build`) with a compose smoke test, and a hosted CI dispatch. Record the outcomes in a completion report. |

### Step 08 corpus status (history)

Step 08 now includes completed [C1 readiness queues](docs/progress/completion-08b-c1.md), [C2 review packets and ledgers](docs/progress/completion-08b-c2.md), [C3 adjudication preparation](docs/progress/completion-08b-c3.md), and the [40-case human-review handoff](docs/progress/completion-08b-h1-i.md). The approved [solo-researcher protocol](docs/progress/step-08-solo-protocol-proposal.md) preserves the original multi-reviewer track while explicitly narrowing evidence and evaluation claims.

[SOLO-I is formally closed](docs/progress/completion-08-solo-i-closed.md). [SOLO-E preparation and local activation passed](docs/progress/completion-08-solo-e.md); genuine owner review remains unfinished. The final SOLO-E focused check passed 52 fictional tests. Its earlier integration check passed 135 tests before the final language-tag/ledger-version correction; these are separate evidence snapshots. No independently validated labels or accepted corpus release are claimed.

This public checkout contains implementation, fictional tests and progress documentation. Source snapshots, real review packets, ledgers, declarations, live execution policies and local receipts remain private. See [publication scope](docs/publication.md). A fresh public checkout cannot activate the private pilot without its separately authorized, pinned local evidence and policy.

The approved **Step 08B-2R continuation completed OFAC/EU acquisition and offline validation**. OpenSanctions was reused. The [continuation report](docs/progress/completion-08b-02rc.md) contains measured yield, exact budgets and remaining corpus requirements. Earlier failed sessions below remain historical. Full Step 08 is incomplete; corpus construction, required genuine inspections, absent-query families and multilingual coverage gates remain open. No training or Step 09 execution is included.

Completion Steps 00–05 delivered the preservation and repair work below. The [Step 06 report](docs/progress/completion-06.md) records the engineering checks, approval scope and final acceptance status. Completion Steps are distinct from the historical Phase 0–10 records. See the [current phase guide](docs/implementation-phases.md), [completion plan](docs/project-completion-plan.md), and [evaluation repair report](docs/progress/completion-05.md).

The table below describes the earlier (v1) prototype capabilities, which remain available.

| Capability | Implemented behavior |
| --- | --- |
| Fictional baseline | Packaged records, Unicode normalization, deterministic RapidFuzz alias ranking, JSON CLI. |
| Source evidence | Verified v3 SQLite snapshots, schema/content checks, explicit rebuild of historical v2 sources into new destinations. |
| Multilingual retrieval | Pinned local E5-small, exact NumPy search, alias caches. |
| Reranking experiment | Pinned pretrained MiniLM scores frozen E5 shortlists; disabled in the review workflow. |
| Human review | LangGraph pause/resume, explicit annotations, saved evidence, deterministic explanations, audit links, checked crash recovery. |
| Evaluation | Bound query/source/view ancestry, separate partition/presence/ambiguity labels, script sets, explicit shortlist metrics. |
| Historical inspection | A separate archive reader verifies preserved cases without executing an incompatible workflow. |

The following do not exist:
- real-data fine-tuning (the training, calibration and policy code has run on generated fictional names only);
- an automatic match/clear policy;
- a live frontier-LLM comparison;
- real-data reviewer-memory evidence;
- a deployed HF, Azure or Microsoft 365 integration.

The v2 API, UI, MCP and memory exist, but only for local fictional use; see the v2 table above. The companion Follow the Money project (blockchain payment tracing) is maintained in its own repository. Missing external access is tracked in the [access/resource ledger](docs/access-and-resource-ledger.md).

## Run the existing environment

From the project directory:

```sh
export UV_CACHE_DIR="$PWD/.tools/uv-cache"

env PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .venv/bin/python -B -m watchlist --name '  LUMA   VESPER  ' --top-k 3

WATCHLIST_WHEELHOUSE=data/eval-comparisons/completion-06/wheelhouse \
  make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=.tools/bin/uv test
```

The CLI uses the packaged fictional fixture and needs neither model weights nor an API key. Tests use fictional records, deterministic vectors, and temporary stores. The full test target includes packaging checks, which require a compatible locally cached RapidFuzz wheel. The shown wheelhouse contains the separately approved CPython 3.14/macOS ARM64 wheel; another host needs its own approved compatible cache. Optional tests can skip when their packages are absent; a partial environment is not evidence that the full project passed. Already-local model checks are separate opt-in tests, not downloads.

**Python 3.14 is the declared target (`>=3.14,<3.15`); local CPython 3.14.0 on macOS ARM64 is the demonstrated runtime.** Python 3.11–3.13 are outside the narrowed declaration. Actual hosted runs use Python 3.14.0 on Ubuntu 24.04; their exact commits, outcomes and repairs are recorded in the [Step 07 report](docs/progress/completion-07.md). Windows support depends on the component:
- **The historical v1 agent** (`src/watchlist/agent/`) uses POSIX file locks and is not a Windows target.
- **The v2 stack** (review service, API, UI, MCP, memory and the completion-step modules) uses portable locks and has been tested locally on Windows with CPython 3.14. See the [Step 29 report](docs/progress/completion-29.md) for counts.
- **Hosted Windows CI** has a prepared job that has not yet run.

The inspected local `uv` 0.13.5 executable has been copied with a checksum receipt to `.tools/bin/uv`; no temporary installer path is required. The reviewed local metadata/tool cache is `.tools/uv-cache`. Set `UV_CACHE_DIR` to its absolute path so checks that change directory still use the same cache. Other hosts need compatible, approved tools and cached artifacts. The project requires that uv version, and Makefile commands accept a `UV` executable override. The existing `.venv` can run directly while its interpreter and packages remain available. A lockfile records exact package versions/artifact hashes; it does not prove every platform or optional combination works.

After obtaining any required installation/network approval, choose a nonexistent environment directory and request a fresh environment from the reviewed lock. The example uses `.venv-new` to preserve the baseline `.venv`:

```sh
export UV_CACHE_DIR="$PWD/.tools/uv-cache"

env UV_PROJECT_ENVIRONMENT=.venv-new UV_PYTHON_DOWNLOADS=never \
  .tools/bin/uv sync --locked --offline --dev --extra embeddings --extra agent --python python3.14

env UV_PROJECT_ENVIRONMENT=.tools/qa-new UV_PYTHON_DOWNLOADS=never \
  .tools/bin/uv sync --locked --offline --only-group qa --no-install-project --python python3.14
```

These commands succeed only if the interpreter and all selected packages are already available locally. Select the new environment paths through `PYTHON` and `QA_PYTHON` when using them. Stop on an offline cache miss; acquiring tools, dependencies, or weights needs a separate bounded approval. Do not regenerate the lock merely to make a demonstration run. A passing lock check establishes consistency with declared requirements; it does not prove a fresh installation of every optional extra.

## Local engineering checks

The Makefile selects installed tools explicitly and sets offline flags. It does not acquire missing dependencies. The application uses `.venv`; quality tools use the separate `.tools/qa` environment. Start with:

```sh
export UV_CACHE_DIR="$PWD/.tools/uv-cache"

make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=.tools/bin/uv help
make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=.tools/bin/uv lock-check
make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=.tools/bin/uv lint
make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=.tools/bin/uv secret-scan
make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=.tools/bin/uv eval-smoke
make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=.tools/bin/uv inventory

WATCHLIST_WHEELHOUSE=data/eval-comparisons/completion-06/wheelhouse \
  make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=.tools/bin/uv package-smoke
```

`eval-smoke` has passed with 157 deterministic current-contract fictional queries, excluding timing from its comparison. `inventory` separates installed packages, lock entries and unreviewed license metadata; see the [inventory evidence and limits](docs/access-and-resource-ledger.md). The project's own code is MIT-licensed; dependency, model and data terms are separate. An inventory is not a legal clearance.

The packaging checks build a wheel/source archive, rebuild the wheel from the archive, install the core package into an empty environment, verify packaged resources and native RapidFuzz, and run the fictional CLI outside the repository. They also exercise the separate locked QA setup. The core check installs only this project and RapidFuzz; it does not reproduce every optional model/agent dependency on every platform. Receipts under `data/eval-comparisons/completion-06/` bind each checked build to its input/artifact hashes; the Step 06 report records actual outcomes.

`build` requires a new `DIST_DIR`; existing output is refused. `test-local-models` is an explicit opt-in for the three already-local model checks. Quality tooling uses Ruff 0.14.10, pre-commit 4.5.1 and detect-secrets 1.5.0 in `.tools/qa`; `QA_PYTHON` can select another approved environment. The three pre-commit hooks invoke the same lock/lint/scan targets without installing dependencies or rewriting files. Scanner reports distinguish reviewed non-secret fingerprints/fixtures from unreviewed findings. A missing tool, a failed scanner, or wrapper tests alone cannot establish a successful source scan.

CI runs on pull-request/manual events with reviewed full action commit pins, read-only repository permissions and no persisted checkout credentials. Its approved bootstrap acquires locked dependencies and a size/hash-verified RapidFuzz wheel before a separate Linux network-isolated check stage. Actual hosted checks assert a distinct network namespace, dropped privileges/capabilities and no-new-privileges. The [Step 07 report](docs/progress/completion-07.md) distinguishes hosted results for the published Step 06 source plus CI repairs from the larger local Step 07 suite. Failed runs and their fixes remain recorded; dispatch alone never means the checks passed.

## How retrieval works

An **entity** is a reference record; an **alias** is one of its spellings. Different records may have the same name. The input is checked for nonblank text and the 200-code-point limit, normalized with NFKC/case folding/whitespace collapse, and compared with the selected aliases. Original text remains available as evidence. Normalization does not translate, transliterate, or strip accents.

RapidFuzz uses normalized Levenshtein character-edit similarity. E5 embeds queries and aliases into vectors and uses cosine similarity. Each returns one candidate per entity through its best retained alias, with stable ID-based ties. Neither score is an identity probability; unrelated input can receive suggestions because no rejection threshold exists.

The fictional exact-name example deliberately returns both `syn-001` and `syn-012` for `Luma Vesper` at 100 similarity. It demonstrates shared-name ambiguity. Historical measured comparisons remain in [Phase 4](docs/progress/phase-04.md) and [Phase 5A](docs/progress/phase-05a.md); they are not fresh generalization results.

## Source snapshots and compatibility

New source-backed work uses explicitly rebuilt v3 snapshots, when present locally:

```sh
.venv/bin/python -B -m watchlist \
  --db data/source-v3/sample.sqlite --name 'Moonberry Loom' --top-k 3
```

The original `data/sample.sqlite` and `data/watchlist.sqlite` are preserved historical v2 files. Normal loading rejects them with migration guidance; it never silently substitutes another database. See the [source migration guide](docs/maintenance/source-database-migration.md).

The public snapshot is a historical, narrow OpenSanctions UN selection. Provider terms were recorded at acquisition; Step 06 performs no new online license verification. The [data card](docs/data-card.md) distinguishes saved provenance from current permission. Public availability is not unrestricted-use permission or identity ground truth.

## Bounded source acquisition

Completion Step 07 adds a separate, permission-gated acquisition path and closes under the user-approved three-source feasibility scope: OpenSanctions UN, OFAC and EU. Direct UN and Wikidata remain investigated but not acquired; future use requires separate authorization and review. See the [approved closure record](docs/progress/completion-07-approved.md), [current permissions](docs/source-permissions.md), and [retention rules](docs/data-retention.md). A source being listed in the configuration does not mean it is enabled or usable. The historical UN-only importer and its 20-record behavior remain unchanged.

Inspect the local policy and exercise the new fictional tests without downloading anything:

```sh
make PYTHON=.venv/bin/python source-preflight
make PYTHON=.venv/bin/python source-tests
```

For an installed package outside this checkout, provide both the registry and
the project root containing its saved permission evidence. The package alone
does not bundle those local permission captures:

```sh
watchlist_project=/absolute/path/to/polyglot-watchlist-starter
python -m watchlist.ingest.acquire preflight --profile feasibility \
  --registry "$watchlist_project/config/data-sources.json" \
  --project-root "$watchlist_project"
```

Only an explicit `python -m watchlist.ingest.acquire fetch` command can acquire source data; there is no download hidden in these Make targets. Fetching requires reviewed permission evidence, an approved byte budget and a new private output directory. The CLI rejects blocked sources, unapproved redirects, unsafe paths and existing destinations. Receipts record response-body bytes, downloaded-body and retained-record hashes, parsing versions, source evidence and aggregate name/script counts. The original mixed response is filtered in memory and is not saved. Failures do not establish a usable corpus.

The retained records are organisations or vessels, with original names and available alias/language evidence. Similar names never establish shared identity. Wikidata records are supplementary and unlabelled; they are not confirmed sanctions absences or evaluation negatives. Source feasibility alone does not establish training permission, independent evaluation labels or statistical coverage. Step 08A planning and offline analysis are complete; the full Step 08 corpus is not complete. See the [Step 08A plan](docs/step-08a-plan.md) and [progress report](docs/progress/completion-08a.md). Step 08B-0 offline implementation and Step 08B-1 metadata preflight completed within their approved scope; their [historical report](docs/progress/completion-08b-01.md) preserves the test and metadata evidence.

The earlier Step 08B-2 session remains **partial and stopped**. OpenSanctions completed; OFAC returned an unfollowed redirect; EU was interrupted when resource monitoring failed. The completed selection was audited as development evidence, with no real human labels or final-test eligibility. All three request slots are consumed; no final acquisition receipt or corpus release was created. The [08B-2 report](docs/progress/completion-08b-02.md) records actual bytes/yield, focused tests, a reproduced monitor-race fix and the exact proposed recovery package. Further acquisition, human review and Step 09 require separate approval.

The earlier **08B-2R recovery session remains partial and stopped**. Recovery implementation and focused tests passed; OpenSanctions was reused unchanged. One OFAC attempt failed before an HTTP response, with no recorded body bytes; EU was not attempted. Offline diagnosis found an empty default TLS trust store. Native acquisition now blocks that condition before a claim/request, and the installed CA bundle was verified offline with certificate checking enabled. The exact failed exception was not retained, so the diagnosis is not claimed as its proven sole cause. The [08B-2R report](docs/progress/completion-08b-02r.md) records the failed attempt, conservative reservation, partial audit and a proposed continuation within the existing byte ceiling. No retry or candidate corpus publication occurred.

## Evaluate without overwriting history

All existing datasets remain exposed and regression-only, including their v2 representations. Current comparisons validate every query before cache/model work, send only retained aliases to retrieval, and require a new output root. With the already-approved local E5 files and optional dependencies available:

```sh
env PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  .venv/bin/python -B -m watchlist.compare_retrieval \
  --config eval/configs/embeddings.json --dataset multilingual \
  --output-root data/eval-comparisons/manual-multilingual-01
```

Choose a fresh destination each time; existing result/cache directories and historical result trees are refused. New caches live inside the output root. For the public subset, choose `--dataset public-small` and explicitly pass `--database data/source-v3/watchlist.sqlite`, with a different new root. Missing source/model files cause an error, not a download.

`--requested-k 5` evaluates a shortlist. Reports separate index population, requested K, returned count, and evaluated depth. Recall eligibility uses the index and depth. A missing target contributes zero to MRR@K without an invented later rank; full MRR is unavailable for truncated rankings. Threshold false-positive rate remains unavailable because there is no decision threshold.

`--regression-only` selects the exact **legacy row contract**. Omitting it selects the current contract; it does not make an exposed dataset fresh. Saved Phase 3/4/5 outputs and configs remain historical. In particular, do not run the old reranking evaluator as a default command: its historical output destinations are not isolated by the new comparison CLI. Use recorded replay evidence or a separately reviewed isolated harness.

## Local human review and recovery

Every successful retrieval, including an empty candidate list, pauses for human review. A **checkpoint** saves workflow progress; the **case ledger** saves requests, candidate evidence, annotations, and audit events. Both stores must describe the same permitted workflow state before execution resumes.

For a new fictional demonstration, prepare a separate agent JSON config from `config/agent.json`, preserving its policy fields and pointing `workspace` at a **new disposable directory**. Keep original configs/stores unchanged. Use a separate embedding config/cache when isolating model work. Replace these placeholders with the prepared config and returned UUID:

```sh
.venv/bin/python -B -m watchlist.agent start \
  --config /ABSOLUTE/PATH/TO/demo-agent.json --name 'Lumen Sira'
.venv/bin/python -B -m watchlist.agent status \
  --config /ABSOLUTE/PATH/TO/demo-agent.json --thread-id SAVED_THREAD_UUID
```

Copy the returned form, supply a new submission UUID, choose `candidate_for_follow_up`, `none_selected`, or `unresolved`, and enter your own reason. Follow-up requires a displayed candidate ID; other outcomes require JSON `null`. Preserve the exact packet digest, revision, and checkpoint ID. Submit with `review --review-file /ABSOLUTE/PATH/TO/review.json`, using the same config/thread ID. These are human annotations, not identity findings.

Duplicate/stale reviews fail. After an interrupted accepted submission use `recover`, not a replacement review. Status/audit inspection does not initialize missing storage. Recovery reuses surviving evidence; a genuine pre-retrieval request can still require its original local model. Never regenerate a missing companion, delete WAL files, or rewrite fingerprints to bypass an error. See the [recovery matrix](docs/maintenance/recovery-state-matrix.md).

For historical evidence use the separate archive interface:

```sh
.venv/bin/python -B -m watchlist.review archive verify --archive data/review-archives/v1
.venv/bin/python -B -m watchlist.review archive inspect \
  --archive data/review-archives/v1 --case-id SAVED_CASE_UUID
```

Archive verification never grants permission to execute the old workflow. Local actor labels and hash links are not authenticated identities or protection against replacement of all local records. Keep review notes and source/model artifacts private and outside release packages.

## Evidence and further reading

- [Completion Step 05: actual tests, model checks, historical replay and limits](docs/progress/completion-05.md)
- [Data card](docs/data-card.md) and [model card](docs/model-card.md)
- [Phase guide and approval gates](docs/implementation-phases.md)
- [Access, costs, terms and dependency inventory](docs/access-and-resource-ledger.md)
- [Architecture decisions](docs/adr/) and [backup/restore guidance](docs/maintenance/baseline-and-backup.md)
- v2 stack: [reproduction guide](docs/reproduction.md), [API](docs/api.md), [reviewer guide](docs/reviewer-guide.md), [MCP](docs/mcp.md), [operations](docs/operations.md), [threat model](docs/threat-model.md), [data lifecycle](docs/data-lifecycle.md), [third-party notices](THIRD_PARTY_NOTICES.md), [Step 29 report](docs/progress/completion-29.md)

Training, calibration, new sources, hosted services, and Microsoft 365 delivery need their own approved scope and evidence. Passing local fixtures cannot establish operational screening accuracy or a legal/compliance guarantee.
