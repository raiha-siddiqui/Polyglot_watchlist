"""Run every baseline arm on the same validated fictional benchmarks (Step 11).

All arms receive only query text and the selected reference view. Labels are
applied afterward through the shared retrieval-metrics-v2 contract (Recall@K,
MRR@K with missing shortlist targets counted as zero at the cutoff). Results
go to a new output root; existing destinations are refused.

These datasets are fictional and previously exposed or generated: every number
is regression/mechanics evidence only, never a real-world performance claim.

    python -m watchlist.evaluate_baselines --config eval/configs/baselines-v2.json \
        --output-root data/eval-comparisons/<new-name> --partition development

The final test partition is sealed: selecting it (from the config or the
command line) requires ``--unseal-test`` plus a freeze receipt whose config
fingerprint matches the frozen development configuration.
"""

import argparse
from collections.abc import Callable
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import time

from watchlist.baselines import ARMS, BASELINE_PROTOCOL, build_retriever
from watchlist.evaluate import aggregate, latency_summary, ranking_failure
from watchlist.evaluation_data import (
    audit_queries,
    build_benchmark,
    fingerprint,
    query_labels,
)
from watchlist.evaluation_metrics import METRIC_PROTOCOL, ranking_fields

PROTOCOL = "baselines-evaluation-v2"
DATASETS = ("fictional", "multilingual", "synthetic-scale")
DENSE_ARM = "char_ngram_cosine"
PREVIEW = 5


REQUIRED_KEYS = frozenset(
    {
        "protocol_version",
        "datasets",
        "partitions",
        "requested_k",
        "timing_repetitions",
        "arms",
    }
)
OPTIONAL_KEYS = frozenset(
    {"usage", "synthetic_scale", "dense_encoder", "hybrid_variants", "not_run"}
)
VARIANT_KEYS = frozenset(
    {"arm", "lexical_arm", "phonetic_arm", "dense_backend", "dense_parameters"}
)
HNSW_KEYS = frozenset({"connectivity", "expansion_add", "expansion_search"})
FREEZE_PROTOCOL = "baselines-freeze-v1"


def _int(value: object, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _exact_keys(value: object, name: str, keys: frozenset[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(
            f"{name} must be an object with exactly: " + ", ".join(sorted(keys))
        )
    return value


def _validate_variant(variant: object) -> str:
    """One predeclared hybrid variant; every key is checked, none ignored."""
    from watchlist.hybrid_retrieval import HybridConfig

    if not isinstance(variant, dict):
        raise ValueError("hybrid variant must be an object")
    arm = variant.get("arm")
    if not isinstance(arm, str) or not arm or arm in ARMS or arm == DENSE_ARM:
        raise ValueError("hybrid variant arm must be a new nonblank name")
    HybridConfig.from_dict(variant, allowed_extra=VARIANT_KEYS)
    for key in ("lexical_arm", "phonetic_arm"):
        if key in variant and variant[key] not in ARMS:
            raise ValueError(f"{arm}: unknown {key}")
    backend = variant.get("dense_backend", "exact")
    if backend not in {"exact", "hnsw"}:
        raise ValueError(f"{arm}: dense_backend must be exact or hnsw")
    parameters = variant.get("dense_parameters")
    if parameters is not None:
        if (
            backend != "hnsw"
            or not isinstance(parameters, dict)
            or not set(parameters) <= HNSW_KEYS
        ):
            raise ValueError(
                f"{arm}: dense_parameters are HNSW-only: "
                + ", ".join(sorted(HNSW_KEYS))
            )
        for key, value in parameters.items():
            _int(value, f"{arm}.dense_parameters.{key}", 2)
    return arm


def validate_config(config: dict) -> dict:
    """Check the full schema before any retrieval; unknown or missing keys are errors."""
    if not isinstance(config, dict) or config.get("protocol_version") != PROTOCOL:
        raise ValueError(f"config must declare protocol_version {PROTOCOL}")
    missing, unknown = (
        REQUIRED_KEYS - set(config),
        set(config) - REQUIRED_KEYS - OPTIONAL_KEYS,
    )
    if missing or unknown:
        raise ValueError(
            f"config keys: missing {sorted(missing)}, unknown {sorted(unknown)}"
        )
    datasets = config["datasets"]
    if (
        not isinstance(datasets, list)
        or not datasets
        or len(set(datasets)) != len(datasets)
        or any(name not in DATASETS for name in datasets)
    ):
        raise ValueError("datasets must be distinct names among " + ", ".join(DATASETS))
    partitions = config["partitions"]
    if (
        not isinstance(partitions, list)
        or not partitions
        or len(set(partitions)) != len(partitions)
        or any(p not in {"development", "test"} for p in partitions)
    ):
        raise ValueError("partitions must list development and/or test")
    for key in ("requested_k", "timing_repetitions"):
        _int(config[key], key, 1)
    if "synthetic-scale" in datasets or "synthetic_scale" in config:
        scale = _exact_keys(
            config.get("synthetic_scale"),
            "synthetic_scale",
            frozenset({"entity_count", "seed", "absent_count"}),
        )
        _int(scale["entity_count"], "synthetic_scale.entity_count", 1)
        _int(scale["seed"], "synthetic_scale.seed", 0)
        _int(scale["absent_count"], "synthetic_scale.absent_count", 0)
    variants = config.get("hybrid_variants", [])
    if not isinstance(variants, list):
        raise ValueError("hybrid_variants must be a list")
    hybrids = [_validate_variant(variant) for variant in variants]
    if len(set(hybrids)) != len(hybrids):
        raise ValueError("hybrid variant arms must be distinct")
    known = set(ARMS) | {DENSE_ARM} | set(hybrids)
    arms = config["arms"]
    if (
        not isinstance(arms, list)
        or not arms
        or len(set(arms)) != len(arms)
        or any(a not in known for a in arms)
    ):
        raise ValueError(
            "arms must be distinct known baseline, dense or declared hybrid arms"
        )
    if any(arm not in ARMS for arm in arms) or "dense_encoder" in config:
        encoder = _exact_keys(
            config.get("dense_encoder"),
            "dense_encoder",
            frozenset({"kind", "dimension", "ngram_range"}),
        )
        if encoder["kind"] != "hashed-char-ngram-v1":
            raise ValueError(
                "only the hashed-char-ngram-v1 dense encoder is supported offline"
            )
        _int(encoder["dimension"], "dense_encoder.dimension", 8)
        span = encoder["ngram_range"]
        if (
            not isinstance(span, list)
            or len(span) != 2
            or any(type(v) is not int for v in span)
            or not 1 <= span[0] <= span[1]
        ):
            raise ValueError(
                "dense_encoder.ngram_range must be [low, high] with 1 <= low <= high"
            )
    not_run = config.get("not_run", [])
    if not isinstance(not_run, list) or any(
        not isinstance(item, dict)
        or set(item) != {"arm", "reason"}
        or not all(isinstance(v, str) and v.strip() for v in item.values())
        for item in not_run
    ):
        raise ValueError("not_run must list {arm, reason} objects")
    return config


def frozen_fingerprint(config: dict) -> str:
    """Fingerprint of everything except the partition selection."""
    return fingerprint(
        {key: value for key, value in config.items() if key != "partitions"}
    )


def make_freeze_receipt(config: dict, *, frozen_by: str, note: str) -> dict:
    """Receipt recording that arms, variants and parameters were frozen on development."""
    validate_config(config)
    if not frozen_by.strip() or not note.strip():
        raise ValueError("a freeze receipt needs frozen_by and note")
    return {
        "protocol_version": FREEZE_PROTOCOL,
        "config_fingerprint": frozen_fingerprint(config),
        "frozen_by": frozen_by,
        "note": note,
    }


def check_sealed(
    config: dict, *, unseal_test: bool, freeze_receipt: Path | None
) -> None:
    """Refuse the test partition unless explicitly unsealed against a matching freeze receipt."""
    if "test" not in config["partitions"]:
        if unseal_test or freeze_receipt is not None:
            raise ValueError(
                "--unseal-test/--freeze-receipt given but the test partition is not selected"
            )
        return
    if not unseal_test or freeze_receipt is None:
        raise ValueError(
            "the test partition is sealed; it needs --unseal-test and --freeze-receipt"
        )
    receipt = json.loads(Path(freeze_receipt).read_text(encoding="utf-8"))
    if (
        not isinstance(receipt, dict)
        or receipt.get("protocol_version") != FREEZE_PROTOCOL
    ):
        raise ValueError(
            f"freeze receipt must declare protocol_version {FREEZE_PROTOCOL}"
        )
    if receipt.get("config_fingerprint") != frozen_fingerprint(config):
        raise ValueError(
            "freeze receipt does not match this configuration; the test partition stays sealed"
        )


def synthetic_benchmark(settings: dict) -> tuple[dict, list[dict], dict]:
    """Build current-contract queries over the generated fictional corpus."""
    from watchlist.synthetic_corpus import generate_corpus

    corpus = generate_corpus(
        settings["entity_count"], settings["seed"], settings["absent_count"]
    )
    views, queries, audit = build_benchmark(
        corpus.entities, corpus.assignments, seed=settings["seed"], absent=corpus.absent
    )
    return (
        views,
        queries,
        {
            "usage": "generated-fictional-mechanics-only",
            "corpus": corpus.manifest,
            "skipped_transformations": len(audit["skipped"]),
            "shared_aliases": len(audit["shared_aliases"]),
        },
    )


def load_dataset(name: str, config: dict) -> tuple[dict, list[dict], dict]:
    """Return bound views, current-contract queries and provenance evidence."""
    if name == "synthetic-scale":
        return synthetic_benchmark(config["synthetic_scale"])
    # Lazy: compare_retrieval imports the optional embedding stack.
    from watchlist.compare_retrieval import (
        current_multilingual_benchmark,
        current_phase3_benchmark,
    )

    if name == "fictional":
        views, queries, evidence = current_phase3_benchmark("fictional")
    elif name == "multilingual":
        views, queries, evidence = current_multilingual_benchmark()
    else:
        raise ValueError(f"unknown dataset: {name}")
    return views, queries, {"usage": evidence.get("usage"), "regression_only": True}


def make_arm(arm: str, entities: tuple, config: dict):
    """Construct one arm; dense/hybrid arms import NumPy (and usearch) lazily."""
    if arm in ARMS:
        return build_retriever(arm, entities)
    from watchlist.hybrid_retrieval import DenseRetriever, HybridConfig, build_hybrid
    from watchlist.vector_index import HashedCharNgramEncoder

    settings = config["dense_encoder"]
    encoder = HashedCharNgramEncoder(
        settings["dimension"], tuple(settings["ngram_range"])
    )
    if arm == DENSE_ARM:
        return DenseRetriever(entities, encoder)
    variant = next(item for item in config["hybrid_variants"] if item["arm"] == arm)
    return build_hybrid(
        entities,
        encoder,
        HybridConfig.from_dict(variant, allowed_extra=VARIANT_KEYS),
        lexical_arm=variant.get("lexical_arm", "levenshtein"),
        phonetic_arm=variant.get("phonetic_arm", "phonetic_metaphone"),
        dense_backend=variant.get("dense_backend", "exact"),
        dense_parameters=variant.get("dense_parameters"),
        arm=arm,
    )


def _check_evidence(candidates: tuple, entities: tuple, requested_k: int) -> None:
    """Every candidate and every cited alias must come from the supplied view."""
    allowed = {
        entity.entity_id: {(alias.alias_id, alias.name) for alias in entity.aliases}
        for entity in entities
    }
    if len(candidates) > min(len(entities), requested_k):
        raise ValueError("more candidates than the requested reference prefix")
    for candidate in candidates:
        cited = [
            (candidate.entity_id, candidate.matched_alias_id, candidate.matched_alias)
        ]
        cited += [
            (item.entity_id, item.matched_alias_id, item.matched_alias)
            for item in getattr(candidate, "evidence", ())
        ]
        if any(
            owner not in allowed or (alias_id, name) not in allowed[owner]
            for owner, alias_id, name in cited
        ):
            raise ValueError("candidate evidence is outside the supplied alias view")


def _suite(row: dict) -> str:
    """Same suite rule as ``evaluate.aggregate``."""
    return (
        "diagnostic"
        if row["status"] == "diagnostic"
        else "absent"
        if row["status"] == "absent"
        else "sanity"
        if row["kind"] in {"exact", "formatting"}
        else "challenge"
    )


def evaluate_arm(arm: str, views: dict, queries: list[dict], config: dict) -> dict:
    """Retrieve, time and score one arm; aggregates use the shared metric contract."""
    requested_k, repetitions = config["requested_k"], config["timing_repetitions"]
    retrievers, build_seconds = {}, {}
    for view in sorted({query["view"] for query in queries}):
        start = time.perf_counter()
        retrievers[view] = make_arm(arm, views[view], config)
        build_seconds[view] = time.perf_counter() - start
    rows, compact, samples = [], [], []
    for query in queries:
        entities = views[query["view"]]
        retriever = retrievers[query["view"]]
        result = retriever.retrieve(query["text"], requested_k)  # untimed warm call
        _check_evidence(result.candidates, entities, requested_k)
        for _ in range(repetitions):
            start = time.perf_counter_ns()
            repeated = retriever.retrieve(query["text"], requested_k)
            samples.append((time.perf_counter_ns() - start) / 1e6)
            if repeated != result:
                raise ValueError(f"{arm} ranking changed across timing repetitions")
        labelled = {**query, **query_labels(query)}
        fields = ranking_fields(
            labelled,
            result.candidates,
            index_entity_count=len(entities),
            requested_k=requested_k,
        )
        failure = (
            f"query unsupported: {result.unsupported_reason}"
            if result.unsupported_reason and labelled["status"] == "positive"
            else ranking_failure(
                labelled["status"],
                fields["target_rank"],
                list(result.candidates),
                f"{arm} score",
            )
        )
        rows.append(
            {
                **labelled,
                **fields,
                "regression_only": True,
                "candidates": result.candidates,
            }
        )
        compact.append(
            {
                key: labelled[key]
                for key in (
                    "query_id",
                    "text",
                    "kind",
                    "script",
                    "view",
                    "partition",
                    "status",
                    "target_id",
                )
            }
            | {
                key: fields[key]
                for key in (
                    "target_rank",
                    "returned_candidate_count",
                    "evaluated_depth",
                )
            }
            | {
                "suite": _suite(labelled),
                "unsupported_reason": result.unsupported_reason,
                "failure": failure,
                "component_status": dict(result.component_status) or None,
                "candidates_preview": [asdict(c) for c in result.candidates[:PREVIEW]],
            }
        )
    aggregates = aggregate(rows)
    unsupported: dict[str, int] = {}
    for row in compact:
        if row["unsupported_reason"]:
            key = f"{row['partition']}/{row['suite']}/{row['script']}"
            unsupported[key] = unsupported.get(key, 0) + 1
    missing: dict[str, int] = {}
    for row in compact:
        if row["status"] == "positive" and row["target_rank"] is None:
            key = f"{row['partition']}/{row['suite']}"
            missing[key] = missing.get(key, 0) + 1
    coverage = {
        view: asdict(r.coverage)
        | {"unsupported_aliases": len(r.coverage.unsupported_aliases)}
        for view, r in retrievers.items()
    }
    return {
        "arm": arm,
        "scorer_name": next(iter(retrievers.values())).scorer_name,
        "score_note": "raw ranking signal on the arm scale; not a probability",
        "build_seconds": build_seconds,
        "coverage": coverage,
        "unsupported_queries": dict(sorted(unsupported.items())),
        "missing_target_positives": dict(sorted(missing.items())),
        "timing": {
            "scope": "retrieve() incl. query normalization/encoding, scoring and grouping; "
            "excludes reference build, labels and serialization",
            "repetitions": repetitions,
            **latency_summary(samples),
        },
        "aggregates": aggregates,
        "queries": compact,
    }


def _versions() -> dict:
    result = {}
    for name in ("rapidfuzz", "jellyfish", "anyascii", "numpy", "usearch"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


def evaluate_dataset(name: str, config: dict) -> dict:
    """Load one benchmark, keep the selected partitions, and run every arm."""
    views, queries, evidence = load_dataset(name, config)
    selected = [
        query for query in queries if query["partition"] in config["partitions"]
    ]
    audit_queries(selected, views)
    if any(not views[query["view"]] for query in selected):
        raise ValueError("empty evaluation reference")
    arms = {}
    for arm in config["arms"]:
        print(f"{name}: {arm}", flush=True)
        arms[arm] = evaluate_arm(arm, views, selected, config)
    return {
        "protocol_version": PROTOCOL,
        "baseline_protocol": BASELINE_PROTOCOL,
        "metric_protocol_version": METRIC_PROTOCOL,
        "benchmark_protocol_version": views.protocol_version,
        "dataset": name,
        "evidence": evidence,
        "regression_only": True,
        "partitions": config["partitions"],
        "requested_k": config["requested_k"],
        "query_count": len(selected),
        "query_fingerprint": fingerprint(selected),
        "view_sizes": {view: len(members) for view, members in views.items()},
        "config_fingerprint": fingerprint(config),
        "not_run": config.get("not_run", []),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": _versions(),
        },
        "arms": arms,
    }


def _show(metric: dict) -> str:
    return (
        f"{metric['value']:.4f} ({metric['numerator']:g}/{metric['denominator']})"
        if metric["value"] is not None
        else "N/A"
    )


def _find(
    aggregates: list[dict], partition: str, suite: str, slice_: str
) -> dict | None:
    return next(
        (
            a
            for a in aggregates
            if (a["partition"], a["suite"], a["slice"]) == (partition, suite, slice_)
        ),
        None,
    )


def render_report(result: dict) -> str:
    """Readable tables generated from the machine results, never hand-entered."""
    lines = [
        f"# Baseline matrix — {result['dataset']}",
        "",
        "Usage: fictional regression/mechanics evidence only. Scores are ranking signals, not probabilities; "
        "no candidate is an identity, wrongdoing or clearance finding.",
        "",
        f"Partitions: {', '.join(result['partitions'])}; requested K = {result['requested_k']}; "
        f"queries = {result['query_count']}; view sizes = {result['view_sizes']}.",
        "",
        "Recall@K is N/A when the index has K or fewer entities. MRR@K counts an unreturned target as 0.",
        "",
    ]
    for partition in result["partitions"]:
        for suite in ("challenge", "sanity"):
            lines += [
                f"## {partition} / {suite} / all",
                "",
                "| Arm | Positives | R@1 | R@10 | R@50 | MRR@K | Unsupported queries | Missing targets | "
                "Median ms | p95 ms |",
                "|---|---:|---|---|---|---|---:|---:|---:|---:|",
            ]
            for arm, data in result["arms"].items():
                row = _find(data["aggregates"], partition, suite, "all")
                if row is None or not row["query_count"]:
                    continue
                unsupported = sum(
                    v
                    for k, v in data["unsupported_queries"].items()
                    if k.startswith(f"{partition}/{suite}/")
                )
                timing = data["timing"]
                lines.append(
                    f"| {arm} | {row['positive_queries']} | "
                    + " | ".join(
                        _show(row[k])
                        for k in (
                            "recall_at_1",
                            "recall_at_10",
                            "recall_at_50",
                            "mrr_at_k",
                        )
                    )
                    + f" | {unsupported} | {data['missing_target_positives'].get(f'{partition}/{suite}', 0)} | "
                    f"{timing['median_ms']:.3f} | {timing['p95_ms']:.3f} |"
                )
            lines.append("")
        scripts = sorted(
            {
                a["slice"]
                for data in result["arms"].values()
                for a in data["aggregates"]
                if a["partition"] == partition
                and a["suite"] == "challenge"
                and a["slice"].startswith("script:")
                and a["positive_queries"]
            }
        )
        if scripts:
            lines += [
                f"## {partition} / challenge — Recall@10 by query script",
                "",
                "| Arm | " + " | ".join(scripts) + " |",
                "|---|" + "---|" * len(scripts),
            ]
            for arm, data in result["arms"].items():
                cells = [
                    _show(row["recall_at_10"])
                    if (row := _find(data["aggregates"], partition, "challenge", s))
                    else "N/A"
                    for s in scripts
                ]
                lines.append(f"| {arm} | " + " | ".join(cells) + " |")
            lines.append("")
        lines += [f"## {partition} / absent-target suggestion rate", ""]
        for arm, data in result["arms"].items():
            row = _find(data["aggregates"], partition, "absent", "all")
            if row and row["query_count"]:
                lines.append(
                    f"- {arm}: {_show(row['absent_suggestion_rate'])} (any returned candidate counts; "
                    "no threshold exists, so this is not a false-positive rate)."
                )
        lines.append("")
    lines += ["## Coverage (reference aliases each arm can score)", ""]
    for arm, data in result["arms"].items():
        parts = [
            f"{view}: {c['indexed_alias_count']}/{c['alias_count']} aliases, "
            f"{c['retrievable_entity_count']}/{c['entity_count']} entities"
            for view, c in data["coverage"].items()
        ]
        lines.append(f"- {arm}: " + "; ".join(parts))
    lines += ["", "## Not run (explicit blockers)", ""]
    lines += [f"- {item['arm']}: {item['reason']}" for item in result["not_run"]] or [
        "- none declared"
    ]
    lines.append("")
    return "\n".join(lines)


def _destinations(output_root: Path, datasets: list[str]) -> dict[str, Path]:
    """Refuse historical trees and any existing per-dataset destination."""
    root = output_root.resolve()
    project = Path(__file__).resolve().parents[2]
    for protected in (project / "eval/results", project / "data/evaluation"):
        if root == protected or root.is_relative_to(protected):
            raise ValueError(
                "historical result destinations are read-only; choose a new output root"
            )
    if root.exists() and not root.is_dir():
        raise ValueError("output root must be a directory")
    destinations = {name: root / name for name in datasets}
    if any(path.exists() or path.is_symlink() for path in destinations.values()):
        raise ValueError("output dataset destination already exists; overwrite refused")
    return destinations


def prepare_run(
    config_path: Path,
    output_root: Path,
    *,
    dataset: str | None = None,
    partition: str | None = None,
    unseal_test: bool = False,
    freeze_receipt: Path | None = None,
    arm_filter: Callable[[str, set[str]], bool] | None = None,
) -> tuple[dict, dict[str, Path]]:
    """Validate everything (schema, seal, destinations) before any dataset runs."""
    config = validate_config(json.loads(Path(config_path).read_text(encoding="utf-8")))
    if dataset is not None:
        if dataset not in DATASETS:
            raise ValueError(f"unknown dataset: {dataset}")
        config = {**config, "datasets": [dataset]}
    if partition is not None:
        config = {**config, "partitions": [partition]}
    if arm_filter is not None:
        hybrids = {variant["arm"] for variant in config.get("hybrid_variants", [])}
        arms = [arm for arm in config["arms"] if arm_filter(arm, hybrids)]
        if not arms:
            raise ValueError("no configured arm matches this command")
        config = {**config, "arms": arms}
    validate_config(config)
    check_sealed(config, unseal_test=unseal_test, freeze_receipt=freeze_receipt)
    return config, _destinations(Path(output_root), config["datasets"])


def run(config: dict, destinations: dict[str, Path]) -> None:
    """Evaluate each dataset and write results.json and report.md to its fresh destination."""
    for name in config["datasets"]:
        result = evaluate_dataset(name, config)
        output = destinations[name]
        output.mkdir(parents=True, exist_ok=False)
        with (output / "results.json").open(
            "x", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write(
                json.dumps(result, ensure_ascii=False, indent=1, allow_nan=False) + "\n"
            )
        with (output / "report.md").open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(render_report(result))
        print(f"Saved {output}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--dataset",
        choices=DATASETS,
        help="run one dataset instead of the configured list",
    )
    parser.add_argument(
        "--partition",
        choices=["development", "test"],
        help="override the configured partitions; test also needs --unseal-test",
    )
    parser.add_argument(
        "--unseal-test",
        action="store_true",
        help="explicitly unseal the final test partition",
    )
    parser.add_argument(
        "--freeze-receipt",
        type=Path,
        help="JSON receipt of the frozen development config",
    )
    args = parser.parse_args()
    try:
        config, destinations = prepare_run(
            args.config,
            args.output_root,
            dataset=args.dataset,
            partition=args.partition,
            unseal_test=args.unseal_test,
            freeze_receipt=args.freeze_receipt,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    run(config, destinations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
