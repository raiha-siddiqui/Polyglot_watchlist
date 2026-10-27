from dataclasses import replace

import pytest

pytest.importorskip("numpy", reason="install the embeddings extra for dense arms")
pytest.importorskip(
    "jellyfish", reason="install the preprocessing extra for phonetic arms"
)

from watchlist.baselines import RetrievalResult, build_retriever
from watchlist.entities import Alias, Entity, Provenance
from watchlist.hybrid_retrieval import (
    DenseRetriever,
    HybridConfig,
    HybridRetriever,
    build_hybrid,
    fuse,
)
from watchlist.matching import Candidate
from watchlist.synthetic_corpus import generate_entities
from watchlist.vector_index import HashedCharNgramEncoder


def entity(eid, *names):
    return Entity(
        eid,
        names[0],
        "person",
        tuple(Alias(f"{eid}-{i}", n) for i, n in enumerate(names)),
        True,
        "fictional",
    )


class Fixed:
    """A fake component returning a predetermined ranked list."""

    def __init__(self, arm, ranked, reason=None):
        self.arm, self.scorer_name, self.ranked, self.reason = (
            arm,
            f"{arm}-score",
            ranked,
            reason,
        )

    def result(self):
        return RetrievalResult(
            tuple(
                Candidate(e, e.upper(), "person", f"{e}-0", e, s, True, "fictional")
                for e, s in self.ranked
            ),
            self.reason,
        )


def test_hand_checked_weighted_rrf_and_evidence():
    lexical = Fixed("levenshtein", [("a", 90.0), ("b", 80.0)])
    dense = Fixed("cosine", [("b", 0.9), ("c", 0.5)])
    config = HybridConfig((("dense", 2.0), ("lexical", 1.0)), rrf_k=10, per_arm_depth=5)
    result = fuse(
        {"lexical": (lexical, lexical.result()), "dense": (dense, dense.result())},
        config,
        3,
    )
    scores = {c.entity_id: c.fused_score for c in result.candidates}
    assert scores["b"] == pytest.approx(2 / 11 + 1 / 12)
    assert scores["a"] == pytest.approx(1 / 11)
    assert scores["c"] == pytest.approx(2 / 12)
    assert [c.entity_id for c in result.candidates] == ["b", "c", "a"]
    b = result.candidates[0]
    assert [(e.component, e.rank, e.raw_score) for e in b.evidence] == [
        ("dense", 1, 0.9),
        ("lexical", 2, 80.0),
    ]
    assert b.matched_alias_id == "b-0" and b.similarity_score == b.fused_score
    assert result.component_status == (("dense", None), ("lexical", None))


def test_rrf_ties_break_by_entity_id_and_depth_truncates():
    one, two = (
        Fixed("x", [("b", 1.0), ("z", 0.5)]),
        Fixed("y", [("a", 1.0), ("z", 0.4)]),
    )
    config = HybridConfig(
        (("lexical", 1.0), ("phonetic", 1.0)), rrf_k=60, per_arm_depth=1
    )
    result = fuse(
        {"lexical": (one, one.result()), "phonetic": (two, two.result())}, config, 10
    )
    assert [c.entity_id for c in result.candidates] == [
        "a",
        "b",
    ]  # z beyond depth 1 is not fused


def test_unsupported_component_is_reported_not_hidden():
    ok, han = (
        Fixed("lev", [("a", 50.0)]),
        Fixed("phon", [], "phonetic code undefined for script HAN"),
    )
    config = HybridConfig((("lexical", 1.0), ("phonetic", 1.0)))
    result = fuse(
        {"lexical": (ok, ok.result()), "phonetic": (han, han.result())}, config, 5
    )
    assert dict(result.component_status)["phonetic"].endswith("HAN")
    assert result.unsupported_reason is None and len(result.candidates) == 1
    only = fuse(
        {"phonetic": (han, han.result())}, HybridConfig((("phonetic", 1.0),)), 5
    )
    assert only.unsupported_reason.startswith("phonetic:") and only.candidates == ()


def test_duplicate_entity_in_component_list_is_rejected():
    bad = Fixed("lev", [("a", 1.0), ("a", 0.5)])
    with pytest.raises(ValueError, match="collapse"):
        fuse({"lexical": (bad, bad.result())}, HybridConfig((("lexical", 1.0),)), 5)


@pytest.mark.parametrize(
    "weights",
    [
        (),
        (("dense", -1.0),),
        (("dense", 0.0),),
        (("other", 1.0),),
        (("dense", float("nan")),),
        (("dense", 1.0), ("dense", 2.0)),
    ],
)
def test_invalid_config(weights):
    with pytest.raises(ValueError):
        HybridConfig(weights)


def test_dense_retriever_collapses_aliases_to_entities():
    es = [
        entity("a", "Kavendor", "Kavendor Lisa", "Kavendorr"),
        entity("b", "Moro Kalo"),
    ]
    dense = DenseRetriever(es, HashedCharNgramEncoder(64))
    result = dense.retrieve("Kavendor", 5)
    assert [c.entity_id for c in result.candidates] == ["a", "b"]
    assert result.candidates[0].matched_alias_id == "a-0" and result.candidates[
        0
    ].similarity_score == pytest.approx(1)


def test_top50_on_corpus_larger_than_50_one_row_per_entity():
    pytest.importorskip("usearch")
    entities = generate_entities(150, 5)
    target = entities[42]
    query = target.aliases[0].name[:-1]  # drop a final character
    encoder = HashedCharNgramEncoder(128)
    for backend in ("exact", "hnsw"):
        retriever = build_hybrid(
            entities, encoder, HybridConfig(), dense_backend=backend
        )
        result = retriever.retrieve(query, 50)
        ids = [c.entity_id for c in result.candidates]
        assert len(ids) == 50 and len(set(ids)) == 50
        assert target.entity_id in ids[:10]
        assert result == retriever.retrieve(query, 50)
        order = [(-c.fused_score, c.entity_id) for c in result.candidates]
        assert order == sorted(order)
        assert all(
            {e.component for e in c.evidence} <= {"dense", "lexical", "phonetic"}
            for c in result.candidates
        )
    sensitivity = build_hybrid(
        entities, encoder, HybridConfig(per_arm_depth=10)
    ).retrieve(query, 50)
    assert len(sensitivity.candidates) <= 30  # three arms x depth 10


def test_zero_weight_component_is_not_queried():
    es = [entity("a", "Moro"), entity("b", "Kalo")]
    config = HybridConfig((("lexical", 1.0), ("phonetic", 0.0)))
    retriever = HybridRetriever(
        {
            "lexical": build_retriever("levenshtein", es),
            "phonetic": build_retriever("phonetic_metaphone", es),
        },
        config,
    )
    result = retriever.retrieve("Moro", 2)
    assert dict(result.component_status) == {"lexical": None}


def test_linked_duplicates_do_not_consume_the_shortlist():
    # Twelve linked records with one fictional name plus one near neighbour, top 10.
    es = [entity(f"d{i:02d}", "Moro Kalo") for i in range(12)] + [
        entity("other", "Moro Kalu")
    ]
    groups = {f"d{i:02d}": "g" for i in range(12)}
    retriever = build_hybrid(
        es, HashedCharNgramEncoder(64), HybridConfig(per_arm_depth=5), groups=groups
    )
    rows = retriever.retrieve("Moro Kalo", 10).candidates
    assert [(row.group_id, row.entity_id) for row in rows] == [
        ("g", "d00"),
        ("other", "other"),
    ]
    assert rows[0].member_ids == tuple(f"d{i:02d}" for i in range(12)) and rows[
        1
    ].member_ids == ("other",)
    # Ranks are over distinct groups: 'other' is second in every arm despite 12 duplicates ahead of it.
    assert {e.rank for e in rows[1].evidence} == {2}
    assert all(e.entity_id == "d00" for e in rows[0].evidence)
    ungrouped = build_hybrid(
        es, HashedCharNgramEncoder(64), HybridConfig(per_arm_depth=5)
    ).retrieve("Moro Kalo", 10)
    assert "other" not in [
        c.entity_id for c in ungrouped.candidates
    ]  # the failure grouping prevents
    with pytest.raises(ValueError, match="outside"):
        build_hybrid(
            es, HashedCharNgramEncoder(64), HybridConfig(), groups={"ghost": "g"}
        )
    with pytest.raises(ValueError, match="collides"):
        build_hybrid(
            es, HashedCharNgramEncoder(64), HybridConfig(), groups={"d00": "other"}
        )


def test_provenance_is_carried_to_hybrid_rows():
    provenance = Provenance(
        "snap-1",
        "fictional-dataset",
        "fictional-provider",
        "Fictional Publisher",
        "https://example.invalid/source",
        "https://example.invalid/download",
        "2026-01-01T00:00:00Z",
        None,
        None,
        "0" * 64,
        "https://example.invalid/license",
    )
    es = [
        replace(entity("a", "Moro Kalo"), provenance=provenance),
        entity("b", "Kalo Moro"),
    ]
    rows = (
        build_hybrid(es, HashedCharNgramEncoder(64), HybridConfig())
        .retrieve("Moro Kalo", 2)
        .candidates
    )
    assert (
        rows[0].entity_id == "a"
        and rows[0].provenance == provenance
        and rows[1].provenance is None
    )


@pytest.mark.parametrize(
    "data",
    [
        {"weights": {"dense": 1.0}, "per_arm_dept": 10},
        {"weights": {"dense": True}},
        {"weights": {"dense": "2"}},
        {"weights": {}},
        {"rrf_k": 60},
    ],
)
def test_from_dict_is_strict(data):
    with pytest.raises(ValueError):
        HybridConfig.from_dict(data)


def test_from_dict_accepts_declared_keys():
    config = HybridConfig.from_dict(
        {
            "weights": {"lexical": 2, "dense": 1.5},
            "rrf_k": 10,
            "per_arm_depth": 7,
            "arm": "x",
        },
        allowed_extra=frozenset({"arm"}),
    )
    assert config == HybridConfig((("dense", 1.5), ("lexical", 2.0)), 10, 7)


def test_grouped_evidence_passes_view_check():
    from watchlist.evaluate_baselines import _check_evidence

    es = [
        entity("a1", "Moro Kalo"),
        entity("a2", "Moro Kalo", "Moro Kal"),
        entity("b", "Kalo Moro"),
    ]
    retriever = build_hybrid(
        es, HashedCharNgramEncoder(64), HybridConfig(), groups={"a1": "g", "a2": "g"}
    )
    rows = retriever.retrieve("Moro Kal", 2).candidates
    assert [row.group_id for row in rows] == ["g", "b"]
    _check_evidence(rows, tuple(es), 2)
