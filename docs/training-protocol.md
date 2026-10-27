# Training protocol (Completion Step 13 mechanics)

Status: **mechanics implemented and verified on fictional generated data only.** No pretrained model has been fine-tuned. Steps 14–16 (real bi-encoder and cross-encoder fine-tuning and pipeline selection) remain **blocked** on the reviewed Step 08 corpus and a separate full-run approval. Nothing in this document is a performance claim.

## What, why, when, where, who, how

- **What:** a bounded CPU training path (`src/watchlist/training.py`) with loss functions (`training_losses.py`), training-only pair construction (`training_data.py`) and hash-bound derived artifacts (`model_artifacts.py`).
- **Why:** the existing E5 `.encode()` NumPy path is inference-only. Before spending a real training budget we must prove that weights change, losses move in the right direction, checkpoints resume bit-for-bit, held-out groups cannot leak, and original weights cannot be overwritten.
- **When:** now, on fictional data. The real runs happen only after Step 08 delivers reviewed groups and the Step 14/15 budgets are approved.
- **Where:** generated artifacts live only in gitignored directories: `data/models/derived-pilot/` and `data/eval-comparisons/completion-13-17/`.
- **Who:** the ML owner runs and reviews; a human approves any real fine-tuning.
- **How:** explicit configs (`eval/configs/train-pilot-fictional.json`), deterministic seeds, explicit caps and refusal-first error handling.

## Data rules

1. Pairs are built only from groups whose partition is `train` (`watchlist.splits.validate_assignments` checks coverage and one partition per group). Both endpoints of every pair must be training groups; `_check_endpoint` re-checks every final pair.
2. Optional `forbidden_entity_ids` (planted held-out IDs) are defence in depth. If one of them is labelled `train`, the split is contradictory and construction stops.
3. Positives are capped per group (`max_positives_per_group`) with hash-ordered selection, so large groups do not dominate.
4. Hard negatives are the top RapidFuzz `token_sort_ratio` aliases from **other training groups** (miner `rapidfuzz-token-sort-train-only-v1`, with rank receipts). Easy negatives are hash-selected other training aliases. No development, calibration, threshold-selection or test alias is ever a mining candidate.
5. The manifest digest covers settings, train IDs/groups, source fingerprint and pair fingerprint, in canonical sorted order. Reordering entities or aliases does not change it; editing it is detected by `verify_manifest`.
6. Contrastive batches are group-aware: no two pairs from one group share a batch, and the MNRL loss additionally masks any same-group column as a possible false negative.

The fictional generator (`generate_fictional_entities`) invents syllable-based names and aliases (initial form, token reorder, adjacent-letter typo and, for even entities, a naive Cyrillic rendering). Every record is `synthetic: true`. The generated corpus is partitioned with the production group-hash splitter (`fictional_assignments`).

## Objectives

- **Bi-encoder:** in-batch MultipleNegativesRanking loss, scale 20 by default, optional hard-negative columns, same-group masking.
- **Cross-encoder/pair scorer:** `BCEWithLogitsLoss` on one raw logit per pair. A sigmoid is never applied before the loss.
- Both raise `NonFiniteLossError` on NaN/inf inputs or losses; `check_finite_gradients` refuses non-finite gradients before an optimizer step.

## Run controls

| Control | Mechanism |
| --- | --- |
| Step cap | `max_steps`; pilot uses 200 |
| Wall-clock cap | `max_wall_seconds`, accumulated across resumes |
| Memory cap | Process working set (Windows `GetProcessMemoryInfo`, Linux `/proc/self/statm`) checked before each step; preflight estimates weights + gradients + Adam moments |
| Non-finite values | Run stops with `stopped_nonfinite`; the bad update never reaches Adam |
| Seed control | `build_model` seeds initialisation; batch order is a pure function of seed and step |
| Determinism | Fixed thread count and `torch.use_deterministic_algorithms(True)` inside `torch_runtime`; previous globals are restored |
| Checkpoints | New `step-NNNNNN.pt`, then atomic `latest.json` pointer with SHA-256; old step files removed afterwards |
| Resume | Requires identical recipe digest (all settings except stop caps, plus the starting artifact's manifest digest when `init_artifact` is used) and data digest; loads with `torch.load(weights_only=True)` |
| Protected weights | Refuses output under configured protected roots, the pretrained route's own `model_path`, any directory holding `acquisition.json` (pretrained receipts) or a sealed artifact manifest |
| Artifacts | `weights.pt` plus `manifest.json` (written last, atomically) with file hashes, config, data digest, metrics, code and environment fingerprints and `is_pretrained_original: false`; verified before every load. The manifest must list at least one payload, every payload name must be a plain relative path (no absolute, drive-letter, UNC or `..` names), and `weights.pt` is loaded only when the manifest lists it |
| Lineage | `derived_from.base` is `random-init`, `pretrained` (model ID, revision, local path and a digest of the verified receipt's file hashes) or `derived-artifact` (the starting artifact's manifest digest, path and its own `derived_from`) |

The manifest self-digest detects accidental corruption and careless edits. It is **not a signature**: anyone who can write the artifact directory can reseal an altered manifest. Protecting artifacts against deliberate tampering needs access control or signing outside this module.

## Models

- `CharNgramBiEncoder` (pilot): hashed boundary-marked character 2–4-grams (CRC32 buckets), mean-pooled `EmbeddingBag`, linear projection, L2 normalisation. Random initialisation; nothing is downloaded.
- `CharNgramPairScorer`: the same encoder plus a small head emitting one raw logit, used to exercise the BCE path.
- `SentenceTransformerBiEncoder` (`architecture: sentence-transformers-e5`, objective `mnrl`): lazily imported adapter for a verified local E5 copy. `watchlist.embeddings.validate_config` checks the protocol section when the config loads and `verify_model` checks every file against the receipt before loading. Like `E5Encoder`, it fixes `max_seq_length` to `max_tokens` and raises `TruncationError` instead of truncating a name.
- `MiniLMCrossEncoder` (`architecture: cross-encoder-minilm`, objective `bce`): lazily imported adapter for a verified local mMiniLM reranker (`watchlist.reranking.validate_config`/`verify_model`). It returns one raw logit per pair and refuses truncation.
- Pretrained routes take the approved protocol section under `model.pretrained`, for example the `model` object of `eval/configs/embeddings.json` or `reranking.json`. In-repo architectures must not carry one. Both routes are tested only with in-memory fakes. They are **not exercised on real weights** because no local E5/MiniLM weights exist in this checkout, and a real run needs separate approval.
- Known blocker: `watchlist.reranking` imports the POSIX-only `resource` module at import time, so on Windows the cross-encoder route refuses with `cross-encoder route unavailable` until that module guards the import.

## Commands

```sh
python -m watchlist.training preflight --config eval/configs/train-pilot-fictional.json
python -m watchlist.training pilot --config eval/configs/train-pilot-fictional.json
```

The pilot refuses to rerun into an existing run/artifact directory; remove or rename the generated directory deliberately first.

## Claim boundaries

- A falling training loss on fictional names proves mechanics, not useful multilingual matching.
- Fictional development recall is regression evidence about this generator, not an estimate for real names.
- The pilot's random-initialised char-n-gram model is not E5 and not MiniLM; its manifest records `base: random-init`, `pretrained_weights: null`.
- Real resource estimates for E5/MiniLM must come from a measured Step 13 smoke run on those models, which is blocked until the weights are present and the run is approved.
