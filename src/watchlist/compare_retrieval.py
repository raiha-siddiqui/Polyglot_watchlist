"""Audited offline retrieval comparisons with explicit legacy regression adapters."""

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import platform
import time

from watchlist.database import connect_database, load_database
from watchlist.entities import Alias, Entity, load_entities
from watchlist.embeddings import E5Encoder, cached_aliases, validate_config
from watchlist.evaluate import aggregate, latency_summary, ranking_failure, summarize
from watchlist.evaluation_data import (
    BenchmarkViews,
    audit_queries,
    bind_legacy_authored_benchmark,
    build_authored_benchmark,
    build_benchmark,
    build_legacy_benchmark,
    fingerprint,
    query_labels,
)
from watchlist.evaluation_metrics import METRIC_PROTOCOL, ranking_fields
from watchlist.matching import rank_candidates
from watchlist.matching_embeddings import rank_vectors
from watchlist.scripts import disjoint_scripts, script_set


class _ExposedFixtureViews(BenchmarkViews):
    """Mark known fixtures as regression evidence without changing their contract.

    The inherited regression_only property controls legacy-row validation. This
    separate immutable usage marker says an already-exposed fixture cannot be
    new held-out evidence, even when its rows use the current protocol.
    """

    __slots__ = ()

    @property
    def dataset_regression_only(self) -> bool:
        return True


def _exposed_fixture(views: BenchmarkViews) -> BenchmarkViews:
    """Retain the original sealed context and exact aliases when labeling usage."""
    return _ExposedFixtureViews(views, views._context())


def _phase3_inputs(
    dataset: str, database: Path | None
) -> tuple[dict, tuple, dict, list[dict]]:
    """Read the declared source without silently substituting a newer database."""
    if dataset not in {"fictional", "public-small"}:
        raise ValueError("unknown Phase 3 dataset")
    if database is not None and dataset != "public-small":
        raise ValueError("database override applies only to public-small")
    filename = "baseline" if dataset == "fictional" else "public-small"
    config = json.loads(
        Path(f"eval/configs/{filename}.json").read_text(encoding="utf-8")
    )
    if dataset == "fictional":
        entities = load_entities()
        assignments = json.loads(Path(config["partitions"]).read_text(encoding="utf-8"))
    else:
        source_path = database if database is not None else Path(config["database"])
        entities, provenance, synthetic = load_database(source_path)
        manifest = json.loads(Path(config["partitions"]).read_text(encoding="utf-8"))
        if (
            synthetic
            or manifest["snapshot_id"] != provenance.snapshot_id
            or manifest["raw_sha256"] != provenance.raw_sha256
        ):
            raise ValueError("public split/source mismatch")
        assignments = manifest["assignments"]
        connection = connect_database(source_path, readonly=True)
        try:
            groups = dict(
                connection.execute("SELECT entity_id, group_id FROM entities")
            )
        finally:
            connection.close()
        if set(groups) != set(assignments) or any(
            assignments[key].get("group_id") != group for key, group in groups.items()
        ):
            raise ValueError(
                "public split groups differ from database source ownership"
            )
    absent = (
        json.loads(Path(config["absent_queries"]).read_text(encoding="utf-8"))
        if config.get("absent_queries")
        else []
    )
    return config, entities, assignments, absent


def _database_override(
    database: Path | None, database_override: Path | None
) -> Path | None:
    """Accept the explicit replay spelling without allowing conflicting sources."""
    if database is not None and database_override is not None:
        raise ValueError("supply only one database override")
    chosen = database if database is not None else database_override
    return Path(chosen) if chosen is not None else None


def phase3_benchmark(
    dataset: str, *, database: Path | None = None, database_override: Path | None = None
) -> tuple[dict, list[dict], dict]:
    """Reproduce historical queries and views exactly for frozen regression callers.

    Regression usage is carried by the bound views and evidence, never inserted
    into legacy query rows whose hashes are checked by frozen reranking.
    """
    chosen = _database_override(database, database_override)
    config, entities, assignments, absent = _phase3_inputs(dataset, chosen)
    old = json.loads(Path(config["output"], "results.json").read_text(encoding="utf-8"))
    views, regenerated, audit = build_legacy_benchmark(
        entities, assignments, seed=config["seed"], absent=absent
    )
    queries = [
        json.loads(line)
        for line in Path(config["output"], "queries.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    if (
        regenerated != queries
        or fingerprint(queries) != old["queries_fingerprint"]
        or audit != old["audit"]
    ):
        raise ValueError("Phase 3 benchmark changed; comparison refused")
    audit_queries(queries, views, regression_only=True)
    return (
        _exposed_fixture(views),
        queries,
        {
            "regression_only": True,
            "legacy_contract": True,
            "phase3_queries_fingerprint": old["queries_fingerprint"],
            "audit": audit,
            "baseline_rows": old["queries"],
            "database_override": str(chosen) if chosen is not None else None,
        },
    )


def current_phase3_benchmark(
    dataset: str, *, database: Path | None = None, database_override: Path | None = None
) -> tuple[dict, list[dict], dict]:
    """Build current-contract regression rows with explicit development mappings.

    Historical ambiguity groups remain diagnostics in development. Historical
    absence declarations also move to development because absence is a label,
    not an independent data-use partition. Frozen input files remain unchanged.
    """
    chosen = _database_override(database, database_override)
    config, entities, assignments, absent = _phase3_inputs(dataset, chosen)
    legacy_partitions = {
        identifier: "ambiguity"
        for identifier, item in assignments.items()
        if item["partition"] == "ambiguity"
    }
    current_assignments = {
        identifier: {**item, "partition": "development"}
        if identifier in legacy_partitions
        else dict(item)
        for identifier, item in assignments.items()
    }
    current_absent = [{**item, "partition": "development"} for item in absent]
    views, queries, audit = build_benchmark(
        entities,
        current_assignments,
        seed=config["seed"],
        absent=current_absent,
        legacy_partitions=legacy_partitions,
    )
    return (
        _exposed_fixture(views),
        queries,
        {
            "regression_only": True,
            "legacy_contract": False,
            "usage": "exposed-fixture-regression-only",
            "audit": {
                **audit,
                "usage": "exposed-fixture-regression-only",
                "regression_only": True,
            },
            "legacy_absent_mappings": [
                {
                    "group_id": item["id"],
                    "original_partition": "absent",
                    "partition": "development",
                }
                for item in absent
            ],
            "database_override": str(chosen) if chosen is not None else None,
        },
    )


def _multilingual_inputs() -> tuple[list[dict], tuple, dict, dict, list[dict]]:
    """Load and validate authored ownership before deriving either protocol."""
    raw = json.loads(
        Path("eval/fixtures/multilingual_entities.json").read_text(encoding="utf-8")
    )
    required = {
        "entity_id",
        "english",
        "arabic",
        "partition",
        "pair_type",
        "synthetic",
        "label_basis",
    }
    if (
        not isinstance(raw, list)
        or not raw
        or any(
            not isinstance(r, dict)
            or set(r) != required
            or any(
                not isinstance(r[key], str) or not r[key].strip()
                for key in required - {"synthetic"}
            )
            or r["synthetic"] is not True
            or r["pair_type"] not in {"translation", "transliteration"}
            for r in raw
        )
    ):
        raise ValueError("multilingual source must contain authored synthetic pairs")
    entities = tuple(
        Entity(
            r["entity_id"],
            r["english"],
            "organisation" if r["pair_type"] == "translation" else "person",
            (
                Alias(r["entity_id"] + "-en", r["english"]),
                Alias(r["entity_id"] + "-ar", r["arabic"]),
            ),
            True,
            "authored-multilingual-v1",
        )
        for r in raw
    )
    if len({e.entity_id for e in entities}) != len(entities):
        raise ValueError("duplicate multilingual entity ID")
    views = {
        "bilingual": entities,
        "english": tuple(replace(e, aliases=(e.aliases[0],)) for e in entities),
        "arabic": tuple(replace(e, aliases=(e.aliases[1],)) for e in entities),
    }
    queries = json.loads(
        Path("eval/fixtures/multilingual_queries.json").read_text(encoding="utf-8")
    )
    records = {r["entity_id"]: r for r in raw}
    if not isinstance(queries, list) or any(
        not isinstance(q, dict)
        or not isinstance(q.get("target_id"), str)
        or q["target_id"] not in records
        or q.get("pair_type") != records[q["target_id"]]["pair_type"]
        for q in queries
    ):
        raise ValueError("authored query pair type or target differs from its source")
    assignments = {
        r["entity_id"]: {"partition": r["partition"], "group_id": r["entity_id"]}
        for r in raw
    }
    bound, checked_queries = bind_legacy_authored_benchmark(
        entities, assignments, views, queries
    )
    return raw, entities, assignments, bound, checked_queries


def multilingual_benchmark() -> tuple[dict, list[dict], dict]:
    """Preserve the original authored rows and alias views for regression only."""
    raw, _, _, views, queries = _multilingual_inputs()
    return (
        _exposed_fixture(views),
        queries,
        {
            "regression_only": True,
            "legacy_contract": True,
            "source_fingerprint": fingerprint(raw),
            "queries_fingerprint": fingerprint(queries),
            "view_fingerprints": {
                v: fingerprint([asdict(e) for e in es]) for v, es in views.items()
            },
        },
    )


def current_multilingual_benchmark() -> tuple[dict, list[dict], dict]:
    """Derive current labels from exposed authored pairs for regression only."""
    raw, entities, assignments, views, legacy_queries = _multilingual_inputs()
    current_assignments = {
        identifier: {**item, "partition": "development"}
        if item["partition"] == "ambiguity"
        else dict(item)
        for identifier, item in assignments.items()
    }
    specs = []
    for query in legacy_queries:
        spec = {
            key: query[key]
            for key in ("target_id", "alias_id", "kind", "text", "view", "pair_type")
        }
        if query["partition"] == "ambiguity":
            spec["legacy_partition"] = "ambiguity"
        specs.append(spec)
    current_views, queries, audit = build_authored_benchmark(
        entities, current_assignments, dict(views), specs
    )
    return (
        _exposed_fixture(current_views),
        queries,
        {
            "regression_only": True,
            "legacy_contract": False,
            "usage": "exposed-fixture-regression-only",
            "source_fingerprint": fingerprint(raw),
            "audit": {
                **audit,
                "usage": "exposed-fixture-regression-only",
                "regression_only": True,
            },
        },
    )


def _validate_options(repetitions: int, requested_k: int | None) -> None:
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("positive timing repetitions required")
    if requested_k is not None and (type(requested_k) is not int or requested_k < 1):
        raise ValueError(
            "requested_k must be a positive integer or None for a full ranking"
        )


def _cross_script(query: dict, entities: tuple) -> tuple[bool, str]:
    """Compare script sets only with the target's aliases retained in this view."""
    query_scripts = script_set(query["text"])
    reference_scripts = {
        script
        for entity in entities
        if entity.entity_id == query["target_id"]
        for alias in entity.aliases
        for script in script_set(alias.name)
    }
    direction = (
        "+".join(sorted(query_scripts)) + "→" + "+".join(sorted(reference_scripts))
    )
    return query["target_id"] is not None and disjoint_scripts(
        query_scripts, reference_scripts
    ), direction


def _check_candidates(candidates: list, entities: tuple, requested_k: int) -> None:
    """Require exactly the returned prefix size and evidence from the supplied view."""
    members = {entity.entity_id: entity for entity in entities}
    if len(candidates) != min(len(members), requested_k):
        raise ValueError("candidate count differs from the requested reference prefix")
    for candidate in candidates:
        entity = members.get(candidate.entity_id)
        if entity is None or (
            candidate.matched_alias_id,
            candidate.matched_alias,
        ) not in {(alias.alias_id, alias.name) for alias in entity.aliases}:
            raise ValueError("candidate evidence is outside the supplied alias view")


def compare(
    views: dict,
    queries: list[dict],
    encoder: object,
    cache_root: Path,
    repetitions: int,
    *,
    requested_k: int | None = None,
    regression_only: bool = False,
) -> dict:
    """Audit before any cache/model work; rank only text against its retained view.

    The complete source manifest stays in the evaluator. Cache and retrieval
    functions receive only the selected view's aliases; unused full views are
    never encoded for a held-out-only query subset. Labels score returned lists
    afterward, and a missing shortlist target keeps an unknown rank.
    """
    _validate_options(repetitions, requested_k)
    audit_queries(queries, views, regression_only=regression_only)
    dataset_regression_only = regression_only or getattr(
        views, "dataset_regression_only", False
    )
    used_views = sorted({query["view"] for query in queries})
    if any(not views[name] for name in used_views):
        raise ValueError("empty evaluation reference")
    cached = {
        name: cached_aliases(views[name], encoder, cache_root) for name in used_views
    }
    methods = {"rapidfuzz": [], "e5_small": []}
    timings = {"rapidfuzz": [], "e5_small": []}
    for number, q in enumerate(queries, 1):
        entities = views[q["view"]]
        limit = len(entities) if requested_k is None else requested_k
        matrix = cached[q["view"]][0]
        # Untimed outputs also warm both methods; aliases are identical by construction.
        rankings = {
            "rapidfuzz": rank_candidates(q["text"], entities, limit),
            "e5_small": rank_vectors(
                encoder.encode([q["text"]]), matrix, entities, limit
            ),
        }
        labelled = {**q, **query_labels(q, regression_only=regression_only)}
        fields = {}
        for method, candidates in rankings.items():
            _check_candidates(candidates, entities, limit)
            fields[method] = ranking_fields(
                labelled,
                candidates,
                index_entity_count=len(entities),
                requested_k=limit,
            )
        total, encoding, searching, fuzzy = [], [], [], []
        for _ in range(repetitions):
            start = time.perf_counter_ns()
            repeated_fuzzy = rank_candidates(q["text"], entities, limit)
            fuzzy.append((time.perf_counter_ns() - start) / 1e6)
            if repeated_fuzzy != rankings["rapidfuzz"]:
                raise ValueError("RapidFuzz ranking changed across timing repetitions")
            start = time.perf_counter_ns()
            vector = encoder.encode([q["text"]])
            middle = time.perf_counter_ns()
            repeated_vectors = rank_vectors(vector, matrix, entities, limit)
            end = time.perf_counter_ns()
            if repeated_vectors != rankings["e5_small"]:
                raise ValueError("E5 ranking changed across timing repetitions")
            encoding.append((middle - start) / 1e6)
            searching.append((end - middle) / 1e6)
            total.append((end - start) / 1e6)
        timings["rapidfuzz"].append({"query_id": q["query_id"], "samples_ms": fuzzy})
        timings["e5_small"].append(
            {
                "query_id": q["query_id"],
                "samples_ms": total,
                "encoding_ms": encoding,
                "search_ms": searching,
            }
        )
        for method, candidates in rankings.items():
            cross, direction = _cross_script(q, entities)
            methods[method].append(
                {
                    **labelled,
                    **fields[method],
                    "regression_only": dataset_regression_only,
                    "legacy_contract": regression_only,
                    "cross_script_only": cross,
                    "cross_script_direction": direction,
                    "candidate_count": len(candidates),
                    "failure": ranking_failure(
                        labelled["status"],
                        fields[method]["target_rank"],
                        candidates,
                        "cosine similarity"
                        if method == "e5_small"
                        else "normalized edit similarity",
                    ),
                    "candidates": [asdict(c) for c in candidates],
                }
            )
        if number % 25 == 0:
            print(f"Compared {number}/{len(queries)} queries", flush=True)
    results = {}
    for method, rows in methods.items():
        slices = aggregate(rows)
        # True cross-script cases are additional diagnostics, not replacements for original slices.
        for partition in ("development", "test"):
            subset = [
                r
                for r in rows
                if r["partition"] == partition and r["cross_script_only"]
            ]
            slices.append(
                {
                    "partition": partition,
                    "suite": "cross-script-only",
                    "slice": "all",
                    **summarize(subset),
                }
            )
            for direction in sorted({r["cross_script_direction"] for r in subset}):
                selected = [
                    r for r in subset if r["cross_script_direction"] == direction
                ]
                slices.append(
                    {
                        "partition": partition,
                        "suite": "cross-script-only",
                        "slice": direction,
                        **summarize(selected),
                    }
                )
            for pair_type in sorted(
                {r.get("pair_type", "source-alias") for r in subset}
            ):
                slices.append(
                    {
                        "partition": partition,
                        "suite": "cross-script-only",
                        "slice": pair_type,
                        **summarize(
                            [
                                r
                                for r in subset
                                if r.get("pair_type", "source-alias") == pair_type
                            ]
                        ),
                    }
                )
        results[method] = {
            "queries": rows,
            "aggregates": slices,
            "timing": {
                "per_query": timings[method],
                **latency_summary(
                    [t for x in timings[method] for t in x["samples_ms"]]
                ),
            },
        }
        if method == "e5_small":
            for component in ("encoding_ms", "search_ms"):
                results[method]["timing"][component] = latency_summary(
                    [t for x in timings[method] for t in x[component]]
                )
    return {
        "methods": results,
        "reference_cache": {k: v[1] for k, v in cached.items()},
        "metric_protocol_version": METRIC_PROTOCOL,
        "regression_only": dataset_regression_only,
        "legacy_contract": regression_only,
        "benchmark_protocol_version": views.protocol_version,
        "requested_k": requested_k,
        "timing_repetitions": repetitions,
        "query_fingerprint": fingerprint(queries),
    }


def report(result: dict) -> str:
    """Render computed values and paired successes/regressions without hand-entering scores."""

    def show(m: dict) -> str:
        return (
            f"{m['value']:.4f} (n={m['denominator']})"
            if m["value"] is not None
            else "N/A"
        )

    def truncated(m: dict) -> str:
        return show(m) + (f"; K={m['k']}" if m.get("k") is not None else "")

    usage = (
        "historical-contract regression only"
        if result.get("legacy_contract")
        else "exposed-fixture regression only (current evaluation contract)"
        if result.get("regression_only")
        else "current research evaluation"
    )
    lines = [
        "# Paired retrieval comparison",
        "",
        f"Dataset: {result['dataset']}",
        "",
        f"Usage: {usage}.",
        "",
        "Cosine and edit similarity are ranking signals, not identity probabilities.",
        "",
        "| Method | Partition / suite / slice | Queries / groups | R@1 | R@10 | R@50 | Full MRR | MRR@K |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for method, data in result["methods"].items():
        for a in data["aggregates"]:
            if a["query_count"]:
                lines.append(
                    f"| {method} | {a['partition']} / {a['suite']} / {a['slice']} | {a['query_count']} / {a['group_count']} | "
                    + " | ".join(
                        show(a[k])
                        for k in ("recall_at_1", "recall_at_10", "recall_at_50", "mrr")
                    )
                    + " | "
                    + truncated(a["mrr_at_k"])
                    + " |"
                )
                if len(a.get("mrr_by_depth", {})) > 1:
                    for metric in a["mrr_by_depth"].values():
                        lines.append(
                            f"| {method} | {a['partition']} / {a['suite']} / {a['slice']} / "
                            f"requested={metric['requested_k']}, evaluated={metric['k']} | — | — | — | — | — | "
                            + truncated(metric)
                            + " |"
                        )
    lines += [
        "",
        "## Warm latency",
        "",
        "E5 total includes fresh query encoding, exact search and entity grouping. Reference encoding and model load are separate.",
    ]
    for method, data in result["methods"].items():
        timing = data["timing"]
        if timing["observations"]:
            lines.append(
                f"- {method}: median {timing['median_ms']:.4f} ms, p95 {timing['p95_ms']:.4f} ms, {timing['observations']} observations."
            )
        else:
            lines.append(f"- {method}: no timed queries.")
    lines += [
        "",
        "## Paired examples",
        "",
        "First ten improvements and first ten regressions among eligible test positives with observed ranks in both methods. A missing shortlist target has no known rank; all returned candidates remain in JSON.",
    ]
    pairs = list(
        zip(
            result["methods"]["rapidfuzz"]["queries"],
            result["methods"]["e5_small"]["queries"],
        )
    )
    for label, sign in [("Improvements", 1), ("Regressions", -1)]:
        selected = [
            (a, b)
            for a, b in pairs
            if a["status"] == "positive"
            and a["partition"] == "test"
            and a["target_rank"] is not None
            and b["target_rank"] is not None
            and (a["target_rank"] - b["target_rank"]) * sign > 0
        ]
        lines += ["", f"### {label}: {len(selected)}", ""]
        for a, b in selected[:10]:
            lines.append(
                f"- {json.dumps(a['text'], ensure_ascii=False)} ({a['kind']}): target `{a['target_id']}`, RapidFuzz rank {a['target_rank']} → E5 rank {b['target_rank']}."
            )
    lines += [
        "",
        "## Limits",
        "",
        "Small authored/selected samples; correlated aliases; no population confidence guarantee. Translation and transliteration are different tasks. Unknown upstream duplicates and pretrained-data overlap remain possible. No training, reranking, threshold or automatic identity decision.",
        "Recall eligibility uses the index population and evaluated depth. Full MRR is unavailable for truncated rankings. MRR@K counts an unreturned target as zero at the stated cutoff without assigning a later rank. N/A also covers empty denominators, insufficient evaluated depth, or a cutoff covering the entire index. Cross-script diagnostics require nonempty, known, disjoint script sets for the query and its target’s retained aliases.",
        "",
    ]
    return "\n".join(lines)


def _output_destinations(output_root: Path, datasets: list[str]) -> dict[str, Path]:
    """Protect historical trees and require a new directory for every result."""
    root = output_root.resolve()
    project = Path(__file__).resolve().parents[2]
    historical = (project / "eval/results", project / "data/evaluation")
    destinations = {dataset: root / dataset for dataset in datasets}
    for path in (root, *destinations.values()):
        resolved = path.resolve()
        if any(
            resolved == protected or resolved.is_relative_to(protected)
            for protected in historical
        ):
            raise ValueError(
                "historical result destinations are read-only; choose a new output root"
            )
    if root.exists() and not root.is_dir():
        raise ValueError("output root must be a directory")
    if any(path.exists() or path.is_symlink() for path in destinations.values()):
        raise ValueError("output dataset destination already exists; overwrite refused")
    cache = root / "reference-cache"
    if cache.exists() or cache.is_symlink():
        raise ValueError(
            "output cache destination already exists; choose a new output root"
        )
    return destinations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--dataset", choices=["fictional", "public-small", "multilingual"]
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="explicit public-small database override; never inferred",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="new result directory; historical trees are protected",
    )
    parser.add_argument(
        "--requested-k",
        type=int,
        help="returned prefix length; omitted means a full ranking",
    )
    parser.add_argument(
        "--regression-only",
        action="store_true",
        help="explicitly reproduce historical benchmark rows",
    )
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        datasets = [args.dataset] if args.dataset else config["datasets"]
        if (
            not isinstance(datasets, list)
            or not datasets
            or any(
                dataset not in {"fictional", "public-small", "multilingual"}
                for dataset in datasets
            )
            or len(set(datasets)) != len(datasets)
        ):
            raise ValueError(
                "datasets must be a nonempty list of distinct supported names"
            )
        if args.database is not None and "public-small" not in datasets:
            raise ValueError("--database requires the public-small dataset")
        _validate_options(config["timing_repetitions"], args.requested_k)
        destinations = _output_destinations(args.output_root, datasets)
        prepared = []
        for dataset in datasets:
            if dataset == "multilingual":
                benchmark = (
                    multilingual_benchmark()
                    if args.regression_only
                    else current_multilingual_benchmark()
                )
            else:
                factory = (
                    phase3_benchmark
                    if args.regression_only
                    else current_phase3_benchmark
                )
                benchmark = factory(
                    dataset,
                    database=args.database if dataset == "public-small" else None,
                )
            views, queries, evidence = benchmark
            audit_queries(queries, views, regression_only=args.regression_only)
            if any(not views[query["view"]] for query in queries):
                raise ValueError("empty evaluation reference")
            prepared.append((dataset, views, queries, evidence))
        validate_config(config["model"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    # Every selected benchmark and destination has passed before model loading.
    encoder = E5Encoder(config["model"])
    for dataset, views, queries, evidence in prepared:
        print(f"Starting {dataset}", flush=True)
        result = compare(
            views,
            queries,
            encoder,
            args.output_root / "reference-cache",
            config["timing_repetitions"],
            requested_k=args.requested_k,
            regression_only=args.regression_only,
        )
        # Guard the original baseline's rankings/scores as well as its query hashes.
        evidence = dict(evidence)
        if "baseline_rows" in evidence:
            old_rows = evidence.pop("baseline_rows")
            new_rows = result["methods"]["rapidfuzz"]["queries"]
            if len(old_rows) != len(new_rows):
                raise ValueError(
                    "RapidFuzz query count differs from saved Phase 3 ranking"
                )
            for old, new in zip(old_rows, new_rows):
                expected = (
                    old["candidates"]
                    if args.requested_k is None
                    else old["candidates"][: args.requested_k]
                )
                if old["query_id"] != new["query_id"] or expected != new["candidates"]:
                    raise ValueError("RapidFuzz differs from saved Phase 3 ranking")
        result.update(
            dataset=dataset,
            evidence=evidence,
            model=encoder.metadata,
            model_load_seconds=encoder.load_seconds,
            environment={
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
        )
        output = destinations[dataset]
        output.mkdir(parents=True, exist_ok=False)
        with (output / "results.json").open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        with (output / "report.md").open("x", encoding="utf-8") as stream:
            stream.write(report(result))
        print(f"Saved {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
