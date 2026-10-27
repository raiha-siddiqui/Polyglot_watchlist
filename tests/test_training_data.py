"""Training pairs come only from training groups and carry stable lineage digests."""

from dataclasses import replace

import pytest

from watchlist.entities import SYNTHETIC_SOURCE, Alias, Entity
from watchlist.normalize import normalize_name
from watchlist.training_data import (
    TrainingDataError,
    build_training_pairs,
    fictional_assignments,
    generate_fictional_entities,
    group_aware_batches,
    verify_manifest,
)


def entity(entity_id: str, *names: str) -> Entity:
    aliases = tuple(
        Alias(f"{entity_id}-a{i}", name) for i, name in enumerate(names, start=1)
    )
    return Entity(entity_id, names[0], "person", aliases, True, SYNTHETIC_SOURCE)


def assignment(partition: str, group: str) -> dict:
    return {"group_id": group, "partition": partition}


@pytest.fixture
def poisoned() -> tuple[tuple[Entity, ...], dict]:
    """Held-out records are near-identical to training names: ideal hard negatives if leaked."""
    entities = (
        entity("tr-1", "Orin Vale", "O. Vale", "Vale Orin"),
        entity("tr-2", "Orin Valle", "O. Valle"),
        entity("tr-3", "Pella Quist", "P. Quist"),
        entity("dev-1", "Orin Vaile", "O. Vaile"),
        entity("test-1", "Orin Val", "O. Val"),
        entity("cal-1", "Pela Quist", "P. Kwist"),
    )
    assignments = {
        "tr-1": assignment("train", "g1"),
        "tr-2": assignment("train", "g2"),
        "tr-3": assignment("train", "g3"),
        "dev-1": assignment("development", "g4"),
        "test-1": assignment("test", "g5"),
        "cal-1": assignment("calibration_fit", "g6"),
    }
    return entities, assignments


def test_poison_held_out_ids_never_reach_pairs_or_mining(poisoned) -> None:
    entities, assignments = poisoned
    result = build_training_pairs(
        entities,
        assignments,
        hard_negatives_per_positive=3,
        easy_negatives_per_positive=3,
        forbidden_entity_ids={"dev-1", "test-1", "cal-1"},
    )
    endpoints = {
        pid for p in result.pairs for pid in (p.query_entity_id, p.candidate_entity_id)
    }
    assert endpoints <= {"tr-1", "tr-2", "tr-3"}
    held_texts = {normalize_name(a.name) for e in entities[3:] for a in e.aliases}
    assert not held_texts & {normalize_name(p.candidate_text) for p in result.pairs}
    assert set(result.manifest["train_entity_ids"]) == {"tr-1", "tr-2", "tr-3"}
    hard = [p for p in result.pairs if p.kind == "hard_negative"]
    assert hard and all(p.query_group_id != p.candidate_group_id for p in hard)


def test_planted_held_out_id_labelled_train_is_refused(poisoned) -> None:
    entities, assignments = poisoned
    with pytest.raises(TrainingDataError, match="held-out IDs are labelled train"):
        build_training_pairs(entities, assignments, forbidden_entity_ids={"tr-2"})


def test_group_leakage_and_missing_coverage_are_refused(poisoned) -> None:
    entities, assignments = poisoned
    leaked = {**assignments, "dev-1": assignment("development", "g1")}
    with pytest.raises(ValueError, match="leakage"):
        build_training_pairs(entities, leaked)
    with pytest.raises(ValueError, match="coverage"):
        build_training_pairs(
            entities, {k: v for k, v in assignments.items() if k != "cal-1"}
        )


def test_positive_cap_and_labels(poisoned) -> None:
    entities, assignments = poisoned
    result = build_training_pairs(entities, assignments, max_positives_per_group=1)
    positives = result.positives
    assert len([p for p in positives if p.query_group_id == "g1"]) == 1
    assert all(
        p.label == 1 and p.query_entity_id == p.candidate_entity_id for p in positives
    )
    assert all(p.label == 0 for p in result.pairs if p.kind != "positive")


def test_manifest_digest_is_independent_of_input_order_and_detects_tampering(
    poisoned,
) -> None:
    entities, assignments = poisoned
    shuffled = tuple(
        replace(e, aliases=tuple(reversed(e.aliases))) for e in reversed(entities)
    )
    first = build_training_pairs(entities, assignments)
    second = build_training_pairs(shuffled, dict(reversed(list(assignments.items()))))
    assert first.pairs == second.pairs
    assert first.manifest == second.manifest
    assert verify_manifest(first.manifest) == first.manifest["digest"]
    with pytest.raises(TrainingDataError, match="digest"):
        verify_manifest({**first.manifest, "counts": {"positive": 999}})
    assert (
        build_training_pairs(entities, assignments, seed=7).manifest["digest"]
        != first.manifest["digest"]
    )


def test_one_training_group_cannot_supply_negatives() -> None:
    entities = (entity("a", "Ana Lor", "A. Lor"), entity("b", "Bel Mor", "B. Mor"))
    assignments = {"a": assignment("train", "g"), "b": assignment("test", "h")}
    with pytest.raises(TrainingDataError, match="two training groups"):
        build_training_pairs(entities, assignments)


def test_group_aware_batches_never_repeat_a_group_and_are_deterministic(
    poisoned,
) -> None:
    entities, assignments = poisoned
    pairs = build_training_pairs(entities, assignments).pairs
    batches = group_aware_batches(pairs, 3, seed=1, epoch=0)
    assert batches == group_aware_batches(pairs, 3, seed=1, epoch=0)
    for batch in batches:
        groups = [p.query_group_id for p in batch]
        assert len(groups) == len(set(groups)) and all(p.label == 1 for p in batch)
    with pytest.raises(TrainingDataError):
        group_aware_batches(pairs, 1, seed=1, epoch=0)


def test_fictional_generator_is_deterministic_and_valid() -> None:
    first = generate_fictional_entities(40, seed=3)
    assert first == generate_fictional_entities(40, seed=3)
    assert first != generate_fictional_entities(40, seed=4)
    assert len({e.entity_id for e in first}) == 40
    for record in first:
        normalized = [normalize_name(a.name) for a in record.aliases]
        assert record.synthetic and record.source == SYNTHETIC_SOURCE
        assert (
            len(normalized) == len(set(normalized))
            and normalize_name(record.name) in normalized
        )
    partitions = {v["partition"] for v in fictional_assignments(first, 42).values()}
    assert "train" in partitions
    with pytest.raises(TrainingDataError):
        generate_fictional_entities(0)
