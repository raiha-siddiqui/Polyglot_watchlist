"""Offline Step 18 harness: retrieval shortlist versus an LLM judge on equal evidence.

The retriever's shortlist is frozen first. The LLM judge sees exactly that
shortlist (ordered by entity ID, each record's display name and all aliases,
baseline scores and the baseline's chosen alias omitted by default), so it can
never be credited for a record retrieval missed; misses are reported
separately. Replay mode uses recorded receipts only. Live mode is refused
unless both the comparison config (including ``live.approved_partition``) and
the LLM config approve it and ``--allow-live`` is passed; each paid receipt is
appended to ``receipts.jsonl`` as soon as it returns. Fake-fixture responses
are labelled as such and never reported as LLM results. Reports over the
packaged fictional records are always ``regression_only``.

Commands::

    python -m watchlist.llm.comparison preflight --config eval/configs/llm-comparison.json
    python -m watchlist.llm.comparison replay --config ... --receipts FILE [--receipts-sha256 HEX] --output DIR
    python -m watchlist.llm.comparison run --config ... --output DIR --allow-live
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

from watchlist.entities import load_entities
from watchlist.llm import RESEARCH_LIMITATION, prompts
from watchlist.llm.client import (
    AnthropicClient,
    LLMClient,
    LLMConfig,
    RecordingClient,
    ReplayClient,
    call_model,
    load_config,
    make_receipt,
)
from watchlist.llm.schemas import LLMError, ValidationFailure, canonical, sha256_text
from watchlist.llm.validation import strict_json
from watchlist.matching import SCORER_NAME, rank_candidates

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = ROOT / "eval" / "configs" / "llm-comparison.json"
REPORT_SCHEMA = "llm-comparison-report-v2"
# Reference sets this offline harness can load, and the partitions each allows.
# The packaged records are fictional, so every report over them is regression-only.
REFERENCE_PARTITIONS = {"packaged-synthetic-v1": ("fictional-regression-only",)}
REGRESSION_ONLY_REFERENCES = frozenset({"packaged-synthetic-v1"})


@dataclass(frozen=True)
class Case:
    query_id: str
    text: str
    target_entity_ids: tuple[str, ...]


@dataclass(frozen=True)
class ComparisonConfig:
    path: Path
    sha256: str
    llm_config_path: Path
    reference: str
    partition: str
    top_k: int
    include_baseline_scores: bool
    live_enabled: bool
    approved_partition: str | None
    cases: tuple[Case, ...]

    @property
    def regression_only(self) -> bool:
        """Fixed by the reference set, never by a free-text label."""
        return self.reference in REGRESSION_ONLY_REFERENCES


def load_comparison_config(path: Path) -> ComparisonConfig:
    """Strictly validate the comparison config; unknown retrievers are rejected."""
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("schema_version") != "llm-comparison-v1":
        raise ValueError("unsupported comparison config schema")
    if (
        data.get("retriever") != "rapidfuzz-baseline"
        or data.get("reference") != "packaged-synthetic-v1"
    ):
        raise ValueError(
            "only the fictional rapidfuzz-baseline harness is implemented offline"
        )
    partition = data.get("partition")
    if partition not in REFERENCE_PARTITIONS[data["reference"]]:
        raise ValueError(
            f"partition must be one of {REFERENCE_PARTITIONS[data['reference']]}"
        )
    if data.get("shortlist_order") != "entity_id":
        raise ValueError(
            "shortlist_order must be entity_id so baseline rank is not leaked"
        )
    top_k, max_cases = data.get("top_k"), data.get("max_cases")
    if (
        type(top_k) is not int
        or not 1 <= top_k <= 20
        or type(max_cases) is not int
        or max_cases < 1
    ):
        raise ValueError("top_k must be 1..20 and max_cases positive")
    queries = data.get("queries")
    if not isinstance(queries, list) or not 1 <= len(queries) <= max_cases:
        raise ValueError(f"queries must contain 1..{max_cases} cases")
    cases, ids = [], set()
    for q in queries:
        if not isinstance(q, dict) or set(q) != {
            "query_id",
            "text",
            "target_entity_ids",
        }:
            raise ValueError("query fields mismatch")
        targets = q["target_entity_ids"]
        if (
            not isinstance(q["query_id"], str)
            or q["query_id"] in ids
            or not isinstance(q["text"], str)
            or not isinstance(targets, list)
            or not all(isinstance(t, str) for t in targets)
        ):
            raise ValueError("invalid or duplicate query")
        ids.add(q["query_id"])
        cases.append(Case(q["query_id"], q["text"], tuple(sorted(set(targets)))))
    live = data.get("live") or {}
    if (
        type(data.get("include_baseline_scores")) is not bool
        or type(live.get("enabled")) is not bool
    ):
        raise ValueError("include_baseline_scores and live.enabled must be booleans")
    approved = live.get("approved_partition")
    if approved is not None and not isinstance(approved, str):
        raise ValueError("live.approved_partition must be a string or null")
    llm_path = Path(data.get("llm_config", "config/llm.json"))
    return ComparisonConfig(
        path=path,
        sha256=sha256_text(raw),
        llm_config_path=llm_path if llm_path.is_absolute() else ROOT / llm_path,
        reference=data["reference"],
        partition=partition,
        top_k=top_k,
        include_baseline_scores=data["include_baseline_scores"],
        live_enabled=live["enabled"],
        approved_partition=approved,
        cases=tuple(cases),
    )


def shortlist(case: Case, entities, top_k: int) -> list[dict]:
    """Baseline candidates in score order (ties broken by entity ID inside rank_candidates)."""
    aliases = {
        e.entity_id: [a.name for a in sorted(e.aliases, key=lambda a: a.alias_id)]
        for e in entities
    }
    return [
        {
            "entity_id": c.entity_id,
            "display_name": c.display_name,
            "entity_type": c.entity_type,
            "aliases": aliases[c.entity_id],
            "matched_alias_id": c.matched_alias_id,
            "matched_alias": c.matched_alias,
            "similarity_score": c.similarity_score,
            "baseline_rank": rank,
        }
        for rank, c in enumerate(rank_candidates(case.text, entities, top_k), 1)
    ]


def judge_view(case: Case, candidates: list[dict], include_scores: bool) -> dict:
    """Equal evidence for the judge: the same shortlist, in entity-ID order.

    Each record shows all its aliases in alias-ID order, not the alias the
    baseline scored highest, so no baseline choice leaks to the judge.
    """
    keys = ["entity_id", "display_name", "entity_type", "aliases"]
    if include_scores:
        keys.append("similarity_score")
    items = [
        {("id" if k == "entity_id" else k): c[k] for k in keys}
        for c in sorted(candidates, key=lambda c: c["entity_id"])
    ]
    return {"query": case.text, "shortlist": items}


def validate_judgement(
    text: object, candidate_ids: list[str], max_chars: int
) -> tuple[list[str], bool]:
    """Accept only a full permutation of the shortlist plus a boolean abstain flag."""
    data = strict_json(text, max_chars)
    if set(data) != {"ranking", "abstain"} or type(data["abstain"]) is not bool:
        raise LLMError(
            "wrong_schema", "judge output must have exactly ranking and abstain"
        )
    ranking = data["ranking"]
    if (
        not isinstance(ranking, list)
        or not all(isinstance(r, str) for r in ranking)
        or sorted(ranking) != sorted(candidate_ids)
        or len(set(ranking)) != len(ranking)
    ):
        raise LLMError(
            "invalid_ranking", "ranking must list every shortlist ID exactly once"
        )
    return ranking, data["abstain"]


def _first_rank(order: list[str], targets: tuple[str, ...]) -> int | None:
    ranks = [n for n, entity_id in enumerate(order, 1) if entity_id in targets]
    return min(ranks) if ranks else None


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None when nothing was measured."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def judge_requests(
    config: ComparisonConfig, llm_config: LLMConfig
) -> list[tuple[Case, list[dict], dict, object]]:
    """Render every case's frozen shortlist, judge view and request without calling anything."""
    entities = load_entities()
    rendered = []
    for case in config.cases:
        candidates = shortlist(case, entities, config.top_k)
        view = judge_view(case, candidates, config.include_baseline_scores)
        rendered.append(
            (
                case,
                candidates,
                view,
                prompts.judge_request(view, llm_config.max_output_tokens["judge"]),
            )
        )
    return rendered


def run_cases(
    config: ComparisonConfig, client: LLMClient, llm_config: LLMConfig
) -> tuple[list[dict], list[dict]]:
    """Return per-case records and receipts. No case result is ever imputed."""
    records, receipts = [], []
    for case, candidates, view, request in judge_requests(config, llm_config):
        baseline_order = [c["entity_id"] for c in candidates]
        record = {
            "query_id": case.query_id,
            "target_entity_ids": list(case.target_entity_ids),
            "absent_query": not case.target_entity_ids,
            "baseline_order": baseline_order,
            "baseline_target_rank": _first_rank(baseline_order, case.target_entity_ids),
            "retrieval_miss": bool(case.target_entity_ids)
            and not set(case.target_entity_ids) & set(baseline_order),
            "request_sha256": request.digest(),
            "judge_evidence_sha256": sha256_text(canonical(view)),
            "llm_status": None,
            "llm_order": None,
            "llm_abstain": None,
            "llm_target_rank": None,
            "llm_call": None,
        }
        response, error = call_model(client, request)
        if response is not None:
            record["llm_call"] = response.accounting()
            if response.evidence_kind == "live_provider":
                receipts.append(
                    make_receipt(request, response, datetime.now(UTC).isoformat())
                )
        if error is None:
            try:
                order, abstain = validate_judgement(
                    response.text, baseline_order, llm_config.max_output_chars
                )
            except (LLMError, ValidationFailure, ValueError) as exc:
                error = f"invalid_output:{getattr(exc, 'code', type(exc).__name__)}"
            else:
                record.update(
                    llm_status="valid",
                    llm_order=order,
                    llm_abstain=abstain,
                    llm_target_rank=_first_rank(order, case.target_entity_ids),
                )
        if error:
            record["llm_status"] = error
        records.append(record)
    return records, receipts


def summarize(records: list[dict]) -> dict:
    """Aggregate counts with explicit denominators; unmeasured fields stay None."""
    with_target = [r for r in records if not r["absent_query"]]
    retrieved = [r for r in with_target if not r["retrieval_miss"]]
    valid = [r for r in retrieved if r["llm_status"] == "valid"]
    calls = [r["llm_call"] for r in records if r["llm_call"]]
    kinds = sorted({c["evidence_kind"] for c in calls})

    def total(key: str) -> float | None:
        values = [c[key] for c in calls]
        return (
            None
            if not values or any(v is None for v in values)
            else round(sum(values), 8)
        )

    latencies = [c["latency_ms"] for c in calls if c["latency_ms"] is not None]
    errors: dict[str, int] = {}
    for r in records:
        if r["llm_status"] != "valid":
            errors[r["llm_status"]] = errors.get(r["llm_status"], 0) + 1
    return {
        "cases": len(records),
        "cases_with_target": len(with_target),
        "absent_queries": len(records) - len(with_target),
        "retrieval_misses": len(with_target) - len(retrieved),
        "baseline_top1_given_retrieved": {
            "hits": sum(r["baseline_target_rank"] == 1 for r in retrieved),
            "denominator": len(retrieved),
        },
        "llm_valid_outputs": {
            "count": sum(r["llm_status"] == "valid" for r in records),
            "denominator": len(records),
        },
        "llm_top1_given_retrieved_and_valid": {
            "hits": sum(r["llm_target_rank"] == 1 for r in valid),
            "denominator": len(valid),
        },
        "llm_abstentions": sum(bool(r["llm_abstain"]) for r in records),
        "llm_errors": dict(sorted(errors.items())),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cost_usd_estimate": total("cost_usd"),
        "cost_usd_per_1000_queries": (
            None
            if total("cost_usd") is None or not records
            else round(total("cost_usd") * 1000 / len(records), 6)
        ),
        "latency_ms": {
            "measured_calls": len(latencies),
            "median": statistics.median(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
        },
        "evidence_kinds": kinds,
    }


def status_for(summary: dict, regression_only: bool = True) -> str:
    """Honest status label. Only replayed/live provider output can count as a result,
    and a run over fictional reference data is never reported as a real comparison."""
    if summary["llm_valid_outputs"]["count"] == 0 and summary["llm_errors"]:
        return "BLOCKED_no_llm_outputs"
    if "fake_fixture" in summary["evidence_kinds"]:
        return "fixture_only_not_an_llm_result"
    if summary["llm_errors"]:
        return "incomplete_with_recorded_llm_errors"
    if regression_only:
        return "regression_only_not_a_comparison_result"
    return "complete_on_this_partition"


def build_report(
    config: ComparisonConfig,
    llm_config: LLMConfig,
    mode: str,
    records: list[dict],
    receipts_meta: dict | None = None,
) -> dict:
    """Pure report construction; it runs before anything is written."""
    summary = summarize(records)
    return {
        "schema_version": REPORT_SCHEMA,
        "mode": mode,
        "status": status_for(summary, config.regression_only),
        "reference": config.reference,
        "partition": config.partition,
        "regression_only": config.regression_only,
        "config_sha256": config.sha256,
        "llm_config_sha256": llm_config.sha256,
        "model": llm_config.model,
        "prompt_id": prompts.JUDGE_PROMPT_ID,
        "prompt_sha256": prompts.PROMPT_SHA256[prompts.JUDGE_PROMPT_ID],
        "retriever": {
            "name": "rapidfuzz-baseline",
            "scorer": SCORER_NAME,
            "top_k": config.top_k,
        },
        "equal_evidence": {
            "shortlist_order": "entity_id",
            "aliases_shown": "all, alias_id order",
            "baseline_scores_shown": config.include_baseline_scores,
        },
        "score_semantics": {
            "baseline": "normalized Levenshtein similarity x100; not a probability",
            "llm": "ordinal ranking only; no confidence requested; not calibrated",
        },
        "receipts": receipts_meta or {"source": None},
        "summary": summary,
        "limitations": RESEARCH_LIMITATION,
    }


def _write_outputs(output: Path, report: dict, records: list[dict]) -> None:
    with (output / "report.json").open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    with (output / "cases.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(canonical(record) + "\n")


def write_report(
    output: Path,
    config: ComparisonConfig,
    llm_config: LLMConfig,
    mode: str,
    records: list[dict],
    receipts: list[dict],
    receipts_meta: dict | None = None,
) -> dict:
    """Build the report first, then write into a fresh directory; an existing path is never overwritten."""
    report = build_report(config, llm_config, mode, records, receipts_meta)
    output.mkdir(parents=True, exist_ok=False)
    _write_outputs(output, report, records)
    if receipts:
        with (output / "receipts.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            for receipt in receipts:
                handle.write(canonical(receipt) + "\n")
    return report


def preflight(config: ComparisonConfig, llm_config: LLMConfig) -> dict:
    """Describe what a live run would transmit, its worst-case cost and why it is (not) allowed. No network."""
    blockers = list(llm_config.live_blockers())
    if not config.live_enabled:
        blockers.insert(0, "comparison live.enabled is false")
    if config.approved_partition != config.partition:
        blockers.append(
            f"live.approved_partition {config.approved_partition!r} != partition {config.partition!r}"
        )
    needed = len(config.cases)
    if llm_config.max_requests_per_run < needed:
        blockers.append(
            f"max_requests_per_run {llm_config.max_requests_per_run} < {needed} cases"
        )
    requests = [r for *_, r in judge_requests(config, llm_config)]
    largest = max(r.size_chars() for r in requests)
    if largest > llm_config.max_request_chars:
        blockers.append(
            f"largest request {largest} chars > max_request_chars {llm_config.max_request_chars}"
        )
    worst = round(sum(llm_config.worst_case_cost(r) for r in requests), 8)
    budget = llm_config.approval.budget_usd
    if budget is not None and worst > budget:
        blockers.append(f"worst-case cost {worst} USD > budget {budget} USD")
    return {
        "cases": needed,
        "model": llm_config.model,
        "reference": config.reference,
        "partition": config.partition,
        "regression_only": config.regression_only,
        "transmitted_fields": [
            "query text",
            "shortlist entity_id",
            "display_name",
            "entity_type",
            "aliases",
        ]
        + (["similarity_score"] if config.include_baseline_scores else []),
        "max_output_tokens_per_call": llm_config.max_output_tokens["judge"],
        "largest_request_chars": largest,
        "worst_case_cost_usd": worst,
        "budget_usd": budget,
        "live_allowed": not blockers,
        "blockers": blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m watchlist.llm.comparison")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "replay", "run"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if name != "preflight":
            p.add_argument("--output", type=Path, required=True)
        if name == "replay":
            p.add_argument("--receipts", type=Path, required=True)
            p.add_argument(
                "--receipts-sha256",
                help="pin the receipts file to the hash recorded by the live run",
            )
        if name == "run":
            p.add_argument("--allow-live", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_comparison_config(args.config)
        llm_config = load_config(config.llm_config_path)
        if args.command == "preflight":
            print(
                json.dumps(preflight(config, llm_config), indent=2, ensure_ascii=False)
            )
            return 0
        if args.output.exists():
            raise ValueError("output directory must not already exist")
        if args.command == "replay":
            file_sha = hashlib.sha256(args.receipts.read_bytes()).hexdigest()
            if (
                args.receipts_sha256 is not None
                and args.receipts_sha256.lower() != file_sha
            ):
                raise LLMError(
                    "invalid_receipt", "receipts file does not match the pinned SHA-256"
                )
            client: LLMClient = ReplayClient.from_file(args.receipts, llm_config.model)
            records, _ = run_cases(config, client, llm_config)
            meta = {
                "source": "replay",
                "file_sha256": file_sha,
                "pinned": args.receipts_sha256 is not None,
                "authentication": "sha256 integrity only; origin not authenticated",
            }
            report = write_report(
                args.output, config, llm_config, "replay", records, [], meta
            )
        else:
            check = preflight(config, llm_config)
            if not args.allow_live or not check["live_allowed"]:
                reasons = check["blockers"] + (
                    [] if args.allow_live else ["--allow-live not passed"]
                )
                raise LLMError("live_blocked", "; ".join(reasons))
            live = AnthropicClient(llm_config, allow_live=True)
            # Create the output first so a failure here costs nothing, then keep each paid receipt.
            args.output.mkdir(parents=True, exist_ok=False)
            receipts_path = args.output / "receipts.jsonl"
            records, _ = run_cases(
                config, RecordingClient(live, receipts_path), llm_config
            )
            file_sha = (
                hashlib.sha256(receipts_path.read_bytes()).hexdigest()
                if receipts_path.exists()
                else None
            )
            meta = {
                "source": "live",
                "file": "receipts.jsonl",
                "file_sha256": file_sha,
                "authentication": "sha256 integrity only; pin file_sha256 when replaying",
            }
            report = build_report(config, llm_config, "live", records, meta)
            _write_outputs(args.output, report, records)
    except (LLMError, ValueError, OSError) as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(args.output),
                "summary": report["summary"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
