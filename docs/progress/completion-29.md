# Completion 29: reproducibility and integration

Recorded during the Project 1 adversarial review, 2026-10-27. This is an implementation and validation receipt for fictional research mechanics, not completion of the real-data evaluation or deployment plan.

## What exists

- `Makefile` exposes `init-v2`, memory initialization/publication, API/UI/MCP launchers, `test-v2`, `eval-fictional` and `demo`.
- `watchlist.stack` wires the shared review service and opt-in memory into the API/MCP paths. Its fictional demo uses an in-process API, an explicitly simulated annotation, memory publication, ranking comparison, audit verification and an MCP read.
- `scripts/eval_fictional.py` creates a fresh output root, invokes configured fictional experiments and records command exits, timings and output digests. Its default relative root resolves under the checkout. UUIDs, timestamps and environment metadata prevent blanket byte-for-byte reproducibility claims.
- `Dockerfile`, `docker-compose.yml` and `.dockerignore` define a non-root recipe, a private workspace volume, API hash-entry secret, read-only root filesystems and loopback host exposure. The UI asks for its own session key; no reviewer bearer token is passed through Compose environment configuration.
- The API requires viewer credentials for readiness/metrics and reviewer credentials for mutation. The UI launcher binds `127.0.0.1`, disables usage statistics and restricts allowed hosts. MCP remains local stdio with explicit write enablement and no review-submission tool.
- README, reproduction and operations guides now describe the current boundaries. The scoped completion reports were revised to remove unsupported historical totals and measurements. The reference HTML has LF line endings without a content rewrite.

## Environment and commands actually executed

Source checkout: `/mnt/d/Projects/raiha/Polyglot_watchlist-review`, branch `p1-review`.
Linux rsync mirror: `~/pw-p1-review2`.
Separate application and quality environments: `~/pw-tools/venv-p1-review2` and `~/pw-tools/qa-p1-review2`.

The initial mirror command excluded any directory named `data`, accidentally omitting the packaged fictional fixture. That setup error was corrected by excluding only root `/data/`; its failing diagnostic run is not final validation evidence.

The all-extras offline sync failed because the locked Linux Torch wheel was absent. The successful application sync used the locked `agent`, `api`, `ui`, `mcp`, `preprocessing`, `index` and `llm` extras. Quality tools were synced separately with `--only-group qa --no-install-project`. No dependency was downloaded. Test processes set `PYTHONDONTWRITEBYTECODE=1`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` and disabled external tracing.

The [final review receipt](review-p1-final.md) and [machine-readable validation record](review-p1-validation.json) contain the exact final commands, test counts, skip reasons, Ruff counts and smoke results. They also record corrected diagnostic failures. Scoped tests are the `V2_TESTS` list, partitioned into independent pytest processes with separate temporary directories; the legacy full suite was not run.

Final scoped coverage is **1,200 passed and two module-level skips**, with no remaining failures, errors or expected failures. The two skipped modules require Torch; their uncollected tests are not counted as passes. Ruff check and format check passed over **109 files**, including a final rerun after the WSL interruption. All saved pytest receipts were complete; no interrupted pytest run remained to rerun. The current 109 Python files match the tested mirror byte-for-byte.

Separately executed commands passed for offline lock checking, preprocessing evaluation, corrected LLM preflight, the fictional stack demo, corrected memory evaluation and an offline wheel/sdist build. The build used the final production code, before the final documentation edits; it was a Python package build, not a container build. Their exact commands and output summaries appear in the receipts. An initial LLM preflight used the provider config where the comparison config was required and returned a schema error; the corrected invocation uses `eval/configs/llm-comparison.json`. The first memory evaluation could not write its output because the parent directory was absent; the corrected run passed after creating it. No live provider call was attempted.

## What is not done

No Docker or Podman build or container smoke run was performed. Static inspection and launcher tests do not establish image buildability, runtime permissions or deployment security. Base images remain tag-pinned; digest pinning requires separately obtained image evidence.

No native Windows execution, real-browser walkthrough, hosted CI dispatch, public deployment, model download, live LLM experiment or real-data matching evaluation was performed during this review. Torch-dependent tests are skipped, not passed. Existing CI files belong to the other engineer and were not changed.

The project code licence is the owner's decision. No `LICENSE` or component-licence manifest was invented. Dependency notices and configuration proposals are not an approval to use sources or models. Model/API prices, availability and terms were not verified online.

## Reproduction and next work

Follow [reproduction](../reproduction.md), choosing new ignored output directories. The review records an honest partial environment and exercised integration path; a complete model-dependent validation still needs the missing approved dependencies. Any image build, live data/model acquisition, hosted run or publication requires its own concrete approved work package and receipt.

What: runnable fictional stack wiring. Why: connect implementation to observable evidence. Who: local research operators and human reviewers. Where/when: the checkout and dated Linux mirror above. How: managed environments, scoped tests, explicit smoke commands and durable receipts. Scripted review annotations remain explicitly simulated, never genuine human-review evidence.

## Licence decision (2026-10-27)

The owner chose the MIT License. `LICENSE` was added, `pyproject.toml` declares `license = "MIT"`, and the source archive includes the licence file; the packaging tests check both. Dependency, model and data terms remain separate and are not covered by this decision.
