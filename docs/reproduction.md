# Reproduction guide (Completion Step 29)

**Read this first.** This project is a **research-only** prototype that runs on **fictional data**. It suggests *possible* reference-record matches for a *human* reviewer. It never establishes identity, wrongdoing, sanctions status or clearance, and nothing in it is a live screening service.

This guide gives the exact commands for:
- recreating the environments from the lock;
- running the tests, the fictional evaluations and the fictional demo;
- serving the API, the reviewer UI and the MCP tools on your own machine.

The [Step 29 report](progress/completion-29.md) records which of these commands were actually run, on which platform, and what they returned. When a command below has no recorded result there, treat it as unverified.

## 0. What you need

| Item | Requirement | Notes |
| --- | --- | --- |
| Python | CPython **3.14** (`requires-python = ">=3.14,<3.15"`) | The lock pins `==3.14.*`. This review uses the installed locked environment; hosted CI was not run. |
| uv | **0.13.5** exactly (`[tool.uv] required-version`) | Other versions refuse to run. |
| Checkout | This repository, with `uv.lock` unchanged | Do not regenerate the lock to make a run pass. |
| Network | Only for the first `uv sync`, which downloads locked wheels | Everything after that runs offline. Downloading needs your own approval. Nothing downloads models or data. |
| Disk | The `embeddings` extra pulls Torch, which is large on Linux because of the NVIDIA wheels | `--skip-torch` / the reduced extras below avoid it. |

Never put API keys, tokens or real names into the repository. Generated workspaces, memory stores, evaluation outputs and demo outputs go under the documented ignored roots (`data/review/`, `data/demo/`, `data/eval-fictional/`). Not all of `data/` is ignored.

## 1. Linux or WSL (bash)

### 1.1 Create the environments from the lock

```sh
cd /path/to/polyglot-watchlist
uv --version   # must print 0.13.5

# Application environment: every extra (needed by the full test suite).
env UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT="$PWD/.venv" \
  uv sync --locked --dev --all-extras --python python3.14

# Quality tools (ruff, pre-commit, detect-secrets) in their own environment.
env UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT="$PWD/.tools/qa" \
  uv sync --locked --only-group qa --no-install-project --python python3.14
```

To install without Torch, replace `--all-extras` with `--extra agent --extra api --extra ui --extra mcp --extra preprocessing --extra index --extra llm`. Torch-dependent test modules then skip, and `make eval-fictional` needs `EVAL_FLAGS=--skip-torch`.

If the wheels are already in uv's cache, add `--offline`. Stop if a wheel is missing from the cache: a cache miss needs a separately approved download.

### 1.2 Checks, tests, evaluations and demo

The Makefile sets the offline flags itself: `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`, `UV_OFFLINE`, `PIP_NO_INDEX` and the tracing switches.

```sh
M="make PYTHON=.venv/bin/python QA_PYTHON=.tools/qa/bin/python UV=$(command -v uv)"

$M lint            # ruff check + format --check over the reviewed scope
$M lock-check      # lock consistency, offline
$M test-v2         # v2 stack + completion-step modules (list in Makefile V2_TESTS)
$M test            # every non-integration test; packaging checks also need WATCHLIST_WHEELHOUSE
$M eval-smoke      # legacy fictional regression target; outside this review
$M eval-fictional  # Steps 10-17 + 20-21 fictional evaluations into a NEW data/eval-fictional/run-<UTC>/
$M eval-fictional EVAL_FLAGS=--skip-torch   # same, without the training -> calibration -> policy chain
$M demo            # fictional walkthrough into a NEW data/demo/run-<UTC>/
```

What two of these targets do:
- **`eval-fictional`** writes a `manifest.json` that lists each command, its exit code and wall time, and the SHA-256 of every output file. Timing fields differ from run to run. UUIDs, timestamps and environment fields also differ. Compare deterministic results separately; the manifest records checksums, not a claim of byte-for-byte reproducibility.
- **`demo`** runs the API in-process, with no listening socket and a key that exists only for that run. It then:
  1. starts a case for the fictional name `Luma Vesper`;
  2. records one fictional review annotation;
  3. publishes the case to reviewer memory;
  4. starts a second case and shows the earlier annotation as display-only context;
  5. checks that the ranking is unchanged and that the audit chain verifies;
  6. reads the case through the MCP tool layer.

  It saves `demo-summary.json` in the output folder.

### 1.3 Serve the stack on loopback

```sh
# One-time setup. Both commands refuse a non-empty directory.
$M init-v2 WORKSPACE=data/review/v2-local
$M memory-init MEMORY_STORE=data/review/memory-local     # optional: reviewer memory

# Make a reviewer key. The token is printed once; keep it out of files and history.
.venv/bin/python -B -m watchlist.api new-key --actor demo-reviewer --role reviewer
export WATCHLIST_API_KEYS='<the printed "entry" value>'

# Terminal 1: API on 127.0.0.1:8000 (memory attached only because MEMORY_STORE is set).
$M serve-api WORKSPACE=data/review/v2-local MEMORY_STORE=data/review/memory-local

# Terminal 2: reviewer UI on 127.0.0.1:8501.
# Enter your private token in the UI sign-in form; do not export it to the server.
$M serve-ui

# After reviews are completed, publish them so later cases show them as context.
$M memory-publish WORKSPACE=data/review/v2-local MEMORY_STORE=data/review/memory-local WORKSPACE_ID=v2-local

# MCP over stdio (for a local MCP client; paths are made absolute by make).
$M serve-mcp WORKSPACE=data/review/v2-local MCP_ORIGIN=test
```

How these commands behave:
- **Memory is opt-in.** Without `MEMORY_STORE`, no memory context is attached. With it, the API and MCP server open the store **read-only**. Publishing is always the separate `memory-publish` step.
- **The API only binds to loopback.** `watchlist.api serve` refuses any non-loopback host.
- **The MCP server needs absolute paths.** It refuses relative paths for the workspace, the config and the memory options.

## 2. Windows alternatives (not executed in this review)

Use Ubuntu 24.04 WSL for this project. For native-Windows users the equivalent command forms use `.venv\Scripts\python.exe`. Set the offline flags yourself:

```powershell
cd D:\path\to\polyglot-watchlist
uv --version   # 0.13.5

$env:UV_PYTHON_DOWNLOADS = "never"
$env:UV_PROJECT_ENVIRONMENT = "$PWD\.venv"
uv sync --locked --dev --all-extras --python 3.14
$env:UV_PROJECT_ENVIRONMENT = "$PWD\.tools\qa"
uv sync --locked --only-group qa --no-install-project --python 3.14
Remove-Item Env:UV_PROJECT_ENVIRONMENT

$env:HF_HUB_OFFLINE = "1"; $env:TRANSFORMERS_OFFLINE = "1"; $env:PYTHONUTF8 = "1"
$env:LANGSMITH_TRACING = "false"; $env:LANGCHAIN_TRACING_V2 = "false"

# Lint: given directories, ruff checks only the files in pyproject's [tool.ruff]
# include list (the same scope as QUALITY_FILES in the Makefile).
.tools\qa\Scripts\python.exe -I -B -m ruff check --config pyproject.toml src tests scripts
.tools\qa\Scripts\python.exe -I -B -m ruff format --check --config pyproject.toml src tests scripts

# Tests: the v2 list from the Makefile (V2_TESTS); for example:
.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider tests\test_integration_v2_stack.py tests\test_api.py

# Evaluations and demo (each refuses an existing output folder).
.venv\Scripts\python.exe -B scripts\eval_fictional.py
.venv\Scripts\python.exe -B -m watchlist.stack demo

# Serving (as in 1.3):
.venv\Scripts\python.exe -B -m watchlist.review v2 init --workspace data\review\v2-local --config config\review-v2.json
.venv\Scripts\python.exe -B -m watchlist.stack memory-init --store data\review\memory-local --memory-config config\memory.json
.venv\Scripts\python.exe -B -m watchlist.api new-key --actor demo-reviewer --role reviewer
$env:WATCHLIST_API_KEYS = "<entry>"
.venv\Scripts\python.exe -B -m watchlist.api serve --workspace data\review\v2-local --config config\review-v2.json `
  --memory-store data\review\memory-local --memory-config config\memory.json
# second window:
$env:WATCHLIST_API_URL = "http://127.0.0.1:8000"
.venv\Scripts\python.exe -B -m streamlit run src\watchlist\ui\app.py --server.address 127.0.0.1 `
  --server.headless true --browser.gatherUsageStats false --server.allowedHosts localhost --server.allowedHosts 127.0.0.1
```

**Lint scope.** The directory form follows the configured include list; the Make target lists explicit files. This review records its own scoped Ruff invocation separately. No native-Windows lint execution is claimed.

**Portability.** The v2 review service, API, UI, MCP and memory modules use `portalocker` locks, `pathlib` and explicit UTF-8, and include portability tests. This review executed Linux tests only. The historical v1 agent (`src/watchlist/agent/`) still uses POSIX file locks and is **not** a Windows target.

## 3. Container (Linux)

| File | Purpose |
| --- | --- |
| `Dockerfile` | Image with core + `agent` + `api` + `ui` extras; no Torch, non-root user, offline at runtime, `HEALTHCHECK` on `/health` |
| `docker-compose.yml` | Services `api` and `ui`: workspace volume, API keys as a compose secret, read-only root filesystem, resource limits |
| `.dockerignore` | Allowlist: only build inputs and two fictional configs reach the build |

```sh
mkdir -p deploy/secrets
.venv/bin/python -B -m watchlist.api new-key --actor demo-reviewer --role reviewer
# Write only the "entry" value to deploy/secrets/api-keys.txt (git-ignored).
# Enter your private token in the UI sign-in form; do not export it to the server.
docker compose up --build        # or: podman compose up --build
# open http://127.0.0.1:8501
```

**Status:** in Step 29 the image was **not built or run**. Building it downloads the base images and the locked wheels, and that network access was not approved. No container machine was set up either. Until a recorded build and smoke run exist, treat these files as unverified.

## 4. What this guide does not reproduce

- **Real-data results.** These need the reviewed Step 08 corpus and human labels, which are BLOCKED. All evaluation outputs here are software-mechanics evidence on fictional, authored or generated data.
- **Model-dependent runs.** The E5 and MiniLM runs need already-local, approved weights; `make test-local-models` opts into them. Nothing downloads weights.
- **Live LLM calls.** These need an approved provider account and key. The LLM harness runs with recorded or fake replies only.
- **Historical benchmark totals.** The scoped reports have been revised to separate implemented mechanics from this review’s actual evidence. Unverified timings and totals were removed.
