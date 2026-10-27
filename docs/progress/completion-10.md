# Completion Step 10 — normalization views, structured parsing and evidence reconciliation

Date: 2026-10-26. **Status: mechanics implemented and tested on fictional data only. The real-data experiment remains BLOCKED** (see below). No commit, push, network access, model download or dependency change occurred.

## Goal and why

Step 10 of `docs/project-completion-plan.md` asks for richer input handling that never loses the original evidence:

1. keep the existing NFKC/casefold normalization as a versioned control, add script evidence and a separately evaluated transliteration view;
2. keep the original text and record every transform and version; unsupported scripts get a visible status;
3. typed optional structured fields for organisations/vessels (identifiers, jurisdictions, dates); persons remain outside default scope;
4. deterministic agree / conflict / missing / not-comparable field evidence, kept separate from name scores;
5. a contract for messy-text extraction (implementation is Step 23) that requires confirmation before retrieval.

What: three small stdlib-first modules. Why: a reviewer must be able to see exactly how a name view was derived and which structured facts agree or differ, without the system turning those facts into an identity claim. Who: the data/workflow owner; future Steps 11, 12 and 23 consume these contracts. Where: `src/watchlist/`. When: before baselines (Step 11) so ablations use identical views. How: pure functions, frozen dataclasses, explicit error codes, deterministic sorting.

## Files

| File | Status | Purpose |
| --- | --- | --- |
| `src/watchlist/preprocessing.py` | new | Versioned profile pipeline, trace, script status, designator separation, collision measurement, CLI. |
| `config/preprocessing.json` | new | Four profiles plus designator rules (`designators-v2` after the review fixes; `designator_key` now also strips quotes and brackets). |
| `src/watchlist/structured_input.py` | new | Typed screening-request parsing/validation and the extraction-confirmation contract. |
| `src/watchlist/reconciliation.py` | new | Candidate attribute parsing with provenance and per-attribute evidence items. |
| `tests/test_preprocessing.py`, `tests/test_structured_input.py`, `tests/test_reconciliation.py` | new | 80 + 75 + 25 = 180 test cases (148 before the adversarial-review fixes). |
| `docs/progress/completion-10.md` | new | This report. |

No existing file was edited. `watchlist.normalize.normalize_name` is unchanged and is asserted equal to the control profile.

## Function-level explanation

### `preprocessing.py`

- `TransformStep(name, version, before, after)`: one applied transform. Every profile transform is recorded, including no-op steps, so the chain is complete.
- `_nfkc`, `_casefold` (casefold then NFKC, exactly as `normalize_name`), `_whitespace`: the `v1-compatible` control transforms.
- `HYPHEN_VARIANTS` (public, shared with registration-ID keys) and `_unify_punctuation`: maps hyphen/dash, apostrophe and quotation variants (for example U+2019, U+2013, «») to ASCII `-`, `'`, `"`.
- `_fold_latin_diacritics`: NFD, removes nonspacing marks only when their base letter is Latin, then NFC. Cyrillic й/ё and Arabic harakat are left untouched; letters without a decomposition (ø, ł) are not guessed.
- `_transliterate_anyascii`: lazy `from anyascii import anyascii`; a missing package raises `PreprocessingError("dependency_unavailable")`. The trace version records the installed package version (`1+anyascii-0.3.3`). This is a lossy romanization, not the ICU representation named in the plan.
- `parse_config` / `load_config`: strict validation (unknown fields, unknown transforms, duplicate names/keys, entity types, Han-only suffixes). The `v1-compatible` profile must keep exactly `nfkc, casefold, whitespace` and the 200 code-point limit, so the control cannot drift silently.
- `designator_key`: NFKC + casefold, dots removed, edge punctuation stripped (`L.L.C.` → `llc`, `Co.,` → `co`). Edge punctuation includes ASCII brackets/quotes, all apostrophe and quotation variants (`«»“”„`) and CJK brackets (`「」【】《》` ...), so `«ООО»` is recognised in every profile, not only after punctuation unification.
- `separate_designators`: removes leading/trailing runs of configured legal-form or vessel-prefix tokens, plus Han suffixes such as 有限公司 (longest first), into a separate `designators` tuple. Only edge positions, only for the configured entity types (`legal_form`: organization/unknown; `vessel_prefix`: vessel/unknown). A name made only of designator tokens (`Ltd LLC`, `Co Ltd`) keeps all tokens as core with no designators. `text` and `tokens` keep everything. `core_tokens` depends on the profile (Han suffixes are not recognised after transliteration), so core views from different profiles are not directly comparable; compare `text`/`tokens` across profiles.
- `preprocess(text, config, profile=None, *, entity_type="unknown") -> PreprocessedName`: validates input (string, valid Unicode, at most 200 code points, not blank), applies the profile, tokenizes on whitespace, separates designators and records `scripts` and `support_status` (`supported`, `partially_supported`, `unsupported`, `no_script_evidence`) from `watchlist.scripts.script_profile` of the **original** text.
- `verify_trace(result)`: checks the trace chains exactly from `original` to `text` (provenance-based reversibility).
- `view_collisions` / `evaluate_views`: on the same input names for every profile, counts distinct views and groups of different originals merged into one view. Counts only; no labels or scores.
- `EVALUATE_PARTITIONS` / `_fixture_names`: `evaluate` accepts only `development`, which reads `development` plus legacy `ambiguity` records (the same explicit mapping as `watchlist.compare_retrieval`). Any other partition, including `test`, `final-test` and misspellings, is `partition_not_allowed`; an empty result is `zero_support` (exit 2), never a silent `inputs: 0`.
- `main`: `show` (one name) and `evaluate` (fictional fixture partition) subcommands; UTF-8 JSON output on any console code page; exit code 2 with a JSON error object.

### `structured_input.py`

- `FieldError(field, code, message)` and `StructuredInputError(errors)`: all errors for a request, sorted deterministically. No partially repaired query is returned.
- `parse_screening_request(raw) -> ScreeningQuery` and non-raising `validate_screening_request(raw) -> (query | None, errors)`.
  - `name` required; same 200 code-point and blank rules as `normalize_name` and the CLI; control characters and lone surrogates are rejected. Stricter than `normalize_name`: bidi embedding/override/isolate controls (U+202A–U+202E, U+2066–U+2069) are `bidi_control`, and a name without any letter or digit (for example U+200B alone) is `no_letter_or_digit`. ZWJ/ZWNJ remain allowed for Arabic, Persian and Indic text.
  - `aliases`: up to 20, each validated the same way (including the bidi and letter/digit rules); duplicates after `normalize_name` are errors, not merged.
  - `entity_type`: `organization` (also accepts the spelling `organisation`), `vessel`, `unknown` (default). `person` → `out_of_scope_entity_type`.
  - `countries`: `"DE"` or `{"code", "role"}` with role in unspecified/jurisdiction/registration/flag/address. Exact uppercase officially assigned ISO 3166-1 alpha-2 codes (249 embedded); `de` is rejected with an uppercase hint, `UK`, `EU`, `XK` are rejected.
  - `dates`: `{"kind", "value"}`; kinds depend on entity type (vessels: build/registration). `parse_partial_date` accepts `YYYY`, `YYYY-MM`, `YYYY-MM-DD` with ASCII digits only; impossible dates such as 2001-02-30 are `invalid_date`. A date kind takes one value: `incorporation 2001` together with `incorporation 2005` or `2001-05` is `conflicting_values`.
  - `identifiers`: `imo` (7 ASCII digits, optional `IMO` prefix, `imo_checksum_valid` weights 7..2; the placeholder `0000000` is `invalid_imo_number`) and `registration` (optional `issuer_country`; comparison key is NFKC+casefold without spaces, dots, slashes and every hyphen/dash variant in `preprocessing.HYPHEN_VARIANTS`, so `AB–12` (en dash) keys like `AB-12`; underscores are kept). IMO numbers on an organization request are `not_applicable`.
- `ExtractedField` + `unconfirmed_consequential(fields)`: Step 23 contract. Every request field, including `aliases`, is consequential (`CONSEQUENTIAL_FIELDS`) and returns `unconfirmed_extraction` until `confirmed is True`; other field names (`identifier`, `Name`, `note`) are `unknown_field`; non-int or bool offsets and spans outside the source text are `invalid_span`; a `name`/`aliases` value that differs from `source_text[start:end]` is `value_not_in_span` (`VERBATIM_FIELDS`).

### `reconciliation.py`

- `EvidenceSource(source_id, record_id, field)`; `SourcedCountry`, `SourcedDate`, `SourcedIdentifier`; `CandidateRecord`.
- `parse_candidate_record(raw)`: reuses the query validators; every value must carry a `source` object or it is rejected.
- `reconcile(query, candidate) -> ReconciliationReport` with `EvidenceItem(attribute, status, details, query_values, candidate_values, provenance)`:
  - `entity_type`: unknown on either side → `not_comparable`.
  - `country:<role>`: same role, or `unspecified` on either side (detail `role_unspecified`). Candidate-only values → `missing` / `query_missing`. Several candidate values are all shown (`candidate_has_additional_values`); when they carry different roles each is displayed as `role:code` (for example `address:DE`, `flag:FR`).
  - `date:<kind>`: equal → `agree`; same up to the shared precision but different precision → `not_comparable` / `precision_differs_consistent`; otherwise `conflict`. Disagreeing candidate sources add `candidate_values_disagree`.
  - `identifier:imo|registration`: registration IDs from different issuers → `not_comparable` / `different_issuers`; missing issuer adds `issuer_unspecified`. Different-issuer identifiers get their own `not_comparable` item even when another identifier of the same scheme was comparable, so no candidate identifier or source is dropped.
- `ReconciliationReport.status_counts()` is display-only. There is no score, weight, fusion or probability field, and a test asserts this.

## Commands actually run and results

All from `D:\Projects\raiha\Polyglot_watchlist` with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`.

| Command | Result |
| --- | --- |
| `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_preprocessing.py tests/test_structured_input.py tests/test_reconciliation.py` | **180 passed**, 0 failed, 0 skipped (80 + 75 + 25 run separately) after the review fixes |
| `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider tests/test_normalize.py tests/test_scripts.py tests/test_entities.py` | 100 passed (existing behaviour unchanged) |
| `.tools/qa/Scripts/ruff.exe check --isolated --select E4,E7,E9,F --target-version py314 <6 new .py files>` | All checks passed |
| `.tools/qa/Scripts/ruff.exe format --check --config pyproject.toml <6 new .py files>` | 6 files already formatted |
| `... evaluate --config config/preprocessing.json --partition test` and `--partition devel` | exit 2, `{"error": "partition_not_allowed", ...}` |
| `.venv/Scripts/python.exe -B -m watchlist.preprocessing evaluate --config config/preprocessing.json --partition development` | see below |

During development one test failed (a bare `有限公司` token was split into `有限` + `公司`); the rule was fixed so that the longest matching suffix wins and a token that is itself a suffix is not split. Two test expectations of mine were also corrected (`Co.,` token text; leading-run order for `Ltd LLC`).

### Fictional regression output (not a real-data result)

`evaluate` on `eval/fixtures/multilingual_entities.json`, `development` partition. After the review fix this reads the 2 `development` records plus the 2 legacy `ambiguity` records (mapped to development diagnostics, as in `compare_retrieval`), giving 6 distinct authored English/Arabic names (before the fix: 4 names, development only). Labelled in the output as "fictional authored fixture; regression mechanics only":

| Profile | inputs | changed from control | distinct views | collision groups | support status |
| --- | --- | --- | --- | --- | --- |
| v1-compatible | 6 | 0 | 6 | 0 | 6 supported |
| punctuation-unified | 6 | 0 | 6 | 0 | 6 supported |
| latin-diacritics-folded | 6 | 0 | 6 | 0 | 6 supported |
| latin-transliteration | 6 | 3 | 6 | 0 | 6 supported |

Six fictional names say nothing about benefit or collision rates on real data.

## Example input/output

`python -m watchlist.preprocessing show --config config/preprocessing.json --profile latin-transliteration --entity-type organization --name "ООО «Ромашка-Трейд»"` (abridged):

```text
original "ООО «Ромашка-Трейд»"  scripts ["CYRILLIC"]  support_status "supported"
trace: nfkc (no change) -> unify_punctuation «» -> "" -> transliterate_anyascii (1+anyascii-0.3.3)
       "OOO \"Romashka-Treyd\"" -> casefold -> whitespace
text "ooo \"romashka-treyd\""   core_tokens ["\"romashka-treyd\""]
designators [{"text": "ooo", "key": "ooo", "category": "legal_form", "rule": "leading", "token_index": 0}]
```

Control profile, `星辰贸易有限公司` as organization: `text` unchanged, `core_tokens ["星辰贸易"]`, designator `有限公司` (`han_suffix`).

Same name, control profile: `«ооо»` is now separated as a designator (key `ооо`), matching `punctuation-unified`.

Reconciliation of a fictional vessel query (flag PA, build 2001, IMO 9074729) with a fictional candidate (flag LR, address GR, build 2001-05, IMO 9074729):

```text
country:address  missing         [query_missing]                 []         ['GR']
country:flag     conflict        [values_differ]                 ['PA']     ['LR']
date:build       not_comparable  [precision_differs_consistent]  ['2001']   ['2001-05']
entity_type      agree           []                              ['vessel'] ['vessel']
identifier:imo   agree           []                              ['9074729'] ['9074729']
status_counts {'agree': 2, 'conflict': 1, 'not_comparable': 1, 'missing': 1}
```

Different-issuer evidence is kept: query registration `DE:123` against candidate `FR:123` and `777` (no issuer) gives two items, `conflict [issuer_unspecified, values_differ]` with `['777']` and `not_comparable [different_issuers]` with `['FR:123']` (before the fix `FR:123` was dropped). Query country `DE` (unspecified role) against candidate flag FR and address DE: `agree`, candidate values `['address:DE', 'flag:FR']`.

Invalid request `{"name": "", "countries": ["de"], "dates": [{"kind": "build", "value": "2001-02-30"}], "identifiers": [{"scheme": "imo", "value": "9074728"}]}` returns all four errors: `countries/0 invalid_country_code`, `dates/0/value invalid_date`, `identifiers/0/value invalid_imo_checksum`, `name blank`.

## Limitations

- Transliteration uses `anyascii` (already installed), not ICU; it is lossy (Han becomes concatenated pinyin without tones, Arabic loses short vowels) and creates collisions (`Мира` and `Mira` both become `mira`, tested). The plan's ICU representation still needs dependency/license approval.
- Designator lists are a small hand-written set; cultural appropriateness is not reviewed. Edge-only matching can still separate a genuine name word (for example a trailing `Co` or `AO`); it is always kept in `text`/`tokens`, but a core-token view could mislead if used without the full view. Han suffixes are not recognized after transliteration (`星辰贸易有限公司` becomes one core token `xingchenmaoyiyouxiangongsi`), so `core_tokens` is not comparable across profiles.
- Tokenization is whitespace-only; Han and unspaced scripts produce one token apart from suffix splitting. No word segmentation or language inference.
- Script detection inherits the conservative Latin/Arabic/Cyrillic/Han limits of `watchlist.scripts`.
- The ISO 3166-1 list is embedded as of current knowledge (249 codes); changes to the standard require a code update. No historical codes. IMO validation covers ship numbers only, not IMO company numbers.
- No date-range or future-date checks beyond calendar validity and year ≥ 1000. Vessel flags change over time; reconciliation has no temporal model, so a flag conflict may simply reflect history.
- Bidi marks LRM/RLM (U+200E/U+200F) and other format characters are still accepted in names; only embedding/override/isolate controls are rejected. The control profile also differs from `normalize_name` for lone surrogates (structured error vs `UnicodeEncodeError`); both reject.
- Candidate records with `entity_type: person` are rejected by `parse_candidate_record`, consistent with the scope rules; person reference records cannot be reconciled.
- `conflict` means recorded values differ; it is not evidence of different entities, and `agree` is not evidence of identity.
- Not wired into the CLI, agent workflow or packets yet; no packet contract version was changed.

## BLOCKED / remaining

- **Real-data evaluation is BLOCKED** on the reviewed corpus from Step 08. The proposed `evaluate --partition development` currently reads only the fictional fixture; running it on real development data requires the reviewed corpus and a declared view contract.
- ICU transliteration: blocked on dependency/license approval.
- Integration into packets/agent state requires explicit contract versioning by the owners of `src/watchlist/agent/*` and review archives (not edited here).

## Shared-file changes requested (not made)

- `pyproject.toml` `[tool.ruff] include`: add the six new Python files (they already pass `ruff check` and `ruff format --check`).
- `anyascii`: no longer needed as a request; the working-tree `pyproject.toml` already declares `preprocessing = ["anyascii==0.3.3", ...]` (not edited by this step).

## Adversarial review fixes

| # | Issue | Outcome | Regression test |
| --- | --- | --- | --- |
| 1 | Different-issuer identifiers dropped when another identifier was comparable | fixed | `test_foreign_issuer_identifiers_are_never_dropped` |
| 2 | Unicode dashes in registration IDs gave false conflicts | fixed (`HYPHEN_VARIANTS` are separators) | `test_registration_keys_ignore_every_hyphen_variant`, `test_unicode_dash_registration_ids_agree` |
| 3 | Extraction contract let aliases, unknown fields, bool offsets and off-span values through | fixed | `test_extraction_contract_closes_reviewed_gaps` |
| 4 | Invisible and bidi-override names accepted | fixed (`bidi_control`, `no_letter_or_digit`) | `test_name_and_alias_errors`, `test_joiners_needed_by_arabic_persian_and_indic_stay_allowed` |
| 5 | Country roles hidden in mixed-role candidate values | fixed (`role:code`) | `test_mixed_candidate_roles_stay_visible` |
| 6 | `evaluate` had no partition guard / zero-support check | fixed (allow-list, ambiguity mapping, `zero_support`) | `test_evaluate_partition_guard_and_ambiguity_mapping`, `test_cli_show_and_evaluate` |
| 7 | Contradictory same-kind query dates accepted | fixed (`conflicting_values`) | `test_contradictory_same_kind_dates_are_errors` |
| 8 | Quoted designators separated only under punctuation unification | fixed (edge set extended, rules `designators-v2`); cross-profile core limitation documented | `test_quoted_designators_separate_in_every_profile`, `test_core_tokens_depend_on_profile_for_han_suffixes` |
| 9 | Docstring vs code for all-designator names | fixed code to match docstring | `test_designator_rules_respect_entity_type_and_position` |
| note | IMO `0000000` | fixed (`invalid_imo_number`) | `test_identifier_errors` |

## Next action

After the Step 08 reviewed corpus is accepted: add a corpus reader to `preprocessing.evaluate` that produces identical per-profile views for the development partition, then feed those views into the Step 11 baseline matrix and report collision and support-status counts beside the retrieval metrics.


## Adversarial review follow-up (2026-10-27)

A later independent review on branch `p1-review` changed some behaviour described above and withdrew figures it could not reproduce. Read [the review report](review-p1-final.md) and [its validation record](review-p1-validation.json) alongside this report; where they disagree, the review report is current. The text above is kept as the record of what was reported at the time.
