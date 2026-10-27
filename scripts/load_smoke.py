"""Bounded local load smoke test of the review API on fictional data.

What: runs ``--requests`` case starts (``POST /v2/cases``) with ``--concurrency``
client threads against an **in-process** API (FastAPI ``TestClient``; no socket,
no network) over a new temporary v2 workspace, then the same number of case
reads (``GET /v2/cases/{id}``), and prints one JSON report with p50/p95/max
latency, status counts and error codes per phase.

Why: Completion Step 27 needs measured, reproducible numbers for the
single-process deployment, and evidence that overload returns structured
``429``/``503`` errors, not ``500``.

Names are packaged fictional reference names with a deterministic one-letter
deletion, so retrieval does real RapidFuzz work. The API key is generated in
memory and never printed. Results are **local fictional measurements** of this
machine and this in-process transport; they are not a capacity, latency or
production-readiness claim and say nothing about matching quality.

Usage::

    python -B scripts/load_smoke.py --requests 40 --concurrency 8
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import platform
import random
import secrets
import statistics
import sys
import tempfile
import time
from typing import Any, Callable, Sequence
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "review-v2.json"
LABEL = (
    "local in-process fictional measurement (FastAPI TestClient, no network, "
    "packaged fictional names); not a capacity, latency or production claim"
)

try:  # an installed (editable) package is preferred; fall back to the checkout
    import watchlist  # noqa: F401
except ImportError:  # pragma: no cover - only without an install
    sys.path.insert(0, str(ROOT / "src"))


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in (0, 100]); ``None`` for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100.0 * len(ordered)))
    return ordered[rank - 1]


def fictional_names(count: int, seed: int) -> list[str]:
    """Deterministic perturbed names from the packaged fictional entities."""
    from watchlist.entities import load_entities

    rng = random.Random(seed)
    base = sorted({entity.name for entity in load_entities()})
    names = []
    for index in range(count):
        name = base[index % len(base)]
        if len(name) > 4:
            cut = rng.randrange(1, len(name) - 1)
            if name[cut] != " ":
                name = name[:cut] + name[cut + 1 :]
        names.append(name)
    return names


def _phase(
    calls: Sequence[Callable[[], Any]], concurrency: int
) -> tuple[dict[str, Any], list[Any]]:
    latencies: list[float] = []
    statuses: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    responses: list[Any] = []

    def timed(call: Callable[[], Any]) -> tuple[float, Any]:
        start = time.perf_counter()
        response = call()
        return (time.perf_counter() - start) * 1000.0, response

    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(timed, calls))
    wall = time.perf_counter() - began
    for elapsed, response in results:
        latencies.append(elapsed)
        statuses[str(response.status_code)] += 1
        if response.status_code >= 400:
            try:
                codes[str(response.json().get("code"))] += 1
            except ValueError:
                codes["non_json"] += 1
        responses.append(response)
    report = {
        "requests": len(calls),
        "status_counts": dict(sorted(statuses.items())),
        "error_codes": dict(sorted(codes.items())),
        "errors": sum(n for s, n in statuses.items() if int(s) >= 400),
        "server_errors_5xx": sum(n for s, n in statuses.items() if int(s) >= 500),
        "internal_errors_500": statuses.get("500", 0),
        "latency_ms": {
            "p50": _round(percentile(latencies, 50)),
            "p95": _round(percentile(latencies, 95)),
            "max": _round(max(latencies) if latencies else None),
            "mean": _round(statistics.fmean(latencies) if latencies else None),
        },
        "wall_seconds": round(wall, 3),
        "throughput_rps": round(len(calls) / wall, 2) if wall > 0 else None,
    }
    return report, responses


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


def run(
    requests: int = 40,
    concurrency: int = 8,
    *,
    config: Path | str = DEFAULT_CONFIG,
    write_queue_max: int | None = None,
    max_in_flight: int | None = None,
    seed: int = 0,
    workspace_parent: Path | str | None = None,
) -> dict[str, Any]:
    """Run both phases on a new temporary workspace and return the report."""
    if not 1 <= requests <= 10000 or not 1 <= concurrency <= 256:
        raise ValueError("requests must be 1..10000 and concurrency 1..256")
    from fastapi.testclient import TestClient

    from watchlist.api import ApiSettings, create_app
    from watchlist.api.auth import ApiKey, hash_key
    from watchlist.review.service import ReviewService

    token = secrets.token_urlsafe(32)
    headers = {"Authorization": f"Bearer {token}"}
    options: dict[str, Any] = {}
    if write_queue_max is not None:
        options["write_queue_max"] = write_queue_max
    if max_in_flight is not None:
        options["max_in_flight"] = max_in_flight
    settings = ApiSettings(
        api_keys=(ApiKey("fictional-load-reviewer", "reviewer", hash_key(token)),),
        **options,
    )
    names = fictional_names(requests, seed)
    with tempfile.TemporaryDirectory(
        prefix="watchlist-load-", dir=workspace_parent, ignore_cleanup_errors=True
    ) as tmp:
        service = ReviewService.initialize(Path(tmp) / "ws", Path(config))
        app = create_app(settings, service=service)
        with TestClient(app) as client:
            # Warm-up (lazy retriever and graph construction), not measured.
            warm = client.post(
                "/v2/cases",
                json={"name": "Luma Vesper", "origin": "test"},
                headers=headers,
            )
            if warm.status_code != 200:
                raise RuntimeError(f"warm-up failed with HTTP {warm.status_code}")
            start_calls = [
                (
                    lambda name=name: client.post(
                        "/v2/cases",
                        json={
                            "name": name,
                            "origin": "test",
                            "request_id": str(uuid4()),
                        },
                        headers=headers,
                    )
                )
                for name in names
            ]
            start_report, responses = _phase(start_calls, concurrency)
            case_ids = [
                r.json()["case"]["case_id"] for r in responses if r.status_code == 200
            ] or [warm.json()["case"]["case_id"]]
            read_calls = [
                (
                    lambda case_id=case_ids[i % len(case_ids)]: client.get(
                        f"/v2/cases/{case_id}", headers=headers
                    )
                )
                for i in range(requests)
            ]
            read_report, _ = _phase(read_calls, concurrency)
            metrics = client.get("/metrics", headers=headers).text
    return {
        "label": LABEL,
        "config": {
            "requests_per_phase": requests,
            "concurrency": concurrency,
            "write_queue_max": settings.write_queue_max,
            "max_in_flight": settings.max_in_flight,
            "retriever": "rapidfuzz over packaged fictional entities",
            "seed": seed,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(terse=True),
            "cpu_count": os.cpu_count(),
        },
        "phases": {"start_case": start_report, "get_case": read_report},
        "trace_records_dropped": _metric_total(
            metrics, "watchlist_trace_records_dropped_total"
        ),
    }


def _metric_total(text: str, name: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith(name + " ") or line.startswith(name + "{"):
            total += float(line.rsplit(" ", 1)[1])
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -B scripts/load_smoke.py")
    parser.add_argument("--requests", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--write-queue-max", type=int, default=None)
    parser.add_argument("--max-in-flight", type=int, default=None)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    report = run(
        args.requests,
        args.concurrency,
        config=args.config,
        write_queue_max=args.write_queue_max,
        max_in_flight=args.max_in_flight,
        seed=args.seed,
    )
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True))
    phases = report["phases"].values()
    # Structured load shedding (429, 503 workspace_busy) is expected under
    # overload; an unstructured 500 is a failure.
    return 1 if any(p["internal_errors_500"] for p in phases) else 0


if __name__ == "__main__":
    raise SystemExit(main())
