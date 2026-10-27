# Use an activated environment or pass PYTHON=/absolute/path/to/python.
# UV is an executable path, not a command with extra arguments.
PYTHON ?= python3
# Keep approved quality tools separate from the application environment.
QA_PYTHON ?= .tools/qa/bin/python
UV ?= uv
DIST_DIR ?= dist
QUALITY_FILES = \
	scripts/project_checks.py \
	tests/test_project_checks.py \
	tests/test_packaging.py \
	src/watchlist/ingest/source_registry.py \
	src/watchlist/ingest/acquire.py \
	src/watchlist/ingest/_parse_common.py \
	src/watchlist/ingest/parse_ftm.py \
	src/watchlist/ingest/parse_un.py \
	src/watchlist/ingest/parse_ofac.py \
	src/watchlist/ingest/parse_eu.py \
	src/watchlist/ingest/parse_wikidata.py \
	tests/test_source_acquisition.py \
	tests/test_source_adapters.py \
	src/watchlist/corpus.py \
	src/watchlist/label_review.py \
	src/watchlist/ingest/link_records.py \
	src/watchlist/ingest/full_acquire.py \
	src/watchlist/ingest/resource_guard.py \
	src/watchlist/ingest/wikidata_api.py \
	src/watchlist/ingest/metadata_preflight.py \
	tests/test_corpus.py \
	tests/test_label_review.py \
	tests/test_record_links.py \
	tests/test_full_acquisition.py \
	tests/test_resource_guard.py \
	tests/test_wikidata_api.py \
	tests/test_metadata_preflight.py \
	tests/conftest.py \
	tests/test_private_evidence.py \
	src/watchlist/baselines.py \
	src/watchlist/calibration.py \
	src/watchlist/evaluate_baselines.py \
	src/watchlist/evaluate_policy.py \
	src/watchlist/hybrid_retrieval.py \
	src/watchlist/llm/__init__.py \
	src/watchlist/llm/client.py \
	src/watchlist/llm/comparison.py \
	src/watchlist/llm/explanations.py \
	src/watchlist/llm/parsing.py \
	src/watchlist/llm/prompts.py \
	src/watchlist/llm/schemas.py \
	src/watchlist/llm/validation.py \
	src/watchlist/model_artifacts.py \
	src/watchlist/preprocessing.py \
	src/watchlist/reconciliation.py \
	src/watchlist/review/public_errors.py \
	src/watchlist/review/cli.py \
	src/watchlist/review/contracts.py \
	src/watchlist/review/locking.py \
	src/watchlist/review/nodes.py \
	src/watchlist/review/recovery.py \
	src/watchlist/review/service.py \
	src/watchlist/review/storage.py \
	src/watchlist/review/validation.py \
	src/watchlist/review/workflow.py \
	src/watchlist/review_policy.py \
	src/watchlist/structured_input.py \
	src/watchlist/synthetic_corpus.py \
	src/watchlist/training.py \
	src/watchlist/training_data.py \
	src/watchlist/training_losses.py \
	src/watchlist/vector_index.py \
	tests/review_v2_support.py \
	tests/test_baselines.py \
	tests/test_calibration.py \
	tests/test_explanation_grounding.py \
	tests/test_hybrid_retrieval.py \
	tests/test_llm_comparison.py \
	tests/test_llm_contracts.py \
	tests/test_model_artifacts.py \
	tests/test_preprocessing.py \
	tests/test_prompt_injection.py \
	tests/test_reconciliation.py \
	tests/test_review_policy.py \
	tests/test_review_v2_cli.py \
	tests/test_review_v2_concurrency.py \
	tests/test_review_v2_contracts.py \
	tests/test_review_v2_portability.py \
	tests/test_review_v2_recovery.py \
	tests/test_review_v2_service.py \
	tests/test_structured_input.py \
	tests/test_synthetic_corpus.py \
	tests/test_training.py \
	tests/test_training_data.py \
	tests/test_training_losses.py \
	tests/test_vector_index.py \
	scripts/eval_fictional.py \
	scripts/load_smoke.py \
	src/watchlist/api/__init__.py \
	src/watchlist/api/__main__.py \
	src/watchlist/api/app.py \
	src/watchlist/api/auth.py \
	src/watchlist/api/errors.py \
	src/watchlist/api/schemas.py \
	src/watchlist/api/settings.py \
	src/watchlist/case_memory/__init__.py \
	src/watchlist/case_memory/evaluation.py \
	src/watchlist/case_memory/identifiers.py \
	src/watchlist/case_memory/lifecycle.py \
	src/watchlist/case_memory/lookup.py \
	src/watchlist/case_memory/provider.py \
	src/watchlist/case_memory/publisher.py \
	src/watchlist/case_memory/state.py \
	src/watchlist/case_memory/store.py \
	src/watchlist/mcp_server/__init__.py \
	src/watchlist/mcp_server/__main__.py \
	src/watchlist/mcp_server/auth.py \
	src/watchlist/mcp_server/server.py \
	src/watchlist/mcp_server/tools.py \
	src/watchlist/observability.py \
	src/watchlist/review/backup.py \
	src/watchlist/review/retention.py \
	src/watchlist/stack.py \
	src/watchlist/ui/__init__.py \
	src/watchlist/ui/app.py \
	src/watchlist/ui/client.py \
	src/watchlist/ui/components.py \
	tests/test_api.py \
	tests/test_api_auth.py \
	tests/test_api_concurrency.py \
	tests/test_case_memory_evaluation.py \
	tests/test_case_memory_identifiers.py \
	tests/test_case_memory_lifecycle.py \
	tests/test_case_memory_provider.py \
	tests/test_case_memory_publisher.py \
	tests/test_case_memory_store.py \
	tests/test_case_memory_support.py \
	tests/test_integration_v2_stack.py \
	tests/test_load_failure.py \
	tests/test_mcp_server.py \
	tests/test_mcp_stdio.py \
	tests/test_mcp_tools.py \
	tests/test_observability.py \
	tests/test_review_backup.py \
	tests/test_review_retention.py \
	tests/test_security_hardening.py \
	tests/test_ui_app.py \
	tests/test_ui_client.py \
	tests/test_ui_components.py \
	tests/test_ui_support.py
export PYTHON QA_PYTHON UV

# These targets verify local inputs; they never acquire packages or models.
override PYTHONDONTWRITEBYTECODE := 1
override HF_HUB_OFFLINE := 1
override TRANSFORMERS_OFFLINE := 1
override HF_HUB_DISABLE_TELEMETRY := 1
override LANGSMITH_TRACING := false
override LANGCHAIN_TRACING_V2 := false
override UV_OFFLINE := 1
override UV_PYTHON_DOWNLOADS := never
override PIP_NO_INDEX := 1
override PIP_DISABLE_PIP_VERSION_CHECK := 1
export PYTHONDONTWRITEBYTECODE HF_HUB_OFFLINE TRANSFORMERS_OFFLINE HF_HUB_DISABLE_TELEMETRY
export LANGSMITH_TRACING LANGCHAIN_TRACING_V2 UV_OFFLINE UV_PYTHON_DOWNLOADS
export PIP_NO_INDEX PIP_DISABLE_PIP_VERSION_CHECK

# v2 stack (Completion Step 29). Fictional data only; servers bind loopback.
# Paths are examples: init targets refuse non-empty directories, and the
# eval/demo targets refuse an existing output directory.
WORKSPACE ?= data/review/v2-local
REVIEW_CONFIG ?= config/review-v2.json
MEMORY_STORE ?=
MEMORY_CONFIG ?= config/memory.json
WORKSPACE_ID ?= v2-local
API_HOST ?= 127.0.0.1
API_PORT ?= 8000
UI_PORT ?= 8501
MCP_ORIGIN ?= test
EVAL_OUTPUT ?=
EVAL_FLAGS ?=
DEMO_OUTPUT ?=
# Memory is opt-in: set MEMORY_STORE to attach the read-only memory provider.
MEMORY_ARGS = $(if $(MEMORY_STORE),--memory-store "$(abspath $(MEMORY_STORE))" --memory-config "$(abspath $(MEMORY_CONFIG))")
V2_TESTS = \
	tests/test_review_v2_contracts.py tests/test_review_v2_service.py \
	tests/test_review_v2_recovery.py tests/test_review_v2_concurrency.py \
	tests/test_review_v2_cli.py tests/test_review_v2_portability.py \
	tests/test_api.py tests/test_api_auth.py tests/test_api_concurrency.py \
	tests/test_ui_client.py tests/test_ui_components.py tests/test_ui_app.py \
	tests/test_mcp_tools.py tests/test_mcp_server.py tests/test_mcp_stdio.py \
	tests/test_case_memory_identifiers.py tests/test_case_memory_store.py \
	tests/test_case_memory_publisher.py tests/test_case_memory_provider.py \
	tests/test_case_memory_lifecycle.py tests/test_case_memory_evaluation.py \
	tests/test_review_backup.py tests/test_review_retention.py \
	tests/test_observability.py tests/test_security_hardening.py tests/test_load_failure.py \
	tests/test_integration_v2_stack.py \
	tests/test_preprocessing.py tests/test_structured_input.py \
	tests/test_baselines.py tests/test_hybrid_retrieval.py tests/test_vector_index.py \
	tests/test_synthetic_corpus.py tests/test_reconciliation.py \
	tests/test_llm_contracts.py tests/test_llm_comparison.py \
	tests/test_explanation_grounding.py tests/test_prompt_injection.py \
	tests/test_training.py tests/test_training_data.py tests/test_training_losses.py \
	tests/test_model_artifacts.py tests/test_calibration.py tests/test_review_policy.py

.PHONY: help test test-local-models lock-check lint build package-smoke eval-smoke secret-scan inventory source-preflight source-tests
.PHONY: init-v2 memory-init memory-publish serve-api serve-ui serve-mcp test-v2 eval-fictional demo

help:
	@printf '%s\n' \
	  'Select an installed environment: make PYTHON=.venv/bin/python <target>' \
	  'test           Run all offline tests except explicitly opt-in model tests.' \
	  'test-local-models  Opt in to three already-local model tests with isolated caches.' \
	  'lock-check     Check lock consistency offline without installing anything.' \
	  'lint           Check the reviewed Ruff scope without changing files.' \
	  'build          Build wheel and source archive into a new DIST_DIR (default: dist).' \
	  'package-smoke  Check packaging and installation outside this checkout.' \
	  'eval-smoke     Run deterministic current-contract fictional evaluation.' \
	  'secret-scan    Scan the declared publication scope; requires detect-secrets.' \
	  'inventory      Report lock and installed dependency/license metadata.' \
	  'source-preflight  Inspect reviewed source policies offline; no download.' \
	  'source-tests   Run fictional acquisition and adapter boundary tests.' \
	  'init-v2        Create a new v2 review workspace at WORKSPACE.' \
	  'memory-init    Create a new reviewer-memory store at MEMORY_STORE.' \
	  'memory-publish Publish completed cases of WORKSPACE into MEMORY_STORE.' \
	  'serve-api      Serve the review API on loopback (keys from WATCHLIST_API_KEYS[_FILE]).' \
	  'serve-ui       Serve the Streamlit reviewer UI on loopback (per-session key sign-in).' \
	  'serve-mcp      Serve the MCP tools over stdio for a local MCP client.' \
	  'test-v2        Run the v2 stack and completion-step test modules.' \
	  'eval-fictional Rerun the fictional evaluations into a new EVAL_OUTPUT (default: data/eval-fictional/run-<UTC>).' \
	  'demo           Run the fictional API + memory + MCP walkthrough into a new DEMO_OUTPUT.' \
	  'Overrides: PYTHON=<executable> QA_PYTHON=<executable> UV=<executable> DIST_DIR=<new-directory>' \
	  'v2 overrides: WORKSPACE REVIEW_CONFIG MEMORY_STORE MEMORY_CONFIG WORKSPACE_ID API_PORT UI_PORT MCP_ORIGIN EVAL_FLAGS=--skip-torch'

test:
	"$(PYTHON)" -B -m pytest -q -m 'not integration' -p no:cacheprovider

test-local-models:
	"$(PYTHON)" -B scripts/project_checks.py local-model-tests

lock-check:
	"$(UV)" lock --check --offline

lint:
	"$(QA_PYTHON)" -I -B -m ruff check --config pyproject.toml \
		$(QUALITY_FILES)
	"$(QA_PYTHON)" -I -B -m ruff format --check --config pyproject.toml \
		$(QUALITY_FILES)

build:
	@test ! -e "$(DIST_DIR)" && test ! -L "$(DIST_DIR)" || { printf '%s\n' 'Build output exists; choose a new DIST_DIR to preserve it.' >&2; exit 1; }
	"$(UV)" build --offline --no-build-isolation --python "$(PYTHON)" --out-dir "$(DIST_DIR)"

package-smoke:
	UV="$(UV)" "$(PYTHON)" -B -m pytest -q tests/test_packaging.py -p no:cacheprovider

eval-smoke:
	"$(PYTHON)" -B scripts/project_checks.py eval-smoke

secret-scan:
	"$(QA_PYTHON)" -I -B scripts/project_checks.py secret-scan

inventory:
	"$(PYTHON)" -B scripts/project_checks.py inventory

source-preflight:
	"$(PYTHON)" -B -m watchlist.ingest.acquire preflight --profile feasibility --registry config/data-sources.json

source-tests:
	"$(PYTHON)" -B -m pytest -q tests/test_source_acquisition.py tests/test_source_adapters.py tests/test_corpus.py tests/test_label_review.py tests/test_record_links.py tests/test_full_acquisition.py tests/test_resource_guard.py tests/test_wikidata_api.py tests/test_metadata_preflight.py -p no:cacheprovider

# -- v2 stack (Completion Step 29) ------------------------------------------------
# Windows without make: run the same commands with .venv/Scripts/python.exe;
# see docs/reproduction.md.

init-v2:
	"$(PYTHON)" -B -m watchlist.review v2 init --workspace "$(WORKSPACE)" --config "$(REVIEW_CONFIG)"

memory-init:
	@test -n "$(MEMORY_STORE)" || { printf '%s\n' 'Set MEMORY_STORE=<new directory>.' >&2; exit 2; }
	"$(PYTHON)" -B -m watchlist.stack memory-init --store "$(MEMORY_STORE)" --memory-config "$(MEMORY_CONFIG)"

memory-publish:
	@test -n "$(MEMORY_STORE)" || { printf '%s\n' 'Set MEMORY_STORE=<existing memory store>.' >&2; exit 2; }
	"$(PYTHON)" -B -m watchlist.stack memory-publish --store "$(MEMORY_STORE)" --memory-config "$(MEMORY_CONFIG)" \
		--workspace "$(WORKSPACE)" --config "$(REVIEW_CONFIG)" --workspace-id "$(WORKSPACE_ID)"

serve-api:
	"$(PYTHON)" -B -m watchlist.api serve --workspace "$(WORKSPACE)" --config "$(REVIEW_CONFIG)" \
		--host "$(API_HOST)" --port "$(API_PORT)" $(MEMORY_ARGS)

serve-ui:
	WATCHLIST_API_URL="$${WATCHLIST_API_URL:-http://$(API_HOST):$(API_PORT)}" \
		"$(PYTHON)" -B -m streamlit run src/watchlist/ui/app.py --server.address 127.0.0.1 \
		--server.port "$(UI_PORT)" --server.headless true --browser.gatherUsageStats false \
		--server.allowedHosts localhost --server.allowedHosts 127.0.0.1

serve-mcp:
	"$(PYTHON)" -B -m watchlist.mcp_server --workspace "$(abspath $(WORKSPACE))" \
		--config "$(abspath $(REVIEW_CONFIG))" --origin "$(MCP_ORIGIN)" $(MEMORY_ARGS)

test-v2:
	"$(PYTHON)" -B -m pytest -q -p no:cacheprovider $(V2_TESTS)

eval-fictional:
	"$(PYTHON)" -B scripts/eval_fictional.py $(if $(EVAL_OUTPUT),--output-root "$(EVAL_OUTPUT)") $(EVAL_FLAGS)

demo:
	"$(PYTHON)" -B -m watchlist.stack demo $(if $(DEMO_OUTPUT),--output-root "$(DEMO_OUTPUT)")

