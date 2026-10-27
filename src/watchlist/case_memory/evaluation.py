"""Run the authored fictional memory episodes through the real v2 service.

``python -m watchlist.case_memory.evaluation --config eval/configs/memory.json``

For every episode a fresh temporary v2 workspace and memory store are created.
History cases are started and reviewed through ``ReviewService`` with a
scripted retriever (authored candidates over registered fictional snapshots),
published with ``Publisher``, corrected with ``lifecycle``; then the query case
is started twice, with and without ``MemoryContextProvider``. The frozen
memory context is compared with the hand-written oracle in the fixture.

Metrics (denominators are reported with every value):

* ``displayed_links``: cards shown across all query keys;
* ``wrong_links``: shown cards whose earlier case is not in the oracle for
  that exact key (any wrong namespace/ID/snapshot/source link lands here);
* ``link_precision`` = correct / displayed (None when nothing was displayed);
* ``recall_at_cap`` = expected cards shown / expected cards (cap = policy);
* ``order_exact``: keys whose shown order equals the oracle order;
* ``stale_exposures``: shown cards the oracle marks ineligible (withdrawn,
  superseded, expired);
* ``ranking_parity``: queries whose retrieval result is identical with
  memory on and off;
* ``lookup_ms``: provider wall time (machine-local; not a performance claim).

This tests software behaviour on 12 correlated fictional groups. It is not
evidence about reviewer benefit, real-world identity or matching quality.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time
from typing import Any, Sequence
from uuid import uuid4

from watchlist.case_memory.lifecycle import supersede, withdraw
from watchlist.case_memory.lookup import check_freshness
from watchlist.case_memory.provider import MemoryContextProvider, memory_context
from watchlist.case_memory.publisher import Publisher
from watchlist.case_memory.state import MemoryConfig, iso, parse_time
from watchlist.case_memory.store import MemoryStore
from watchlist.review.contracts import (
    AliasEvidence,
    Candidate,
    RetrievalResult,
    RetrieverDescriptor,
    ReviewConfig,
    digest,
    loads_strict,
)

ROOT = Path(__file__).resolve().parents[3]


def packaged_snapshot() -> str:
    """Fingerprint of the packaged fictional entities as the v2 adapter digests them."""
    from watchlist.entities import load_entities
    from watchlist.review.nodes import reference_digest

    return reference_digest(load_entities())


def authored_snapshot(key: str) -> str:
    return digest(["memory-episodes-v1", key])


class EpisodeClock:
    """Deterministic clock: ``base + day`` plus a millisecond counter that never resets."""

    def __init__(self, base: str):
        self._base = parse_time(base, "base_time")
        self._day = 0
        self._tick = 0

    def set_day(self, day: int) -> None:
        self._day = day  # ticks keep increasing so times stay ordered within a day

    def __call__(self) -> str:
        self._tick += 1
        return iso(self._base + timedelta(days=self._day, milliseconds=self._tick))


class ScriptedRetriever:
    """Authored candidates for one fictional snapshot (test/evaluation adapter).

    ``script`` maps a normalized query name to candidate entity IDs in rank
    order. Scores are authored rank placeholders (``authored_rank_score``),
    not similarities. Evidence ``source`` is the snapshot's declared label.
    """

    def __init__(
        self,
        snapshot_sha256: str,
        *,
        evidence_source: str,
        names: dict[str, str],
        tag: str,
    ):
        self.script: dict[str, list[str]] = {}
        self.calls = 0
        self._source = evidence_source
        self._names = names
        self._descriptor = RetrieverDescriptor(
            "scripted",
            "memory-episode-retriever",
            f"scripted-{tag}",
            snapshot_sha256,
            {"snapshot": tag, "score_kind": "authored_rank_score"},
        )

    def descriptor(self) -> RetrieverDescriptor:
        return self._descriptor

    def search(self, normalized_name: str, *, top_k: int) -> RetrievalResult:
        self.calls += 1
        candidates, evidence = [], []
        for rank, entity_id in enumerate(self.script[normalized_name][:top_k], 1):
            alias_id = f"{entity_id}-a1"
            evidence_id = f"{entity_id}/{alias_id}"
            name = self._names[entity_id]
            evidence.append(
                AliasEvidence(
                    evidence_id,
                    entity_id,
                    alias_id,
                    name,
                    self._source,
                    True,
                    "Authored fictional fixture; not real persons or organizations.",
                    None,
                )
            )
            candidates.append(
                Candidate(
                    rank,
                    entity_id,
                    name,
                    "person",
                    alias_id,
                    name,
                    float(100 - 5 * rank),
                    "authored_rank_score",
                    (evidence_id,),
                )
            )
        return RetrievalResult(
            self._descriptor, top_k, tuple(candidates), tuple(evidence)
        )


class TimedProvider:
    """Wraps the memory provider to record lookup wall time."""

    def __init__(self, inner: MemoryContextProvider):
        self.inner = inner
        self.name, self.version = inner.name, inner.version
        self.durations_ms: list[float] = []

    def provide(self, request):
        started = time.perf_counter()
        try:
            return self.inner.provide(request)
        finally:
            self.durations_ms.append((time.perf_counter() - started) * 1000)


def build_memory_config(base: MemoryConfig, fixture: dict[str, Any]) -> MemoryConfig:
    """``config/memory.json`` plus the fixture's adversarial namespaces/snapshots."""
    data = base.to_dict()
    data["namespaces"] = data["namespaces"] + fixture["extra_namespaces"]
    for key, spec in fixture["snapshots"].items():
        if spec["namespace_id"] is None or spec["fingerprint"] == "packaged":
            continue
        data["snapshots"].append(
            {
                "snapshot_sha256": authored_snapshot(key),
                "namespace_id": spec["namespace_id"],
                "label": f"Authored episode snapshot {key}",
            }
        )
    return MemoryConfig.from_dict(data)


@dataclass
class Totals:
    episodes: int = 0
    query_keys: int = 0
    displayed_links: int = 0
    correct_links: int = 0
    wrong_links: int = 0
    episodes_with_wrong_link: int = 0
    expected_cards: int = 0
    expected_cards_shown: int = 0
    order_exact: int = 0
    stale_exposures: int = 0
    ranking_parity: int = 0
    queries: int = 0
    failures: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        return {
            "episodes": self.episodes,
            "queries": self.queries,
            "query_keys": self.query_keys,
            "displayed_links": self.displayed_links,
            "correct_links": self.correct_links,
            "wrong_links": self.wrong_links,
            "link_precision": (
                None
                if not self.displayed_links
                else round(self.correct_links / self.displayed_links, 6)
            ),
            "episodes_with_wrong_link": self.episodes_with_wrong_link,
            "expected_cards": self.expected_cards,
            "expected_cards_shown": self.expected_cards_shown,
            "recall_at_cap": (
                None
                if not self.expected_cards
                else round(self.expected_cards_shown / self.expected_cards, 6)
            ),
            "keys_with_exact_order": self.order_exact,
            "stale_exposures": self.stale_exposures,
            "ranking_parity": f"{self.ranking_parity}/{self.queries}",
            "failures": list(self.failures),
        }


class EpisodeRunner:
    """Executes one episode in its own temporary workspace and memory store."""

    def __init__(
        self,
        fixture: dict[str, Any],
        review_config: ReviewConfig,
        memory_config: MemoryConfig,
        workspace_id: str,
        scratch: Path,
    ):
        from watchlist.entities import load_entities

        self.fixture = fixture
        self.review_config = review_config
        self.memory_config = memory_config
        self.workspace_id = workspace_id
        self.scratch = scratch
        packaged = {e.entity_id: e.name for e in load_entities()}
        self.names = {"packaged": packaged, "alt": dict(fixture["alt_names"])}
        self.main_snapshot = packaged_snapshot()

    def _snapshot_sha(self, key: str) -> str:
        spec = self.fixture["snapshots"][key]
        return (
            self.main_snapshot
            if spec["fingerprint"] == "packaged"
            else authored_snapshot(key)
        )

    def run(self, episode: dict[str, Any]) -> dict[str, Any]:
        from watchlist.review.service import ReviewService

        eid = episode["episode_id"]
        base = self.scratch / eid
        clock = EpisodeClock(self.fixture["base_time"])
        store = MemoryStore.initialize(base / "memory", self.memory_config, clock=clock)
        provider = TimedProvider(MemoryContextProvider(store, clock=clock))
        workspace = base / "review"
        ReviewService.initialize(workspace, self.review_config, clock=clock)
        retrievers: dict[str, ScriptedRetriever] = {}
        services: dict[tuple[str, bool], Any] = {}

        def service(snapshot: str, with_memory: bool):
            if snapshot not in retrievers:
                spec = self.fixture["snapshots"][snapshot]
                retrievers[snapshot] = ScriptedRetriever(
                    self._snapshot_sha(snapshot),
                    evidence_source=spec["evidence_source"],
                    names=self.names[spec["names"]],
                    tag=snapshot,
                )
            key = (snapshot, with_memory)
            if key not in services:
                services[key] = ReviewService.open(
                    workspace,
                    self.review_config,
                    retriever=retrievers[snapshot],
                    context_providers=[provider] if with_memory else [],
                    clock=clock,
                )
            return services[key]

        def run_case(spec: dict[str, Any], label: str, *, with_memory: bool = False):
            snapshot = spec.get("snapshot", "main")
            name = f"Episode {eid} case {label}"
            svc = service(snapshot, with_memory)
            from watchlist.normalize import normalize_name

            retrievers[snapshot].script[normalize_name(name)] = list(spec["candidates"])
            clock.set_day(spec["day"])
            view = svc.start_case(
                name, origin=spec["origin"], actor="fictional-eval-reviewer"
            )
            return svc, view

        labels: dict[str, str] = {}
        full, lagging = [], []
        for spec in episode["history"]:
            svc, view = run_case(spec, spec["label"])
            view = svc.submit_review(
                view.case_id,
                {
                    "submission_id": str(uuid4()),
                    "packet_sha256": view.packet_sha256,
                    "outcome": spec["outcome"],
                    "candidate_entity_id": spec["selected"],
                    "reason": spec["reason"],
                    "actor": "fictional-eval-reviewer",
                    "origin": spec["origin"],
                },
            )
            labels[view.case_id] = spec["label"]
            (lagging if spec.get("publish") == "enqueue_only" else full).append(
                view.case_id
            )
        reader = ReviewService.open(workspace, self.review_config, read_only=True)
        publisher = Publisher(store, reader, self.workspace_id)
        report = publisher.publish(case_ids=full)
        quarantined = len(report.quarantined)
        if lagging:
            publisher.enqueue(case_ids=lagging)
        by_label = {v: k for k, v in labels.items()}
        for action in episode.get("lifecycle", []):
            clock.set_day(action["day"])
            receipt = publisher.receipt(by_label[action["case"]])
            if action["action"] == "withdraw":
                withdraw(
                    store,
                    receipt["annotation_id"],
                    actor="fictional-maintainer",
                    reason=action["reason"],
                )
            else:
                replacement = publisher.receipt(by_label[action["replacement"]])
                supersede(
                    store,
                    receipt["annotation_id"],
                    replacement["annotation_id"],
                    actor="fictional-maintainer",
                    reason=action["reason"],
                )
        query = episode["query"]
        _, on = run_case(query, "query", with_memory=True)
        _, off = run_case(query, "query-off", with_memory=False)
        parity = on.packet.retrieval.candidates == off.packet.retrieval.candidates and (
            on.packet.retrieval.evidence == off.packet.retrieval.evidence
        )
        context = memory_context(on.packet)
        fresh_after = None
        if episode.get("post_query"):
            ids = []
            for spec in episode["post_query"]:
                svc, view = run_case(spec, spec["label"])
                view = svc.submit_review(
                    view.case_id,
                    {
                        "submission_id": str(uuid4()),
                        "packet_sha256": view.packet_sha256,
                        "outcome": spec["outcome"],
                        "candidate_entity_id": spec["selected"],
                        "reason": spec["reason"],
                        "actor": "fictional-eval-reviewer",
                        "origin": spec["origin"],
                    },
                )
                ids.append(view.case_id)
            publisher.publish(case_ids=ids)
            fresh_after = check_freshness(store, context).fresh
        verification = store.verify()
        return {
            "episode": episode,
            "labels": labels,
            "context": context,
            "parity": parity,
            "quarantined": quarantined,
            "fresh_after": fresh_after,
            "lookup_ms": provider.durations_ms,
            "memory_ok": verification.ok,
        }


def score_episode(result: dict[str, Any], totals: Totals) -> list[str]:
    """Compare one episode's frozen context with its hand-written oracle."""
    episode, labels, context = result["episode"], result["labels"], result["context"]
    eid = episode["episode_id"]
    problems: list[str] = []
    totals.episodes += 1
    totals.queries += 1
    totals.ranking_parity += int(result["parity"])
    if not result["parity"]:
        problems.append(f"{eid}: ranking differs with memory on/off")
    if not result["memory_ok"]:
        problems.append(f"{eid}: memory verification failed")
    expected_status = episode.get("expected_status", "ok")
    if context["status"] != expected_status:
        problems.append(f"{eid}: status {context['status']} != {expected_status}")
    if expected_status == "ok" and context.get(
        "pending_publications", 0
    ) != episode.get("expected_pending", 0):
        problems.append(f"{eid}: pending publications differ")
    if result["quarantined"] != episode.get("expected_quarantined", 0):
        problems.append(f"{eid}: quarantined {result['quarantined']}")
    if (
        "expected_fresh_after" in episode
        and result["fresh_after"] != episode["expected_fresh_after"]
    ):
        problems.append(f"{eid}: freshness check returned {result['fresh_after']}")
    stale = set(episode.get("stale_labels", []))
    wrong_before = totals.wrong_links
    keys = {k["source_entity_id"]: k for k in context.get("keys", [])}
    if set(keys) != set(episode["expected"]):
        problems.append(f"{eid}: looked-up keys differ from the oracle")
    for entity_id, expected in episode["expected"].items():
        key = keys.get(entity_id)
        if key is None:
            continue
        totals.query_keys += 1
        shown = [labels.get(card["case_id"], "<unknown case>") for card in key["cards"]]
        totals.displayed_links += len(shown)
        for label in shown:
            if label in expected:
                totals.correct_links += 1
            else:
                totals.wrong_links += 1
                problems.append(f"{eid}: wrong link {label} under {entity_id}")
            if label in stale:
                totals.stale_exposures += 1
        eligible = episode.get("expected_eligible", {}).get(entity_id, expected)
        totals.expected_cards += len(expected)
        totals.expected_cards_shown += len(set(shown) & set(expected))
        if shown == expected:
            totals.order_exact += 1
        else:
            problems.append(f"{eid}: {entity_id} shown {shown} != expected {expected}")
        if key["eligible_count"] != len(eligible):
            problems.append(
                f"{eid}: {entity_id} eligible_count {key['eligible_count']}"
            )
        for reason, count in (
            episode.get("expected_omitted", {}).get(entity_id, {}).items()
        ):
            if key["omitted"][reason] != count:
                problems.append(
                    f"{eid}: {entity_id} omitted[{reason}] = {key['omitted'][reason]}"
                )
        if entity_id in episode.get("expected_differing", {}):
            if key["differing_outcomes"] != episode["expected_differing"][entity_id]:
                problems.append(f"{eid}: {entity_id} differing flag")
        for label, relation in (
            episode.get("expected_relations", {}).get(entity_id, {}).items()
        ):
            found = [
                c["relation"] for c in key["cards"] if labels.get(c["case_id"]) == label
            ]
            if found != [relation]:
                problems.append(f"{eid}: {entity_id}/{label} relation {found}")
    if totals.wrong_links > wrong_before:
        totals.episodes_with_wrong_link += 1
    totals.failures.extend(problems)
    return problems


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return round(ordered[index], 3)


def run_evaluation(
    config_path: Path | str,
    *,
    episode_ids: Sequence[str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Run the configured episodes in a temporary directory and return the report."""
    root = root or ROOT
    config = loads_strict(Path(config_path).read_text(encoding="utf-8"))
    fixture = loads_strict((root / config["fixture"]).read_text(encoding="utf-8"))
    review_config = ReviewConfig.load(root / config["review_config"])
    base_memory = MemoryConfig.load(root / config["memory_config"])
    if not any(s.snapshot_sha256 == packaged_snapshot() for s in base_memory.snapshots):
        raise SystemExit(
            "config/memory.json does not register the current packaged snapshot; "
            "memory would report it as unregistered"
        )
    memory_config = build_memory_config(base_memory, fixture)
    episodes = [
        e
        for e in fixture["episodes"]
        if episode_ids is None or e["episode_id"] in episode_ids
    ]
    split_of = {g: s for s, groups in fixture["splits"].items() for g in groups}
    totals = {"all": Totals(), **{s: Totals() for s in fixture["splits"]}}
    lookup_ms: list[float] = []
    per_episode = []
    started = time.perf_counter()
    scratch = Path(tempfile.mkdtemp(prefix="watchlist-memory-eval-"))
    try:
        runner = EpisodeRunner(
            fixture, review_config, memory_config, config["workspace_id"], scratch
        )
        for episode in episodes:
            result = runner.run(episode)
            problems = score_episode(result, totals["all"])
            score_episode(result, totals[split_of[episode["group"]]])
            lookup_ms.extend(result["lookup_ms"])
            per_episode.append(
                {
                    "episode_id": episode["episode_id"],
                    "group": episode["group"],
                    "scenario": episode["scenario"],
                    "passed": not problems,
                    "problems": problems,
                }
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {
        "eval_version": config["eval_version"],
        "notice": config["notice"],
        "fixture_version": fixture["fixture_version"],
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "groups": len({e["group"] for e in episodes}),
        "episodes_passed": sum(1 for e in per_episode if e["passed"]),
        "episodes_total": len(per_episode),
        "totals": {name: t.report() for name, t in totals.items()},
        "lookup_ms": {
            "n": len(lookup_ms),
            "median": None if not lookup_ms else round(statistics.median(lookup_ms), 3),
            "p95": _percentile(lookup_ms, 0.95),
            "note": "Provider wall time on this machine including SQLite open and identity checks; not a performance claim.",
        },
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "episodes": per_episode,
        "not_measured": [
            "reviewer benefit, review time or anchoring (needs a consenting reviewer study, Step 22)",
            "real-world identity or matching quality",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m watchlist.case_memory.evaluation")
    parser.add_argument("--config", default="eval/configs/memory.json")
    parser.add_argument("--episodes", nargs="*")
    parser.add_argument("--output", help="write the JSON report to a NEW file")
    args = parser.parse_args(argv)
    report = run_evaluation(args.config, episode_ids=args.episodes, root=Path.cwd())
    text = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        with Path(args.output).open("x", encoding="utf-8") as handle:
            handle.write(text)
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    return 0 if report["episodes_passed"] == report["episodes_total"] else 1


if __name__ == "__main__":
    sys.exit(main())
