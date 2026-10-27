"""Top-50 hybrid candidate generation with weighted reciprocal-rank fusion (Step 12).

Lexical, phonetic and dense component lists are each collapsed to one
candidate per entity, then fused with weighted reciprocal-rank fusion (RRF):
``fused(e) = sum_arm weight_arm / (rrf_k + rank_arm(e))`` over the arms that
returned ``e``. The fused number is a rank-combination value: it is not a
similarity and never a probability. Each output candidate keeps per-arm
evidence (arm, rank, raw score, scorer, matched alias) and record provenance.

Known-equivalent records (explicit provider links, never name similarity) can
be grouped: component lists are then ranked over group IDs before fusion and
cutting, so linked duplicates never consume shortlist or per-arm depth slots.

CLI (fictional synthetic corpus, deterministic hashed encoder)::

    python -m watchlist.hybrid_retrieval search --query "Kavendor Lisa" --entities 300
    python -m watchlist.hybrid_retrieval evaluate --config eval/configs/baselines-v2.json \
        --output-root data/eval-comparisons/<new-name> --partition development
"""

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from watchlist.baselines import (
    Coverage,
    RetrievalResult,
    build_retriever,
    check_entities,
    check_top_k,
)
from watchlist.entities import Entity, Provenance

FUSION = "weighted-rrf-v1"
COMPONENTS = ("dense", "lexical", "phonetic")
CONFIG_KEYS = frozenset({"weights", "rrf_k", "per_arm_depth"})


@dataclass(frozen=True)
class ArmEvidence:
    """Why one component retrieved a row; raw_score is on that arm's scale.

    ``entity_id`` is the record this arm actually ranked; inside a group it
    may differ from the row's representative.
    """

    component: str
    arm: str
    scorer_name: str
    rank: int
    raw_score: float
    entity_id: str
    matched_alias_id: str
    matched_alias: str


@dataclass(frozen=True)
class HybridCandidate:
    """One shortlist row with its fused RRF value and the evidence of every arm.

    Without grouping, ``group_id`` is None and ``member_ids`` is empty. With
    grouping, the row stands for every listed member and ``entity_id`` is the
    best-ranked member (the representative).
    """

    entity_id: str
    display_name: str
    entity_type: str
    matched_alias_id: str
    matched_alias: str
    fused_score: float
    evidence: tuple[ArmEvidence, ...]
    synthetic: bool
    source: str
    provenance: Provenance | None = None
    group_id: str | None = None
    member_ids: tuple[str, ...] = ()
    fusion: str = FUSION

    @property
    def similarity_score(self) -> float:
        """Ordering value for the shared metric contract (RRF, not a similarity)."""
        return self.fused_score


@dataclass(frozen=True)
class HybridConfig:
    """Predeclared fusion settings; weights are fixed before evaluation."""

    weights: tuple[tuple[str, float], ...] = (
        ("dense", 1.0),
        ("lexical", 1.0),
        ("phonetic", 1.0),
    )
    rrf_k: int = 60
    per_arm_depth: int = 50

    def __post_init__(self):
        names = [name for name, _ in self.weights]
        if sorted(names) != sorted(set(names)) or not set(names) <= set(COMPONENTS):
            raise ValueError(
                "weights must name distinct components among dense/lexical/phonetic"
            )
        if any(
            isinstance(w, bool)
            or not isinstance(w, (int, float))
            or not math.isfinite(w)
            or w < 0
            for _, w in self.weights
        ) or not any(w > 0 for _, w in self.weights):
            raise ValueError("weights must be finite, non-negative, and not all zero")
        for name in ("rrf_k", "per_arm_depth"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    @classmethod
    def from_dict(
        cls, data: Mapping, *, allowed_extra: frozenset[str] = frozenset()
    ) -> "HybridConfig":
        """Strict parse: unknown keys and non-numeric weights are errors, never ignored."""
        if not isinstance(data, Mapping):
            raise ValueError("hybrid config must be an object")
        unknown = set(data) - CONFIG_KEYS - set(allowed_extra)
        if unknown:
            raise ValueError(
                "unknown hybrid config keys: " + ", ".join(sorted(map(str, unknown)))
            )
        weights = data.get("weights")
        if not isinstance(weights, Mapping) or not weights:
            raise ValueError("weights must be a non-empty object")
        for name, value in weights.items():
            if (
                not isinstance(name, str)
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
            ):
                raise ValueError("weights must map component names to numbers")
        return cls(
            tuple(sorted((name, float(value)) for name, value in weights.items())),
            data.get("rrf_k", 60),
            data.get("per_arm_depth", 50),
        )


class DenseRetriever:
    """Alias vectors in a vector index; collapse alias hits to best alias per entity.

    The exact backend scans every alias, so collapse is exact including ties.
    The HNSW backend fetches progressively more alias rows until enough
    distinct entities are found or every row was requested.
    """

    def __init__(
        self,
        entities: Sequence[Entity],
        encoder: object,
        *,
        backend: str = "exact",
        parameters: dict | None = None,
        arm: str = "char_ngram_cosine",
    ):
        from watchlist.vector_index import build_index

        self.entities = check_entities(entities)
        self.encoder, self.arm, self.backend = encoder, arm, backend
        self.scorer_name = (
            f"{encoder.metadata.get('encoder', 'encoder')}_cosine_{backend}"
        )
        self._owner = {}
        ids, texts = [], []
        for entity in self.entities:
            for alias in entity.aliases:
                ids.append(alias.alias_id)
                texts.append(alias.name)
                self._owner[alias.alias_id] = (entity, alias)
        self.index = build_index(backend, ids, encoder.encode(texts), parameters)
        alias_count = len(ids)
        self.coverage = Coverage(
            len(self.entities), alias_count, alias_count, len(self.entities)
        )

    def retrieve(self, query: str, top_k: int) -> RetrievalResult:
        from watchlist.matching import Candidate

        check_top_k(top_k)
        vector = self.encoder.encode([query])[0]
        total = len(self.index.ids)
        fetch = total if self.backend == "exact" else min(total, top_k * 4)
        while True:
            best: dict[str, tuple[float, object]] = {}
            for alias_id, score in self.index.search(vector, fetch):
                entity, alias = self._owner[alias_id]
                current = best.get(entity.entity_id)
                if current is None or (-score, alias.alias_id) < (
                    -current[0],
                    current[1].alias_id,
                ):
                    best[entity.entity_id] = (score, alias)
            if len(best) >= top_k or fetch >= total:
                break
            fetch = min(total, fetch * 2)
        owners = {entity.entity_id: entity for entity in self.entities}
        candidates = [
            Candidate(
                eid,
                owners[eid].name,
                owners[eid].entity_type,
                alias.alias_id,
                alias.name,
                score,
                owners[eid].synthetic,
                owners[eid].source,
                owners[eid].provenance,
            )
            for eid, (score, alias) in best.items()
        ]
        candidates.sort(
            key=lambda candidate: (-candidate.similarity_score, candidate.entity_id)
        )
        return RetrievalResult(tuple(candidates[:top_k]))


def fuse(
    component_results: Mapping[str, tuple[object, RetrievalResult]],
    config: HybridConfig,
    top_k: int,
    groups: Mapping[str, str] | None = None,
) -> RetrievalResult:
    """Weighted RRF over per-component lists; ties by row key (entity or group ID).

    ``component_results`` maps component name to (retriever, result). Each
    component list must already contain at most one row per entity. With
    ``groups`` (entity ID -> group ID), each list is first ranked over distinct
    groups (a group's best member gives its rank) and only then truncated to
    ``per_arm_depth``; fusion, ordering and the top_k cut all use group rows.
    """
    check_top_k(top_k)
    weights = dict(config.weights)

    def key_of(entity_id: str) -> str:
        return entity_id if groups is None else groups.get(entity_id, entity_id)

    evidence: dict[str, list[ArmEvidence]] = {}
    members: dict[str, dict[str, object]] = {}
    status = []
    for component in sorted(component_results):
        retriever, result = component_results[component]
        status.append((component, result.unsupported_reason))
        ids = [candidate.entity_id for candidate in result.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError(
                f"{component} list repeats an entity; collapse aliases first"
            )
        seen: set[str] = set()
        for candidate in result.candidates:
            key = key_of(candidate.entity_id)
            if key not in seen:
                if len(seen) == config.per_arm_depth:
                    break
                seen.add(key)
                evidence.setdefault(key, []).append(
                    ArmEvidence(
                        component,
                        retriever.arm,
                        retriever.scorer_name,
                        len(seen),
                        float(candidate.similarity_score),
                        candidate.entity_id,
                        candidate.matched_alias_id,
                        candidate.matched_alias,
                    )
                )
            members.setdefault(key, {}).setdefault(candidate.entity_id, candidate)
    fused = []
    for key, items in evidence.items():
        # Sum in fixed component order so the float result is reproducible.
        score = sum(
            weights.get(item.component, 0.0) / (config.rrf_k + item.rank)
            for item in items
        )
        lead = min(
            items,
            key=lambda item: (
                item.rank,
                -weights.get(item.component, 0.0),
                item.component,
            ),
        )
        record = members[key][lead.entity_id]
        fused.append(
            HybridCandidate(
                lead.entity_id,
                record.display_name,
                record.entity_type,
                lead.matched_alias_id,
                lead.matched_alias,
                score,
                tuple(items),
                record.synthetic,
                record.source,
                getattr(record, "provenance", None),
                None if groups is None else key,
                () if groups is None else tuple(sorted(members[key])),
            )
        )
    fused.sort(
        key=lambda candidate: (-candidate.fused_score, key_of(candidate.entity_id))
    )
    reasons = [reason for _, reason in status if reason]
    unsupported = (
        "; ".join(f"{c}: {r}" for c, r in status if r)
        if status and len(reasons) == len(status)
        else None
    )
    return RetrievalResult(tuple(fused[:top_k]), unsupported, tuple(status))


def check_groups(
    groups: Mapping[str, str], entities: Sequence[Entity]
) -> dict[str, str]:
    """Validate an explicit entity-to-group link map against the reference view."""
    known = {entity.entity_id for entity in entities}
    result = dict(groups)
    if any(
        not isinstance(k, str) or not isinstance(v, str) or not v.strip()
        for k, v in result.items()
    ):
        raise ValueError("groups must map entity IDs to nonblank group IDs")
    if not set(result) <= known:
        raise ValueError("groups name entity IDs outside the reference view")
    if set(result.values()) & (known - set(result)):
        raise ValueError("a group ID collides with an ungrouped entity ID")
    return result


class HybridRetriever:
    """Run each weighted component to ``per_arm_depth`` rows and fuse with RRF.

    With ``groups``, components are queried progressively deeper until they
    yield ``per_arm_depth`` distinct groups (or are exhausted), so linked
    duplicates do not shrink any arm's effective depth.
    """

    def __init__(
        self,
        components: Mapping[str, object],
        config: HybridConfig,
        *,
        arm: str = "hybrid_rrf",
        groups: Mapping[str, str] | None = None,
    ):
        weights = dict(config.weights)
        if set(components) != set(weights):
            raise ValueError("components must match the configured weights")
        self.components, self.config, self.arm = dict(components), config, arm
        self.groups = None if groups is None else dict(groups)
        self.scorer_name = f"{FUSION}(k={config.rrf_k},depth={config.per_arm_depth})"
        coverages = [component.coverage for component in self.components.values()]
        first = coverages[0]
        self.coverage = Coverage(
            first.entity_count,
            first.alias_count,
            max(c.indexed_alias_count for c in coverages),
            max(c.retrievable_entity_count for c in coverages),
        )

    def _component(self, component: object, query: str) -> RetrievalResult:
        depth = self.config.per_arm_depth
        if self.groups is None:
            return component.retrieve(query, depth)
        limit = max(1, component.coverage.entity_count)
        fetch = min(depth, limit)
        while True:
            result = component.retrieve(query, fetch)
            distinct = {
                self.groups.get(c.entity_id, c.entity_id) for c in result.candidates
            }
            if (
                len(distinct) >= depth
                or len(result.candidates) < fetch
                or fetch >= limit
            ):
                return result
            fetch = min(limit, fetch * 2)

    def retrieve(self, query: str, top_k: int) -> RetrievalResult:
        check_top_k(top_k)
        weights = dict(self.config.weights)
        results = {
            name: (component, self._component(component, query))
            for name, component in sorted(self.components.items())
            if weights[name] > 0
        }
        return fuse(results, self.config, top_k, self.groups)


def build_hybrid(
    entities: Sequence[Entity],
    encoder: object,
    config: HybridConfig,
    *,
    lexical_arm: str = "levenshtein",
    phonetic_arm: str = "phonetic_metaphone",
    dense_backend: str = "exact",
    dense_parameters: dict | None = None,
    arm: str = "hybrid_rrf",
    groups: Mapping[str, str] | None = None,
) -> HybridRetriever:
    """Assemble the three standard components over one reference view."""
    groups = None if groups is None else check_groups(groups, entities)
    builders = {
        "lexical": lambda: build_retriever(lexical_arm, entities),
        "phonetic": lambda: build_retriever(phonetic_arm, entities),
        "dense": lambda: DenseRetriever(
            entities, encoder, backend=dense_backend, parameters=dense_parameters
        ),
    }
    return HybridRetriever(
        {name: builders[name]() for name, _ in config.weights},
        config,
        arm=arm,
        groups=groups,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    actions = parser.add_subparsers(dest="action", required=True)
    search = actions.add_parser(
        "search", help="fuse arms for one query over the generated fictional corpus"
    )
    search.add_argument("--query", required=True)
    search.add_argument("--entities", type=int, default=300)
    search.add_argument("--seed", type=int, default=42)
    search.add_argument("--top-k", type=int, default=10)
    evaluate = actions.add_parser(
        "evaluate", help="run only the dense and hybrid arms of a baselines config"
    )
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument("--output-root", type=Path, required=True)
    evaluate.add_argument("--dataset")
    evaluate.add_argument("--partition", choices=["development", "test"], required=True)
    evaluate.add_argument(
        "--unseal-test",
        action="store_true",
        help="required (with --freeze-receipt) for test",
    )
    evaluate.add_argument("--freeze-receipt", type=Path)
    args = parser.parse_args()
    if args.action == "evaluate":
        from watchlist.evaluate_baselines import DENSE_ARM, prepare_run, run

        try:
            config, destinations = prepare_run(
                args.config,
                args.output_root,
                dataset=args.dataset,
                partition=args.partition,
                unseal_test=args.unseal_test,
                freeze_receipt=args.freeze_receipt,
                arm_filter=lambda arm, hybrids: arm == DENSE_ARM or arm in hybrids,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        run(config, destinations)
        return 0
    try:
        from watchlist.synthetic_corpus import generate_entities
        from watchlist.vector_index import HashedCharNgramEncoder

        retriever = build_hybrid(
            generate_entities(args.entities, args.seed),
            HashedCharNgramEncoder(),
            HybridConfig(),
        )
        result = retriever.retrieve(args.query, args.top_k)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "note": "fictional corpus; fused_score is an RRF rank value, not a probability",
                "component_status": dict(result.component_status),
                "candidates": [asdict(c) for c in result.candidates],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
