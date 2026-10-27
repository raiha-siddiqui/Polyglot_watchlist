# Completion Steps 11–12 — baseline matrix and hybrid top-50 retrieval (mechanics on fictional data)

Date: 2026-10-26. **Status: mechanics implemented and exercised on fictional/synthetic data only. Neither step is complete against its real-data acceptance criteria.** Every number below comes from commands actually run in this checkout and is fictional regression/mechanics evidence. No number here is evidence about real multilingual name matching, about any real person or organisation, or about sanctions status. All outputs are possible-record suggestions for human review; no arm makes a match/clear decision.

## Goal

- **Step 11 (baseline matrix):** run every available lexical/phonetic baseline through one retriever contract on the same validated benchmarks and the existing `retrieval-metrics-v2` contract, report unsupported cases explicitly, and record concrete blockers for baselines that could not run.
- **Step 12 (hybrid retrieval):** fuse lexical, phonetic and dense candidate lists into a top-50 shortlist with one row per entity and per-arm evidence, over a real vector index (exact NumPy oracle plus usearch HNSW) with build/save/load/verify/activate and measured approximate-vs-exact recall and latency.

## What “fictional mechanics” means here

The 12-record fictional fixture and the 10-pair multilingual fixture are already exposed, so they are regression-only. The new `synthetic-scale` dataset is generated from invented syllables; its Cyrillic/Arabic/Han renderings are a private convention of the generator (not a transliteration standard), so cross-script “success” on it only shows that the code paths work. The dense arm uses a deterministic hashed character n-gram encoder, which is a surface-overlap feature map, **not a language model**. Real conclusions need the reviewed corpus from Step 08 and approved pretrained models.

## Files (all new; no shared file edited)

| File | Purpose and key functions |
| --- | --- |
| `src/watchlist/baselines.py` | Common retriever contract. `Retriever` protocol (`arm`, `scorer_name`, `coverage`, `retrieve(query, top_k) -> RetrievalResult`). `RetrievalResult(candidates, unsupported_reason, component_status)`. `Coverage` lists aliases each arm can score and the reasons for the rest. `AliasScoringRetriever` precomputes alias representations once, scores them per query in one RapidFuzz `process.extract` batch, keeps the best alias per entity (ties by alias ID) and orders entities by (−score, entity ID). `build_retriever(arm, entities)` over `ARMS`: `exact_normalized` (returns only exact normalized matches), `levenshtein` (byte-for-byte the existing `rank_candidates` behaviour; tested), `jaro_winkler`, `token_sort`, `token_set`, `phonetic_metaphone`, `phonetic_nysiis`, `translit_levenshtein`. `phonetic_code` romanizes Cyrillic with anyascii and raises `UnsupportedText` for Han, Arabic or unknown scripts; `transliterated` is anyascii + casefold, and raises `UnsupportedText` for text without recognized letters or with non-ASCII symbols (anyascii would emit `:grinning:`-style tokens). `phonetic_code` normalizes first, so blank queries raise `ValueError` in every arm. jellyfish/anyascii import lazily. |
| `src/watchlist/evaluate_baselines.py` | CLI. `validate_config` checks the full schema up front (required/unknown top-level keys, `synthetic_scale`, `dense_encoder`, every hybrid-variant key, HNSW parameter names, `not_run`); `prepare_run` validates schema, test-partition seal and destinations before any dataset runs; `check_sealed` refuses the `test` partition unless `--unseal-test` and a `--freeze-receipt` whose `config_fingerprint` (everything except `partitions`) matches are both supplied (`make_freeze_receipt` builds one); `run`; `load_dataset` (fictional and multilingual reuse `compare_retrieval.current_*_benchmark`, imported lazily; `synthetic-scale` uses `synthetic_benchmark` → existing `build_benchmark`); `make_arm`; `evaluate_arm` (one untimed warm call, timed repetitions that must reproduce it, labels applied afterwards via `query_labels`/`ranking_fields`, aggregates via `evaluate.aggregate`, unsupported queries counted per partition/suite/script, missing targets per partition/suite, alias coverage, build seconds); `evaluate_dataset` (partition filter, `audit_queries`); `render_report`; `_destinations` refuses historical trees and existing destinations. JSON keeps a 5-candidate preview per query; metrics were computed on full returned lists before compaction. |
| `src/watchlist/vector_index.py` | `ExactIndex` (full cosine scan, ties by ID; the oracle), `HNSWIndex` (usearch, lazy import, single-threaded insertion so builds are byte-reproducible; missing usearch raises `RuntimeError` naming “no fallback”). `build_index` rejects unknown backends. `save_index` writes into `<dest>.staging` (graph bytes via `Index.save()` + `write_bytes`, so non-ASCII Windows paths work), writes the manifest last, then renames; existing destination or leftover staging (interrupted build) is refused, while an ordinary exception removes the staging directory it created. `load_index` verifies file SHA-256s, shapes, `content_digest` (IDs + float32 bytes) and an optional expected digest (stale index error); for HNSW it loads the graph from a buffer and `_verify_graph` checks keys 0..n−1 and that the stored vectors equal `vectors.npy`, so a swapped graph with a re-hashed manifest is rejected. `activate_index`/`load_active` maintain an atomically replaced `ACTIVE.json` that also pins the manifest SHA-256; versions must be a single directory name directly under the root (`../x/v1` etc. are refused). `measure_recall` gives approximate-vs-exact recall@k, latency and memory. `HashedCharNgramEncoder` (BLAKE2b signed feature hashing). CLI `build` / `verify`. |
| `src/watchlist/hybrid_retrieval.py` | `DenseRetriever` (alias vectors in an index, collapse to best alias per entity; exact backend scans all rows, HNSW backend fetches progressively more rows). `fuse` implements weighted RRF `Σ w/(rrf_k + rank)` in fixed component order, rejects component lists that repeat an entity, ties by entity ID, and records `component_status` (an unsupported component is reported, not hidden). `HybridCandidate` keeps `fused_score` plus `ArmEvidence(component, arm, scorer_name, rank, raw_score, matched alias)`; `similarity_score` is only an ordering alias for the metric contract. `HybridConfig` validates weights/rrf_k/depth; zero-weight components are not queried. `build_hybrid(..., groups=None)`. With an explicit entity→group link map (validated by `check_groups`), `fuse(..., groups)` ranks every component list over distinct groups *before* the per-arm depth cut and the top_k cut, and `HybridRetriever` queries components progressively deeper until they yield `per_arm_depth` distinct groups, so linked duplicates never consume shortlist or depth slots; rows carry `group_id`, `member_ids`, and evidence names the member each arm ranked (`ArmEvidence.entity_id`). `HybridCandidate.provenance` copies the record provenance. `HybridConfig.from_dict` is strict (unknown keys, bool/string weights are errors). CLI `search` and `evaluate` (dense + hybrid arms of a baselines config, same sealing rules). |
| `src/watchlist/synthetic_corpus.py` | `generate_entities(count, seed)`, `generate_absent`, `generate_corpus` (partitions via existing `splits.assign_groups`), `corpus_digest`. Invented syllables in four scripts, seeded Latin spelling variants, seeded alias-ID order so the withheld alias varies across scripts, disjoint syllables for absent names. |
| `eval/configs/baselines-v2.json` | Datasets, development-only partition, K = 50, 2 timing repetitions, 12 arms, synthetic size 400 + 40 absent, encoder, three predeclared hybrid variants (equal weights, rrf_k 60; depth 50 exact, depth 10 exact, depth 50 HNSW) and the `not_run` blocker list. |
| `config/vector-index.json` | 5,000-entity synthetic corpus, 256-d hashed encoder, HNSW M = 16 / ef_add 128 / ef_search 64, recall at K 10 and 50 on 500 perturbed queries. |
| `tests/test_baselines.py`, `tests/test_hybrid_retrieval.py`, `tests/test_vector_index.py`, `tests/test_synthetic_corpus.py` | 87 tests (after the review fixes below): Levenshtein equivalence to `rank_candidates`, hand-checked scores and RRF arithmetic, ID tie-breaking, explicit unsupported Han/Arabic phonetic cases, evaluate-arm metric arithmetic, evidence-outside-view rejection, destination refusal, HNSW recall vs exact, reproducible HNSW bytes, save/load/tamper/stale/missing/interrupted/activation, missing-usearch error, top-50 on a 150-entity corpus with one row per entity, top-10 depth sensitivity, equivalent-record collapse, corpus determinism and benchmark compatibility. Tests use the hashed encoder, never a downloaded model. |

## Commands actually run and results

```sh
# tests (Windows venv, offline flags)
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider \
  tests/test_baselines.py tests/test_hybrid_retrieval.py tests/test_vector_index.py tests/test_synthetic_corpus.py
# -> before review fixes: 48 passed in 24.64s
# -> after review fixes: 87 passed in 26.88s
# with tests/test_matching.py tests/test_evaluation_metrics.py added -> 160 passed in 25.62s
.tools/qa/Scripts/python.exe -I -B -m ruff check --isolated --target-version py314 --select E4,E7,E9,F --line-length 120 <the 9 new .py files>
# -> All checks passed!  (re-run after review fixes)

# baseline matrix (first attempt failed, see limitations; second run used UTF-8 mode)
PYTHONUTF8=1 PYTHONPATH=src HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -B -m watchlist.evaluate_baselines \
  --config eval/configs/baselines-v2.json --output-root data/eval-comparisons/completion-11-12/baselines-v2-development-run2
# -> 3 datasets saved; wall time 15m01s on a machine shared with other concurrent jobs

PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -B -m watchlist.vector_index build \
  --config config/vector-index.json --output data/eval-comparisons/completion-11-12/vector-index-5000
# -> 16,840 aliases; wall time 1m07s
PYTHONUTF8=1 PYTHONPATH=src .venv/Scripts/python.exe -B -m watchlist.vector_index verify \
  --index data/eval-comparisons/completion-11-12/vector-index-5000/hnsw
# -> {"backend": "hnsw", "count": 16840, "dimension": 256, "content_digest": "8c628bb0…d055f2"}

# after review fixes: new hybrid evaluate command (dense + hybrid arms only), fresh output root
PYTHONUTF8=1 PYTHONPATH=src HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv/Scripts/python.exe -B -m watchlist.hybrid_retrieval evaluate   --config eval/configs/baselines-v2.json --output-root data/eval-comparisons/completion-11-12/hybrid-evaluate-run3 --partition development
# -> 3 datasets saved, exit 0. Synthetic-scale challenge metrics identical to run2 (same query fingerprint d6a1928cc28a…):
#    hybrid_rrf 538/725, 592/725, 698/725, MRR 0.772607; hybrid_rrf_depth10 540/725, 694/725, 698/725;
#    hybrid_rrf_hnsw 538/725, 592/725, 698/725, MRR 0.772609; char_ngram_cosine 657/725, 675/725, 677/725.
# sealing check (both CLIs): --partition test without --unseal-test/--freeze-receipt -> exit 2,
#    "the test partition is sealed; it needs --unseal-test and --freeze-receipt"; nothing written.
```

Generated files (gitignored): `data/eval-comparisons/completion-11-12/{baselines-v2-development-run2/{fictional,multilingual,synthetic-scale}/{results.json,report.md}, run2.log, vector-index-5000/{exact,hnsw,ACTIVE.json,receipt.json}, vector-index-build.log, run1-failed-cp1252.log, hybrid-evaluate-run3/{fictional,multilingual,synthetic-scale}/{results.json,report.md}, hybrid-evaluate-run3.log}`. The partial `baselines-v2-development/fictional` from the failed first run is kept as evidence, not overwritten.

### Synthetic-scale development results (fictional mechanics only)

400 generated entities, 1,346 aliases (601 Latin, 232 Cyrillic, 256 Arabic, 257 Han), 1,117 development queries, K = 50. Challenge suite (perturbations and withheld aliases), 725 positives:

| Arm | R@1 | R@10 | R@50 | MRR@50 | Unsupported queries | Median ms |
|---|---|---|---|---|---:|---:|
| exact_normalized | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0 | 0.240 |
| levenshtein | 0.7145 (518/725) | 0.7876 | 0.8290 | 0.7432 | 0 | 9.847 |
| jaro_winkler | 0.6593 | 0.7531 | 0.8069 | 0.6882 | 0 | 9.819 |
| token_sort | 0.8690 | 0.9186 | 0.9283 | 0.8897 | 0 | 13.194 |
| token_set | 0.9241 | 0.9297 | 0.9297 | 0.9269 | 0 | 21.344 |
| phonetic_metaphone | 0.4786 | 0.5697 | 0.6055 | 0.5115 | 212 | 2.158 |
| phonetic_nysiis | 0.4510 | 0.5628 | 0.6028 | 0.4855 | 212 | 2.207 |
| translit_levenshtein | 0.7710 | 0.8593 | 0.8910 | 0.8027 | 0 | 8.149 |
| char_ngram_cosine (hashed, exact) | 0.9062 | 0.9310 | 0.9338 | 0.9165 | 0 | 11.365 |
| hybrid_rrf (depth 50, exact) | 0.7421 | 0.8166 | 0.9628 (698/725) | 0.7726 | 0 | 33.893 |
| hybrid_rrf_depth10 | 0.7448 | 0.9572 | 0.9628 | 0.7956 | 0 | 32.078 |
| hybrid_rrf_hnsw (depth 50) | 0.7421 | 0.8166 | 0.9628 | 0.7726 | 0 | 35.904 |

Hand check: 518/725 = 0.71448. Sanity suite (exact/formatting, 368 positives): every arm 1.0 except both phonetic arms at 240/368 = 0.6522 because 128 Arabic/Han sanity queries are explicitly unsupported (counted as misses on the shared denominator). Absent-target suggestion rate: exact arm 0/20, every ranking arm 20/20 — expected, because there is no threshold; this is not a false-positive rate. Phonetic arms index 833/1,346 full-view aliases. Per-script Recall@10 (challenge) for phonetic arms is 0/133 Arabic and 0/79 Han by construction; `translit_levenshtein` reaches 79/79 Han only because the generator's Han renderings are built from the same syllables.

Observations, all limited to this synthetic setting: the depth-50 equal-weight RRF has the best Recall@50 (0.9628) but worse R@1/R@10 than `token_set` or the hashed n-gram arm, because deep tails of weaker arms promote entities that appear moderately in several lists; restricting each arm to depth 10 raised R@10 from 0.8166 to 0.9572 with the same R@50. HNSW and exact dense backends gave identical hybrid recall at 400 entities (MRR differs in the sixth decimal). None of these settings were tuned; the variants are predeclared, and selecting one would be a development decision for the real corpus.

Fictional (12 entities, 18 challenge positives) and multilingual (10 entities, 10 challenge positives) fixture tables are in the reports. Recall@50 is N/A there (index ≤ 50); multilingual Recall@10 is also N/A (index = 10). They are regression checks only.

### Vector index (5,000 synthetic entities, 16,840 alias vectors, 256-d hashed encoder)

| K | Mean recall vs exact | Queries < 1.0 | HNSW median / p95 ms | Exact median / p95 ms |
|---|---|---:|---|---|
| 10 | 0.9788 | 78/500 | 2.488 / 5.416 | 10.221 / 23.753 |
| 50 | 0.9546 | 342/500 | 4.134 / 7.950 | 11.140 / 22.034 |

Build: exact 0.13 s, HNSW 34.7 s (single-threaded). Memory: HNSW 33,693,312 bytes (usearch report), exact vectors 17,244,160 bytes. Minimum recall is 0.0: diagnosis found one query (`Сикирло Знвовен Маво`, a perturbed fictional Cyrillic alias) where HNSW returned a disjoint neighbourhood (best exact cosine 0.913 vs HNSW top 0.303) — a genuine approximate-search miss at ef_search 64, kept visible rather than hidden.

## Example input/output

Run: `PYTHONPATH=src .venv/Scripts/python.exe -B -m watchlist.hybrid_retrieval search --query "Lidem Vensadem Kirzensa" --top-k 2` (300 generated entities, seed 42). Output JSON: each candidate has `entity_id`, `matched_alias_id`/`matched_alias` (original text), `fused_score` (e.g. `0.04918…` = 3 × 1/61 when all three arms rank the entity first), `fusion: weighted-rrf-v1`, and `evidence` with one entry per arm (`component`, `arm`, `scorer_name`, `rank`, `raw_score` such as cosine `1.0` or Levenshtein `100.0`). The top candidate is `synx-00001`, alias `synx-00001-a1`, with `fused_score` 0.04918 and all three arms at rank 1. The Han query `里德·文莎德` returns `component_status.phonetic = "phonetic code undefined for script HAN"`, while the dense and lexical arms still contribute.

## Limitations

- Fictional/synthetic only; exposed fixtures; generated cross-script renderings follow a toy convention. No real-data or population claim; no confidence intervals.
- The dense arm is a hashed character n-gram encoder, not E5/LaBSE. It cannot capture translation.
- Phonetic arms are Metaphone/NYSIIS on anyascii-romanized Latin/Cyrillic tokens, compared by Levenshtein on codes. Double Metaphone is **not** implemented (jellyfish lacks it).
- RRF weights, rrf_k and depths are untuned predeclared defaults. Fused values are rank combinations, never similarities or probabilities.
- Latency was measured on a machine running other engineers' jobs concurrently; treat it as indicative only. Timing repetitions were 2.
- `results.json` stores a 5-candidate preview per query to bound size (synthetic file is 43.6 MB); metrics were computed from full lists.
- The first baseline run failed: `compare_retrieval._multilingual_inputs` reads `eval/fixtures/multilingual_entities.json` with `Path.read_text()` and no encoding, which uses cp1252 on Windows. The second run used `PYTHONUTF8=1`. That file is outside this step's ownership; see the requested change below.
- Recall vs exact counts ID-tie-equivalent answers at the cutoff as misses (conservative). HNSW parameters were not tuned.
- Group-aware fusion is tested on hand examples only. The evaluation CLIs remain entity-level because the fictional benchmarks have no explicit equivalent-record links and their labels are per entity; group-level metrics need the Step 08 corpus's provider links.
- The freeze receipt is a local JSON fingerprint check, not a signed governance record; it prevents accidental unsealing, not deliberate misuse.
- Graph verification reads every stored vector back from usearch on load (O(n·d)); acceptable at 16,840 × 256, revisit at much larger scale.

## Blocked / not run

| Item | Blocker |
| --- | --- |
| Real-data baseline and hybrid results | Need the reviewed, adjudicated corpus and frozen views from Step 08 (not accepted yet). |
| Double Metaphone | Needs a selected, licensed, pinned implementation. |
| OpenSanctions/yente logic matcher | Not installed; license/terms not verified here; no live/paid API substitute. |
| Pretrained E5 baseline in this matrix | Verified local weights are absent in this checkout (`data/models/` missing); nothing was downloaded. |
| LaBSE | Model/license/resource approval not granted. |
| Final test partition | Kept sealed; development only. |
| Qdrant (plan's proposed backend) | Not used; usearch HNSW (already a declared optional dependency) is the one proven local backend here. Container/client preflight would be a separate decision. |

## Shared-file changes requested (not made)

1. `src/watchlist/compare_retrieval.py`: pass `encoding='utf-8'` to every `read_text()` (at least `_multilingual_inputs` and `_phase3_inputs`) so the CLIs work on Windows without `PYTHONUTF8=1`.
2. `pyproject.toml`: optionally add the nine new files to the Ruff `include` list. No new dependencies are needed (`preprocessing` and `index` extras already declare anyascii, jellyfish and usearch; dense/hybrid need numpy from `embeddings`).

## Adversarial review outcomes (2026-10-26)

| # | Issue | Outcome |
|---|---|---|
| 1 | Linked duplicates filled top_k before `collapse_groups` | Fixed: grouping moved into `fuse`/`HybridRetriever` (rank over groups before depth and top_k cuts, deeper component fetch); `collapse_groups` removed. Test `test_linked_duplicates_do_not_consume_the_shortlist` (12 linked `Moro Kalo` + `Moro Kalu` → 2 rows). |
| 2 | Swapped HNSW graph accepted | Fixed: `_verify_graph` + manifest SHA-256 in `ACTIVE.json`. Test reproduces the swap. |
| 3 | Non-ASCII path save/load failure, staging residue | Fixed: bytes I/O, staging cleanup on ordinary errors. Tests for `índice-индекс-索引` and simulated write failure. |
| 4 | `--partition test` unsealed by one flag | Fixed: `--unseal-test` + matching freeze receipt required (also when the config lists `test`). |
| 5 | Vacuous hand-checked metric test | Fixed: fake constant-order arm on 6 targets; asserts R@1 = 1/6, MRR@3 = 11/36, 3 missing, R@10 N/A. |
| 6 | Provenance dropped | Fixed: `HybridCandidate.provenance`. |
| 7 | Config validation gaps | Fixed: full schema validation, strict `from_dict`. 10 parametrized rejection cases. |
| 8 | Emoji transliterated to `:grinning:` | Fixed: `UnsupportedText` for no recognized letters or non-ASCII symbols. |
| 9 | Blank query inconsistency | Fixed: `ValueError` in every arm (parametrized test). |
| 10 | Activation outside root | Fixed: single directory name under root; 7 parametrized cases. |
| 11 | Gaps | Deletion/update test added; `hybrid_retrieval evaluate --partition` added; weak tie test made exact. HNSW parameters still untuned (limitation, unchanged). |

## Next action

After Step 08 delivers an accepted reviewed corpus and frozen views: add a `real` dataset loader to `evaluate_baselines.py`, run the same command on its development partition, obtain approval/verified local weights for E5 (and LaBSE if approved) and plug them into `DenseRetriever` (it accepts any encoder with `encode` and `metadata`), then select and freeze the fusion rule, depths and HNSW parameters on development before any test-partition run.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](review-p1-final.md) and [its validation record](review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
