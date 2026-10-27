# Polyglot Watchlist — Codex Collaboration Rules

## Source of truth
Read `docs/reference/internship-2093-portfolio-plan.html`, primarily Project 1 (Polyglot Watchlist Agent), plus its engineering standards and AI coding-agent guidance. Project 2 (Follow the Money) is maintained in its own repository; this repository serves it only through the public v2 review service, which it may call to resolve sanctioned-wallet owner names. Treat the reference as a project proposal, not as proof that tools, datasets, deadlines, model claims, or licenses are current.

## Objective
Build a research-only multilingual entity/name matching prototype using approved public sanctions data (start with fictional synthetic data). It assists human review; it does not establish criminality or identity and is not a live screening/compliance service.

## Teaching and working style
- Act as a patient senior engineer and ML mentor to a learner. Explain terminology in simple English and use 5W1H (what, why, when, where, who, how) whenever introducing unfamiliar ideas.
- BEFORE every phase: state its goal, why it matters, exact files to create/edit, dependencies, expected commands, tests, risks, and acceptance criteria. Wait for the user's explicit approval before implementing.
- DURING an approved phase: explain meaningful actions in concise messages before running them; say which file/command and why. Do not claim actions or test results you have not completed.
- AFTER every phase: report what changed, file-by-file explanations, line/function explanations for new code, commands actually run, actual test results, example input/output, bugs/limitations, 5W1H recap, and what the next phase will contain. Save a report in `docs/progress/phase-NN.md` and STOP until user approves the next phase.
- Use small, independently testable changes. Do not start later phases automatically; no sweeping scaffolding, refactors or added libraries without rationale and permission.
- Ask before downloading large datasets/models, incurring costs, using API keys, network access, modifying unrelated files, pushing to GitHub, or exposing any service publicly.
- Never hardcode secrets. Favor local deterministic tests. Show uncertainties; do not fabricate results or scores.

## Phase order
Follow the list below and the completion sequence in `docs/project-completion-plan.md`.

0. Read reference; propose one-page design, precise scope, DAG, evaluation plan, phased tasks and acceptance criteria. No application code until approved.
1. Minimal synthetic entities and aliases; Python normalization; simple RapidFuzz baseline; unit tests and sample CLI.
2. Ingest a licensed/permitted subset of public data; SQLite schema with source provenance; entity-level split and deduplication.
3. Baseline evaluations before training: Recall@10/@50, relevant false-positive measures, stratified perturbations and leakage checks.
4. Off-the-shelf multilingual embedding retrieval; compare with RapidFuzz.
5. Optional fine-tuning of bi-encoder and cross-encoder after data/eval review.
6. Calibration and abstention thresholds with independent calibration data and conservative reporting.
7. LangGraph StateGraph orchestration, conditional branches, checkpointing and human review.
8. Long-term reviewer-decision memory, evidence-based explanations, audit log and security tests.
9. FastAPI and Streamlit UI; optional MCP interface and Microsoft 365 integration only if separately approved.
10. Reproducible evaluation, error analysis, documentation and demo.

## Evaluation and safety
- No entity leakage between training, calibration and test; note duplicates across sources.
- Do not interpret model similarity score as probability until properly calibrated; don't invent operational guarantees.
- Use synthetic/fictitious names for demos and initial tests; avoid private personal data or scraping individuals.
- Restrict use of OpenSanctions and other data to terms allowed by their current licenses; verify before use.
- Present outputs as possible record matches for research/analyst review, not findings of wrongdoing.
- Keep LLM untrusted data from controlling final decisions; ensure prompt-injection tests and provenance for evidence.

## Coding quality
Use a Python package under `src/watchlist/`, type hints, docstrings for nontrivial functions, pytest, `README.md`, `.gitignore`, pinned or managed dependencies with `uv` if suitable, and meaningful small commits only when approved. Prefer clarity over sophistication.
