import json
from pathlib import Path

import pytest

from watchlist.baselines import (
    ARMS,
    Coverage,
    RetrievalResult,
    UnsupportedText,
    build_retriever,
    phonetic_code,
    transliterated,
)
from watchlist.entities import Alias, Entity, load_entities
from watchlist.evaluate_baselines import (
    _check_evidence,
    _destinations,
    evaluate_arm,
    make_freeze_receipt,
    prepare_run,
    validate_config,
)
from watchlist.evaluation_data import build_benchmark
from watchlist.matching import Candidate, rank_candidates

pytest.importorskip(
    "jellyfish", reason="install the preprocessing extra for phonetic arms"
)
pytest.importorskip(
    "anyascii", reason="install the preprocessing extra for transliteration arms"
)

ROOT = Path(__file__).resolve().parents[1]


def entity(eid, *names, kind="organisation"):
    return Entity(
        eid,
        names[0],
        kind,
        tuple(Alias(f"{eid}-{i}", n) for i, n in enumerate(names)),
        True,
        "fictional",
    )


def ids(result):
    return [c.entity_id for c in result.candidates]


def test_levenshtein_arm_reproduces_existing_ranker_exactly():
    entities = load_entities()
    retriever = build_retriever("levenshtein", entities)
    for query in ("Amina Rahman", "Северная Звезда", "نور", "Blue Cedr Library", "zzz"):
        assert list(retriever.retrieve(query, 5).candidates) == rank_candidates(
            query, entities, 5
        )


def test_one_candidate_per_entity_best_alias_and_id_ties():
    es = [
        entity("b", "Moro Kalo", "Kalo Moro"),
        entity("a", "Moro Kalo X"),
        entity("c", "Moro Kalo"),
    ]
    result = build_retriever("levenshtein", es).retrieve("Moro Kalo", 10)
    assert ids(result) == ["b", "c", "a"]  # b and c tie at 100; ID order breaks it
    assert result.candidates[0].matched_alias_id == "b-0"
    # Equal alias scores within one entity choose the smaller alias ID.
    tie = build_retriever(
        "token_sort", [entity("x", "Kalo Moro", "Moro Kalo")]
    ).retrieve("moro kalo", 1)
    assert tie.candidates[0].matched_alias_id == "x-0"


def test_hand_checked_scores():
    es = [entity("a", "abcd"), entity("b", "Moro Kalo")]
    lev = {
        c.entity_id: c.similarity_score
        for c in build_retriever("levenshtein", es).retrieve("abce", 2).candidates
    }
    assert lev["a"] == pytest.approx(75.0)  # one substitution over four characters
    assert (
        build_retriever("token_sort", es)
        .retrieve("kalo moro", 1)
        .candidates[0]
        .similarity_score
        == 100.0
    )
    assert (
        build_retriever("token_set", es)
        .retrieve("Kalo", 1)
        .candidates[0]
        .similarity_score
        == 100.0
    )
    jw = build_retriever("jaro_winkler", es).retrieve("abcd", 1).candidates[0]
    assert (jw.entity_id, jw.similarity_score) == ("a", 100.0)


def test_exact_arm_returns_only_exact_normalized_matches():
    es = [entity("a", "Moro Kalo"), entity("b", "Moro Kala")]
    assert ids(
        build_retriever("exact_normalized", es).retrieve("  MORO   kalo ", 5)
    ) == ["a"]
    assert build_retriever("exact_normalized", es).retrieve("Moro", 5).candidates == ()


def test_phonetic_unsupported_cases_are_explicit():
    es = [
        entity("a", "Kavendor Lisa", "卡文多·里莎", "كافيندور"),
        entity("b", "Кавендор Лиса"),
    ]
    retriever = build_retriever("phonetic_metaphone", es)
    assert retriever.coverage.indexed_alias_count == 2
    reasons = dict(retriever.coverage.unsupported_aliases)
    assert "HAN" in reasons["a-1"] and "ARABIC" in reasons["a-2"]
    han = retriever.retrieve("卡文多", 5)
    assert han == RetrievalResult((), "phonetic code undefined for script HAN")
    # Cyrillic is romanized first, so it shares codes with the Latin spelling.
    assert phonetic_code("Кавендор", "metaphone") == phonetic_code(
        "Kavendor", "metaphone"
    )
    # Latin and Cyrillic aliases romanize to the same codes: an exact tie that entity ID breaks.
    tied = retriever.retrieve("Cavendor Liza", 2).candidates
    assert [c.entity_id for c in tied] == ["a", "b"] and tied[
        0
    ].similarity_score == tied[1].similarity_score
    with pytest.raises(UnsupportedText):
        phonetic_code("123", "nysiis")


def test_transliteration_arm_crosses_scripts():
    assert transliterated("Кавендор") == "kavendor"
    es = [entity("a", "Кавендор"), entity("b", "Moro")]
    result = build_retriever("translit_levenshtein", es).retrieve("Kavendor", 2)
    assert (
        result.candidates[0].entity_id == "a"
        and result.candidates[0].similarity_score == 100.0
    )
    assert (
        result.candidates[0].matched_alias == "Кавендор"
    )  # original text kept as evidence


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_every_arm_is_deterministic_and_validates_k(arm):
    entities = load_entities()
    retriever = build_retriever(arm, entities)
    assert retriever.retrieve("Amina Rahman", 3) == retriever.retrieve(
        "Amina Rahman", 3
    )
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError):
            retriever.retrieve("Amina", bad)
    scores = [
        (-c.similarity_score, c.entity_id)
        for c in retriever.retrieve("Amina Rahman", 12).candidates
    ]
    assert scores == sorted(scores)


def test_invalid_inputs():
    with pytest.raises(ValueError, match="unknown"):
        build_retriever("soundex", [entity("a", "A")])
    with pytest.raises(ValueError, match="duplicate"):
        build_retriever("levenshtein", [entity("a", "A"), entity("a", "B")])


def _config(**changes):
    config = json.loads(
        (ROOT / "eval/configs/baselines-v2.json").read_text(encoding="utf-8")
    )
    config.update(changes)
    return config


def test_shipped_config_validates_and_bad_configs_fail():
    validate_config(_config())
    for bad in (
        {"arms": ["nope"]},
        {"datasets": ["other"]},
        {"partitions": ["train"]},
        {"requested_k": 0},
        {"protocol_version": "x"},
    ):
        with pytest.raises(ValueError):
            validate_config(_config(**bad))


class ConstantOrder:
    """Fake arm ranking a view's entities in a fixed ID order regardless of the query."""

    arm, scorer_name = "constant", "constant-order"

    def __init__(self, entities):
        self.entities = sorted(entities, key=lambda e: e.entity_id)
        self.coverage = Coverage(len(entities), 0, 0, len(entities))

    def retrieve(self, query, top_k):
        return RetrievalResult(
            tuple(
                Candidate(
                    e.entity_id,
                    e.name,
                    e.entity_type,
                    e.aliases[0].alias_id,
                    e.aliases[0].name,
                    float(10 - n),
                    e.synthetic,
                    e.source,
                )
                for n, e in enumerate(self.entities[:top_k])
            )
        )


def test_evaluate_arm_hand_computed_metrics(monkeypatch):
    # Six fictional entities; one challenge positive per target, ranked e0 < e1 < ... by a fake arm.
    es = [
        entity(f"e{i}", f"Nam{c} Mor{c}", f"Alt{c} Kal{c}")
        for i, c in enumerate("abcdef")
    ]
    assignments = {
        e.entity_id: {"group_id": e.entity_id, "partition": "development"} for e in es
    }
    views, queries, _ = build_benchmark(es, assignments, seed=42)
    chosen = {}
    for query in queries:
        if query["status"] == "positive" and query["kind"] not in {
            "exact",
            "formatting",
        }:
            chosen.setdefault(query["target_id"], query)
    assert sorted(chosen) == ["e0", "e1", "e2", "e3", "e4", "e5"]
    monkeypatch.setattr(
        "watchlist.evaluate_baselines.make_arm",
        lambda arm, entities, config: ConstantOrder(entities),
    )
    result = evaluate_arm(
        "constant",
        views,
        list(chosen.values()),
        _config(requested_k=3, timing_repetitions=1),
    )
    agg = next(
        a
        for a in result["aggregates"]
        if (a["partition"], a["suite"], a["slice"])
        == ("development", "challenge", "all")
    )
    # Returned top 3 = e0, e1, e2: target ranks 1, 2, 3, then three unreturned targets.
    assert (agg["recall_at_1"]["numerator"], agg["recall_at_1"]["denominator"]) == (
        1,
        6,
    )
    assert (
        agg["recall_at_10"]["value"] is None
    )  # a 6-entity index cannot inform Recall@10
    assert agg["mrr_at_k"]["value"] == pytest.approx(
        (1 + 1 / 2 + 1 / 3) / 6
    )  # = 11/36, missing count as 0
    assert result["missing_target_positives"] == {"development/challenge": 3}
    assert sorted(r["target_rank"] for r in result["queries"] if r["target_rank"]) == [
        1,
        2,
        3,
    ]


def test_evidence_must_come_from_view():
    es = (entity("a", "Moro"),)
    good = build_retriever("levenshtein", es).retrieve("Moro", 1).candidates
    _check_evidence(good, es, 1)
    with pytest.raises(ValueError, match="outside"):
        _check_evidence(good, (entity("a", "Other"),), 1)


def test_destinations_refuse_existing_and_historical(tmp_path):
    (tmp_path / "fictional").mkdir()
    with pytest.raises(ValueError, match="already exists"):
        _destinations(tmp_path, ["fictional"])
    with pytest.raises(ValueError, match="historical"):
        _destinations(ROOT / "eval/results/new", ["fictional"])
    assert (
        _destinations(tmp_path / "new", ["multilingual"])["multilingual"]
        == (tmp_path / "new/multilingual").resolve()
    )


@pytest.mark.parametrize("arm", sorted(ARMS))
def test_blank_query_is_a_value_error_in_every_arm(arm):
    retriever = build_retriever(arm, [entity("a", "Moro Kalo")])
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="blank"):
            retriever.retrieve(blank, 1)


def test_transliteration_rejects_symbols_instead_of_ranking_names():
    retriever = build_retriever(
        "translit_levenshtein", [entity("a", "Grinning"), entity("b", "Moro")]
    )
    for text in ("\U0001f600", "Moro \U0001f600", "123"):
        result = retriever.retrieve(text, 2)
        assert result.candidates == () and result.unsupported_reason
        with pytest.raises(UnsupportedText):
            transliterated(text)
    assert transliterated("O'Neil-Smith") == "o'neil-smith"  # ASCII punctuation is kept


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c.pop("synthetic_scale"),
        lambda c: c.pop("dense_encoder"),
        lambda c: c["hybrid_variants"][1].update(per_arm_dept=10),
        lambda c: c["hybrid_variants"][0]["weights"].update(dense=True),
        lambda c: c["hybrid_variants"][0]["weights"].update(dense="2"),
        lambda c: c["hybrid_variants"][2]["dense_parameters"].update(ef=5),
        lambda c: c["hybrid_variants"][0].update(dense_backend="faiss"),
        lambda c: c["synthetic_scale"].update(entity_count="400"),
        lambda c: c.update(extra=1),
        lambda c: c.update(partitions=["development", "development"]),
    ],
)
def test_config_schema_rejects_missing_unknown_and_mistyped_keys(change):
    config = _config()
    change(config)
    with pytest.raises(ValueError):
        validate_config(config)


def test_test_partition_is_sealed_without_unseal_and_matching_receipt(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config()), encoding="utf-8")
    with pytest.raises(ValueError, match="sealed"):
        prepare_run(path, tmp_path / "out", partition="test")
    with pytest.raises(ValueError, match="sealed"):
        prepare_run(path, tmp_path / "out", partition="test", unseal_test=True)
    config_with_test = _config(partitions=["development", "test"])
    (tmp_path / "with-test.json").write_text(
        json.dumps(config_with_test), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="sealed"):
        prepare_run(tmp_path / "with-test.json", tmp_path / "out")
    receipt = tmp_path / "freeze.json"
    receipt.write_text(
        json.dumps(
            make_freeze_receipt(_config(), frozen_by="fictional reviewer", note="test")
        ),
        encoding="utf-8",
    )
    config, _ = prepare_run(
        path,
        tmp_path / "out",
        partition="test",
        unseal_test=True,
        freeze_receipt=receipt,
    )
    assert config["partitions"] == ["test"]
    other = tmp_path / "other.json"
    other.write_text(json.dumps(_config(requested_k=10)), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        prepare_run(
            other,
            tmp_path / "out",
            partition="test",
            unseal_test=True,
            freeze_receipt=receipt,
        )
    with pytest.raises(ValueError, match="not selected"):
        prepare_run(path, tmp_path / "out", partition="development", unseal_test=True)
    config, _ = prepare_run(
        path,
        tmp_path / "out",
        partition="development",
        arm_filter=lambda arm, hybrids: arm in hybrids,
    )
    assert config["arms"] == ["hybrid_rrf", "hybrid_rrf_depth10", "hybrid_rrf_hnsw"]
