from dataclasses import asdict

import pytest

from watchlist.evaluation_data import build_benchmark
from watchlist.normalize import normalize_name
from watchlist.scripts import script_set
from watchlist.splits import validate_assignments
from watchlist.synthetic_corpus import (
    SOURCE,
    corpus_digest,
    generate_absent,
    generate_corpus,
    generate_entities,
)


def test_deterministic_and_seed_sensitive():
    first, second = generate_corpus(60, 7, 5), generate_corpus(60, 7, 5)
    assert [asdict(e) for e in first.entities] == [asdict(e) for e in second.entities]
    assert corpus_digest(first) == corpus_digest(second)
    assert corpus_digest(generate_corpus(60, 8, 5)) != corpus_digest(first)
    # A larger corpus extends the same seeded stream: prefixes are stable.
    assert generate_entities(30, 7) == first.entities[:30]


def test_records_are_fictional_well_formed_and_multiscript():
    corpus = generate_corpus(200, 42, 10)
    alias_ids = [a.alias_id for e in corpus.entities for a in e.aliases]
    assert len(alias_ids) == len(set(alias_ids))
    scripts = set()
    for entity in corpus.entities:
        assert entity.synthetic is True and entity.source == SOURCE
        normalized = [normalize_name(a.name) for a in entity.aliases]
        assert len(normalized) == len(set(normalized)) >= 2
        assert normalize_name(entity.name) in normalized
        for alias in entity.aliases:
            scripts |= script_set(alias.name)
    assert {"LATIN", "CYRILLIC", "ARABIC", "HAN"} <= scripts
    primaries = [normalize_name(e.name) for e in corpus.entities]
    assert len(primaries) == len(set(primaries))
    assert corpus.manifest["alias_count"] == len(alias_ids)
    assert sum(corpus.manifest["aliases_by_script"].values()) == len(alias_ids)


def test_partitions_and_absent_are_valid_and_disjoint():
    corpus = generate_corpus(120, 3, 12)
    validate_assignments([e.entity_id for e in corpus.entities], corpus.assignments)
    names = {normalize_name(a.name) for e in corpus.entities for a in e.aliases}
    assert len(corpus.absent) == 12
    assert all(normalize_name(item["text"]) not in names for item in corpus.absent)
    assert {item["partition"] for item in corpus.absent} == {"development", "test"}
    # The existing current-contract benchmark builder accepts the generated corpus.
    views, queries, audit = build_benchmark(
        corpus.entities, corpus.assignments, seed=3, absent=corpus.absent
    )
    assert len(views["full"]) == 120 and queries
    assert {q["status"] for q in queries} >= {"positive", "absent"}


@pytest.mark.parametrize("count", [0, -1, True, 2.0])
def test_invalid_counts(count):
    with pytest.raises(ValueError):
        generate_entities(count)
    if count != 0:
        with pytest.raises(ValueError):
            generate_absent((), count)
