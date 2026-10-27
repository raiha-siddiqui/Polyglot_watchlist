# Third-party notices (direct dependencies)

Generated for Completion Step 29 on 2026-10-27. **This is an inventory, not a legal review or licence clearance.**

- **Versions** come from `uv.lock`.
- **Licence values** were read from the installed package metadata on the Windows development machine:
  - `.venv` for the runtime, extras and `dev` group;
  - `.tools/qa` for the `qa` group.
- A value is taken from `License-Expression` where present, otherwise from `License`, otherwise from the `License ::` trove classifier. The table names which field it came from.
- Metadata can be incomplete or wrong. A value marked **unreviewed** has no usable metadata; nobody has checked its upstream licence text.
- Only **direct** dependencies declared in `pyproject.toml` are listed. Transitive dependencies (for example the NVIDIA wheels that Torch pulls in on Linux, and Streamlit's own dependencies) are not. `make inventory` reports every locked and installed package with its metadata.

**Not covered here:**
- **The project's own code licence.** It is undecided. There is no `LICENSE` file because the project owner has not chosen a licence; this is BLOCKED (see `docs/progress/completion-29.md`).
- **Data and model weights.** Upstream source data (OpenSanctions, OFAC, EU) and model weights (E5-small, MiniLM) have their own terms, recorded in `docs/source-permissions.md`, `docs/data-card.md` and `docs/model-card.md`. A model's licence does not grant rights to its training data or to the reference corpus.
- **Container base images.** The `Dockerfile` uses `python:3.14-slim` and `ghcr.io/astral-sh/uv:0.13.5`. Their contents were not inventoried, because the image was not built in this step.

| Declared in | Package | Locked version | Licence (installed metadata) | Metadata field |
| --- | --- | --- | --- | --- |
| core | rapidfuzz | 3.14.6 | MIT | License-Expression |
| extra `agent` | langgraph | 1.2.16 | MIT | License-Expression |
| extra `agent` | langgraph-checkpoint | 4.3.0 | MIT | License-Expression |
| extra `agent` | langgraph-checkpoint-sqlite | 3.1.2 | MIT | License-Expression |
| extra `agent` | portalocker | 4.4.1 | BSD-3-Clause | License-Expression |
| extra `embeddings` | numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | License-Expression |
| extra `embeddings` | sentence-transformers | 6.1.0 | Apache-2.0 | License-Expression |
| extra `embeddings` | torch | 2.14.1 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT | License-Expression |
| extra `preprocessing` | anyascii | 0.3.3 | ISC License (ISCL) | Classifier |
| extra `preprocessing` | jellyfish | 1.3.0 | MIT License | Classifier |
| extra `index` | usearch | 2.26.6 | Apache-2.0 | License |
| extra `api` | fastapi | 0.162.0 | MIT | License-Expression |
| extra `api` | httpx | 0.28.1 | BSD-3-Clause | License |
| extra `api` | uvicorn | 0.54.0 | BSD-3-Clause | License-Expression |
| extra `ui` | streamlit | 1.66.1 | Apache-2.0 | License-Expression |
| extra `mcp` | mcp | 2.3.0 | MIT | License |
| extra `llm` | anthropic | 2.3.0 | Apache-2.0 | License |
| group `dev`, build system | hatchling | 1.32.4 | MIT | License-Expression |
| group `dev` | pytest | 9.1.1 | MIT | License-Expression |
| group `qa` | ruff | 0.14.10 | MIT License | Classifier |
| group `qa` | pre-commit | 4.5.1 | MIT | License |
| group `qa` | detect-secrets | 1.5.0 | **unreviewed** (metadata says `UNKNOWN`) | License |

**What the container image contains.** It installs only core plus the `agent`, `api` and `ui` extras, together with their transitive dependencies. It contains:
- none of the `embeddings`, `preprocessing`, `index`, `mcp` or `llm` packages;
- none of the `dev` or `qa` tools.

The `anthropic` SDK is a dependency only. The project ships no API key and makes no live provider call by default.
