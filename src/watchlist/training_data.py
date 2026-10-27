"""Training-only pair construction, fictional corpus generation and lineage digests.

Every pair endpoint must belong to a ``train`` partition group. Development,
calibration, threshold-selection and test groups never become positives,
negatives or mining candidates. The manifest digest is computed from sorted
canonical records, so reordering the input entities cannot change it.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
import hashlib
import random

from rapidfuzz import fuzz, process

from watchlist.entities import SYNTHETIC_SOURCE, Alias, Entity
from watchlist.evaluation_data import fingerprint
from watchlist.normalize import normalize_name
from watchlist.splits import assign_groups, validate_assignments

PAIR_PROTOCOL = "training-pairs-v1"
MINER = "rapidfuzz-token-sort-train-only-v1"
GENERATOR = "fictional-syllable-generator-v1"
FICTIONAL_DOMAIN = "fictional-generated-v1"

# Invented syllables; generated names are not drawn from any real-person list.
_SYLLABLES = (
    "ka",
    "lo",
    "mi",
    "ven",
    "tar",
    "zu",
    "rel",
    "sha",
    "no",
    "bri",
    "del",
    "fo",
    "qui",
    "ra",
    "sel",
    "tum",
    "vi",
    "xo",
    "ya",
    "or",
    "pel",
    "dra",
    "nim",
    "eso",
)
_ORG_SUFFIXES = ("Workshop", "Cooperative", "Library", "Foundry", "Atelier", "Guild")
_CYRILLIC = {
    "a": "а",
    "b": "б",
    "d": "д",
    "e": "е",
    "f": "ф",
    "i": "и",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "x": "кс",
    "y": "й",
    "z": "з",
    "h": "х",
    "g": "г",
    "c": "ц",
    "w": "в",
}


class TrainingDataError(ValueError):
    """Structured refusal for leakage, coverage or lineage violations."""


@dataclass(frozen=True)
class TrainingPair:
    """One supervised example; label 1 is a same-entity alias pair, 0 a negative."""

    pair_id: str
    kind: str
    label: int
    query_alias_id: str
    query_text: str
    query_entity_id: str
    query_group_id: str
    candidate_alias_id: str
    candidate_text: str
    candidate_entity_id: str
    candidate_group_id: str
    miner: str | None = None
    miner_rank: int | None = None


@dataclass(frozen=True)
class TrainingSet:
    """Pairs plus the manifest that binds them to their training-only inputs."""

    pairs: tuple[TrainingPair, ...]
    manifest: dict

    @property
    def positives(self) -> tuple[TrainingPair, ...]:
        return tuple(pair for pair in self.pairs if pair.label == 1)


def _stable_int(*parts: object) -> int:
    """Process-independent integer for deterministic selection and tie-breaking."""
    text = ":".join(str(part) for part in parts)
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], 16)


def _cyrillic(text: str) -> str:
    return "".join(_CYRILLIC.get(ch, ch) for ch in text.lower()).title()


def _typo(text: str, salt: int) -> str:
    """Swap two adjacent letters inside the longest word; deterministic by salt."""
    words = text.split()
    index = max(range(len(words)), key=lambda i: (len(words[i]), -i))
    word = words[index]
    if len(word) < 4:
        return text
    position = 1 + salt % (len(word) - 2)
    words[index] = (
        word[:position] + word[position + 1] + word[position] + word[position + 2 :]
    )
    return " ".join(words)


def generate_fictional_entities(count: int, seed: int = 42) -> tuple[Entity, ...]:
    """Deterministically invent fictional people/organisations with varied aliases.

    Alias kinds: canonical, initial-plus-family or acronym-free short form,
    reordered tokens, an adjacent-letter typo and, for half of the entities, a
    naive Latin-to-Cyrillic rendering. Distinct entities may share tokens, which
    supplies realistic hard negatives. All records declare ``synthetic: true``.
    """
    if type(count) is not int or count < 1:
        raise TrainingDataError("entity count must be a positive integer")
    if type(seed) is not int:
        raise TrainingDataError("generator seed must be an integer")
    rng = random.Random(f"{GENERATOR}:{seed}")
    seen: set[str] = set()
    entities = []
    while len(entities) < count:
        given = "".join(
            rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 3))
        ).title()
        family = "".join(
            rng.choice(_SYLLABLES) for _ in range(rng.randint(2, 3))
        ).title()
        organisation = rng.random() < 0.3
        name = (
            f"{given} {family} {rng.choice(_ORG_SUFFIXES)}"
            if organisation
            else f"{given} {family}"
        )
        if normalize_name(name) in seen:
            continue
        seen.add(normalize_name(name))
        number = len(entities) + 1
        entity_id = f"gen-{number:05d}"
        if organisation:
            forms = [
                name,
                f"{given} {family}",
                f"{family} {given} {name.split()[-1]}",
                _typo(name, number),
            ]
        else:
            forms = [
                name,
                f"{given[0]}. {family}",
                f"{family} {given}",
                _typo(name, number),
            ]
        if number % 2 == 0:
            forms.append(_cyrillic(name))
        aliases, normalized = [], set()
        for form in forms:
            key = normalize_name(form)
            if key in normalized:
                continue
            normalized.add(key)
            aliases.append(Alias(f"{entity_id}-a{len(aliases) + 1}", form))
        entities.append(
            Entity(
                entity_id,
                name,
                "organisation" if organisation else "person",
                tuple(aliases),
                True,
                SYNTHETIC_SOURCE,
            )
        )
    return tuple(entities)


def fictional_assignments(
    entities: Sequence[Entity], seed: int = 42
) -> dict[str, dict[str, str]]:
    """Partition generated entities with the production group-hash splitter."""
    return assign_groups([{"id": e.entity_id, "referents": []} for e in entities], seed)


def _ordered(entities: Iterable[Entity]) -> tuple[Entity, ...]:
    """Canonical entity and alias order, so lineage never depends on input order."""
    return tuple(
        replace(e, aliases=tuple(sorted(e.aliases, key=lambda a: a.alias_id)))
        for e in sorted(entities, key=lambda e: e.entity_id)
    )


def partition_entities(
    entities: Sequence[Entity], assignments: Mapping, partition: str
) -> tuple[Entity, ...]:
    """Return one partition after validating exact coverage and group ownership."""
    validate_assignments([e.entity_id for e in entities], dict(assignments))
    return _ordered(
        e for e in entities if assignments[e.entity_id]["partition"] == partition
    )


def _check_endpoint(
    pair: TrainingPair, assignments: Mapping, forbidden: frozenset[str]
) -> None:
    for entity_id in (pair.query_entity_id, pair.candidate_entity_id):
        if entity_id in forbidden:
            raise TrainingDataError(
                f"held-out entity reached training pairs: {entity_id}"
            )
        if assignments[entity_id]["partition"] != "train":
            raise TrainingDataError(f"non-training endpoint in pair: {entity_id}")


def build_training_pairs(
    entities: Sequence[Entity],
    assignments: Mapping,
    *,
    seed: int = 42,
    max_positives_per_group: int = 6,
    hard_negatives_per_positive: int = 1,
    easy_negatives_per_positive: int = 1,
    forbidden_entity_ids: Iterable[str] = (),
) -> TrainingSet:
    """Build capped positives and training-only easy/hard negatives.

    ``forbidden_entity_ids`` is defence in depth: planted held-out IDs that are
    also labelled ``train`` are a contradictory split and raise. Hard negatives
    are the highest RapidFuzz token-sort aliases from other training groups; ties are
    broken by alias ID. Easy negatives are hash-selected other training groups.
    """
    for name, value in (
        ("max_positives_per_group", max_positives_per_group),
        ("hard_negatives_per_positive", hard_negatives_per_positive),
        ("easy_negatives_per_positive", easy_negatives_per_positive),
    ):
        if type(value) is not int or value < (
            1 if name == "max_positives_per_group" else 0
        ):
            raise TrainingDataError(
                f"{name} must be a nonnegative integer (positive for caps)"
            )
    forbidden = frozenset(forbidden_entity_ids)
    train = partition_entities(entities, assignments, "train")
    if conflict := sorted(forbidden & {e.entity_id for e in train}):
        raise TrainingDataError(f"held-out IDs are labelled train: {conflict}")
    groups = {e.entity_id: assignments[e.entity_id]["group_id"] for e in train}
    if len(set(groups.values())) < 2:
        raise TrainingDataError(
            "at least two training groups are required for negatives"
        )
    pool = [(e, a) for e in train for a in sorted(e.aliases, key=lambda a: a.alias_id)]
    normalized = {a.alias_id: normalize_name(a.name) for _, a in pool}

    positives: list[TrainingPair] = []
    by_group: dict[str, list[tuple[Entity, Alias, Alias]]] = {}
    for entity in train:
        aliases = sorted(entity.aliases, key=lambda a: a.alias_id)
        for i, left in enumerate(aliases):
            for right in aliases[i + 1 :]:
                by_group.setdefault(groups[entity.entity_id], []).append(
                    (entity, left, right)
                )
    for group in sorted(by_group):
        candidates = sorted(
            by_group[group],
            key=lambda item: (
                _stable_int(seed, item[1].alias_id, item[2].alias_id),
                item[1].alias_id,
                item[2].alias_id,
            ),
        )
        for entity, left, right in candidates[:max_positives_per_group]:
            positives.append(
                TrainingPair(
                    f"pos:{left.alias_id}:{right.alias_id}",
                    "positive",
                    1,
                    left.alias_id,
                    left.name,
                    entity.entity_id,
                    group,
                    right.alias_id,
                    right.name,
                    entity.entity_id,
                    group,
                )
            )

    negatives: list[TrainingPair] = []
    pool_groups = [groups[e.entity_id] for e, _ in pool]
    others_cache: dict[str, list[tuple[Entity, Alias]]] = {}
    hard = _mine_hard_negatives(
        positives, pool, pool_groups, normalized, hard_negatives_per_positive
    )
    for positive in positives:
        group = positive.query_group_id
        if group not in others_cache:
            others_cache[group] = [
                item for item, owner in zip(pool, pool_groups) if owner != group
            ]
        others = others_cache[group]
        ranked = hard.get(positive.query_alias_id, [])
        for rank, (entity, alias) in enumerate(
            ranked[:hard_negatives_per_positive], start=1
        ):
            negatives.append(
                TrainingPair(
                    f"hard:{positive.query_alias_id}:{alias.alias_id}",
                    "hard_negative",
                    0,
                    positive.query_alias_id,
                    positive.query_text,
                    positive.query_entity_id,
                    positive.query_group_id,
                    alias.alias_id,
                    alias.name,
                    entity.entity_id,
                    groups[entity.entity_id],
                    MINER,
                    rank,
                )
            )
        start = _stable_int(seed, "easy", positive.pair_id) % len(others)
        stride = 1 + _stable_int(seed, "stride", positive.pair_id) % max(
            len(others) - 1, 1
        )
        easy = [
            others[(start + i * stride) % len(others)]
            for i in range(easy_negatives_per_positive)
        ]
        for entity, alias in easy:
            negatives.append(
                TrainingPair(
                    f"easy:{positive.query_alias_id}:{alias.alias_id}",
                    "easy_negative",
                    0,
                    positive.query_alias_id,
                    positive.query_text,
                    positive.query_entity_id,
                    positive.query_group_id,
                    alias.alias_id,
                    alias.name,
                    entity.entity_id,
                    groups[entity.entity_id],
                    "hash-sample-train-only-v1",
                    None,
                )
            )
    unique = {pair.pair_id: pair for pair in positives + negatives}
    pairs = tuple(unique[key] for key in sorted(unique))
    for pair in pairs:
        _check_endpoint(pair, assignments, forbidden)
    return TrainingSet(
        pairs,
        _manifest(
            pairs,
            train,
            assignments,
            {
                "seed": seed,
                "max_positives_per_group": max_positives_per_group,
                "hard_negatives_per_positive": hard_negatives_per_positive,
                "easy_negatives_per_positive": easy_negatives_per_positive,
                "forbidden_entity_ids": sorted(forbidden),
            },
        ),
    )


def _mine_hard_negatives(
    positives: Sequence[TrainingPair],
    pool: list[tuple[Entity, Alias]],
    pool_groups: list[str],
    normalized: dict[str, str],
    count: int,
) -> dict[str, list[tuple[Entity, Alias]]]:
    """Rank the training-only alias pool for each query alias in one bulk call.

    Same-group aliases are excluded before ranking; ties prefer the smaller alias
    ID. NumPy is imported here only because RapidFuzz's bulk scorer returns it.
    """
    if not count:
        return {}
    import numpy as np

    queries = sorted({(p.query_alias_id, p.query_group_id) for p in positives})
    scores = process.cdist(
        [normalized[alias_id] for alias_id, _ in queries],
        [normalized[a.alias_id] for _, a in pool],
        scorer=fuzz.token_sort_ratio,
        processor=None,
        workers=1,
    )
    alias_rank = np.argsort(np.argsort(np.array([a.alias_id for _, a in pool])))
    owners = np.array(pool_groups)
    result = {}
    for row, (alias_id, group) in enumerate(queries):
        allowed = np.flatnonzero(owners != group)
        order = np.lexsort(
            (alias_rank[allowed], -scores[row, allowed].astype(np.float64))
        )
        result[alias_id] = [pool[int(index)] for index in allowed[order[:count]]]
    return result


def _manifest(
    pairs: tuple[TrainingPair, ...],
    train: tuple[Entity, ...],
    assignments: Mapping,
    settings: dict,
) -> dict:
    """Canonical lineage record; the digest covers everything except itself."""
    body = {
        "protocol": PAIR_PROTOCOL,
        "miner": MINER,
        "settings": settings,
        "train_entity_ids": [e.entity_id for e in train],
        "train_group_ids": sorted(
            {assignments[e.entity_id]["group_id"] for e in train}
        ),
        "source_fingerprint": fingerprint([asdict(e) for e in train]),
        "pairs_fingerprint": fingerprint([asdict(p) for p in pairs]),
        "counts": {
            kind: sum(p.kind == kind for p in pairs)
            for kind in ("positive", "hard_negative", "easy_negative")
        },
        "evidence": "fictional/synthetic training pairs; mechanics only, not a reviewed corpus",
    }
    return {**body, "digest": fingerprint(body)}


def verify_manifest(manifest: Mapping) -> str:
    """Recompute the self-digest and return it; tampering is an explicit error."""
    body = {key: value for key, value in manifest.items() if key != "digest"}
    if manifest.get("protocol") != PAIR_PROTOCOL or fingerprint(body) != manifest.get(
        "digest"
    ):
        raise TrainingDataError("training-pair manifest digest mismatch")
    return manifest["digest"]


def group_aware_batches(
    pairs: Sequence[TrainingPair], batch_size: int, *, seed: int, epoch: int
) -> list[tuple[TrainingPair, ...]]:
    """Shuffle positives per epoch so no batch holds two pairs from one group.

    In-batch negatives are only valid across distinct groups; a pair whose group
    is already present waits for a later batch. Order depends only on seed/epoch.
    """
    if type(batch_size) is not int or batch_size < 2:
        raise TrainingDataError("contrastive batch size must be an integer >= 2")
    order = sorted(
        (p for p in pairs if p.label == 1),
        key=lambda p: (_stable_int(seed, epoch, p.pair_id), p.pair_id),
    )
    batches: list[list[TrainingPair]] = []
    for pair in order:
        target = next(
            (
                batch
                for batch in batches
                if len(batch) < batch_size
                and all(item.query_group_id != pair.query_group_id for item in batch)
            ),
            None,
        )
        if target is None:
            batches.append([pair])
        else:
            target.append(pair)
    return [tuple(batch) for batch in batches if len(batch) >= 2]
