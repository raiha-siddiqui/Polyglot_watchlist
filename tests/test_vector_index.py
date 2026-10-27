import json

import pytest

np = pytest.importorskip(
    "numpy", reason="install the embeddings extra for vector tests"
)

from watchlist.vector_index import (
    ExactIndex,
    HashedCharNgramEncoder,
    IndexIntegrityError,
    activate_index,  # noqa: E402
    build_from_config,
    build_index,
    content_digest,
    load_active,
    load_index,
    measure_recall,
    save_index,
)


def corpus_vectors(count=400, dimension=32, seed=0):
    rng = np.random.default_rng(seed)
    return [f"id-{i:04d}" for i in range(count)], rng.normal(
        size=(count, dimension)
    ).astype(np.float32)


def test_hashed_encoder_is_deterministic_and_unit_norm():
    encoder = HashedCharNgramEncoder(64)
    first = encoder.encode(["Kavendor Lisa", "kavendor   LISA", "Moro"])
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0, atol=1e-6)
    assert np.array_equal(first[0], first[1])  # same normalized text
    assert np.array_equal(
        first,
        HashedCharNgramEncoder(64).encode(["Kavendor Lisa", "kavendor   LISA", "Moro"]),
    )
    assert first[0] @ encoder.encode(["Kavendr Lisa"])[0] > first[0] @ first[2]
    assert encoder.encode([]).shape == (0, 64)
    with pytest.raises(ValueError):
        HashedCharNgramEncoder(4)


def test_exact_index_hand_cosine_and_id_ties():
    index = ExactIndex(
        ["b", "a", "c"], np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
    )
    assert index.search(np.array([1, 0]), 3) == [("a", 1.0), ("b", 1.0), ("c", 0.0)]
    assert index.search(np.array([[3, 4]]), 1)[0] == ("c", pytest.approx(0.8))
    with pytest.raises(ValueError, match="dimension"):
        index.search(np.array([1, 0, 0]), 1)
    with pytest.raises(ValueError):
        ExactIndex(["a", "a"], np.eye(2))
    with pytest.raises(ValueError):
        ExactIndex(["a"], np.zeros((1, 2)))


def test_hnsw_recall_against_exact_oracle():
    pytest.importorskip("usearch")
    ids, vectors = corpus_vectors()
    exact, approx = (
        build_index("exact", ids, vectors),
        build_index("hnsw", ids, vectors),
    )
    queries = vectors[:50] + 0.05 * np.random.default_rng(1).normal(
        size=(50, vectors.shape[1])
    )
    report = measure_recall(approx, exact, queries.astype(np.float32), 10)
    assert report["query_count"] == 50 and 0.9 <= report["mean_recall"] <= 1.0
    assert report["approximate_latency"]["observations"] == 50
    assert approx.search(vectors[3], 1)[0][0] == "id-0003"
    assert approx.search(vectors[3], 5) == approx.search(vectors[3], 5)
    # Single-threaded insertion makes the serialized graph reproducible.
    assert build_index("hnsw", ids, vectors)._index.save() == approx._index.save()


def test_save_load_verify_and_tamper(tmp_path):
    pytest.importorskip("usearch")
    ids, vectors = corpus_vectors(120, 16)
    for backend in ("exact", "hnsw"):
        index = build_index(backend, ids, vectors)
        manifest = save_index(index, tmp_path / backend, metadata={"note": "fictional"})
        assert manifest["count"] == 120 and manifest["dimension"] == 16
        assert manifest["content_digest"] == content_digest(ids, index.vectors)
        loaded, _ = load_index(
            tmp_path / backend, expected_content_digest=manifest["content_digest"]
        )
        assert loaded.search(vectors[7], 5) == index.search(vectors[7], 5)
        with pytest.raises(IndexIntegrityError, match="already exists"):
            save_index(index, tmp_path / backend)
    with pytest.raises(IndexIntegrityError, match="stale"):
        load_index(tmp_path / "exact", expected_content_digest="0" * 64)
    (tmp_path / "hnsw" / "index.usearch").write_bytes(b"corrupt")
    with pytest.raises(IndexIntegrityError, match="checksum"):
        load_index(tmp_path / "hnsw")
    with pytest.raises(IndexIntegrityError, match="missing"):
        load_index(tmp_path / "absent")


def test_interrupted_build_and_atomic_activation(tmp_path):
    ids, vectors = corpus_vectors(20, 8)
    index = build_index("exact", ids, vectors)
    (tmp_path / "v2.staging").mkdir()  # simulates a crash before the rename
    with pytest.raises(IndexIntegrityError, match="interrupted"):
        save_index(index, tmp_path / "v2")
    (tmp_path / "v2.staging").rmdir()
    save_index(index, tmp_path / "v1")
    with pytest.raises(IndexIntegrityError, match="no active"):
        load_active(tmp_path)
    pointer = activate_index(tmp_path, "v1")
    loaded, manifest = load_active(tmp_path)
    assert pointer["content_digest"] == manifest[
        "content_digest"
    ] and loaded.ids == tuple(ids)
    # An incomplete version (no manifest) cannot be activated.
    (tmp_path / "v3").mkdir()
    with pytest.raises(IndexIntegrityError):
        activate_index(tmp_path, "v3")
    assert (
        json.loads((tmp_path / "ACTIVE.json").read_text(encoding="utf-8"))["version"]
        == "v1"
    )


def test_unknown_backend_and_parameters_are_errors():
    ids, vectors = corpus_vectors(5, 4)
    with pytest.raises(ValueError, match="unknown"):
        build_index("faiss", ids, vectors)
    with pytest.raises(ValueError):
        build_index("exact", ids, vectors, {"connectivity": 8})


def test_missing_usearch_is_a_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("usearch"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    ids, vectors = corpus_vectors(5, 4)
    with pytest.raises(RuntimeError, match="no fallback"):
        build_index("hnsw", ids, vectors)


def test_build_from_config_receipt(tmp_path):
    pytest.importorskip("usearch")
    config = {
        "corpus": {"entity_count": 80, "seed": 1},
        "encoder": {
            "kind": "hashed-char-ngram-v1",
            "dimension": 64,
            "ngram_range": [2, 3],
        },
        "hnsw": {"connectivity": 8, "expansion_add": 64, "expansion_search": 32},
        "recall_k": [10],
        "query_count": 20,
    }
    receipt = build_from_config(config, tmp_path / "out")
    assert receipt["recall_vs_exact"]["10"]["query_count"] == 20
    assert receipt["active"]["version"] == "hnsw"
    load_active(tmp_path / "out")
    with pytest.raises(ValueError, match="exists"):
        build_from_config(config, tmp_path / "out")


def test_swapped_hnsw_graph_with_rehashed_manifest_is_rejected(tmp_path):
    pytest.importorskip("usearch")
    import hashlib

    ids, vectors = corpus_vectors(150, 16)
    save_index(build_index("hnsw", ids, vectors), tmp_path / "v1")
    activate_index(tmp_path, "v1")
    other = build_index(
        "hnsw", ids, corpus_vectors(150, 16, seed=9)[1]
    )  # same count and dimension
    graph = bytes(other._index.save())
    (tmp_path / "v1" / "index.usearch").write_bytes(graph)
    manifest_path = tmp_path / "v1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["index.usearch"] = hashlib.sha256(graph).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(IndexIntegrityError, match="manifest differs"):
        load_active(tmp_path)  # the pointer pins the original manifest bytes
    with pytest.raises(IndexIntegrityError, match="graph vectors differ"):
        load_index(
            tmp_path / "v1"
        )  # even without a pointer the graph is checked against vectors.npy


def test_non_ascii_index_directory_round_trips(tmp_path):
    pytest.importorskip("usearch")
    ids, vectors = corpus_vectors(60, 8)
    root = tmp_path / "índice-индекс-索引"
    for backend in ("exact", "hnsw"):
        index = build_index(backend, ids, vectors)
        save_index(index, root / backend)
        loaded, _ = load_index(root / backend)
        assert loaded.search(vectors[4], 3) == index.search(vectors[4], 3)
    activate_index(root, "hnsw")
    assert load_active(root)[1]["backend"] == "hnsw"


def test_failed_save_removes_staging_and_allows_retry(tmp_path, monkeypatch):
    import watchlist.vector_index as vector_index

    ids, vectors = corpus_vectors(10, 4)
    index = build_index("exact", ids, vectors)

    def broken(*args):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(vector_index, "_write_staging", broken)
    with pytest.raises(OSError, match="simulated"):
        save_index(index, tmp_path / "v1")
    assert not (tmp_path / "v1.staging").exists() and not (tmp_path / "v1").exists()
    monkeypatch.undo()
    save_index(index, tmp_path / "v1")
    assert load_index(tmp_path / "v1")[1]["count"] == 10


@pytest.mark.parametrize(
    "version", ["../outside/v1", "v1/../v1", "..", ".", "", "C:/abs", "a\b"]
)
def test_activation_requires_a_version_directly_under_root(tmp_path, version):
    ids, vectors = corpus_vectors(10, 4)
    save_index(build_index("exact", ids, vectors), tmp_path / "outside" / "v1")
    root = tmp_path / "root"
    save_index(build_index("exact", ids, vectors), root / "v1")
    with pytest.raises(
        IndexIntegrityError, match="single directory name|not a directory"
    ):
        activate_index(root, version)
    assert not (root / "ACTIVE.json").exists()


def test_alias_deletion_and_update_need_a_new_verified_version(tmp_path):
    ids, vectors = corpus_vectors(30, 8)
    v1 = build_index("exact", ids, vectors)
    save_index(v1, tmp_path / "v1")
    activate_index(tmp_path, "v1")
    # Delete one alias row and update another alias vector; the old version stays immutable.
    keep = [i for i in range(30) if i != 5]
    updated = vectors[keep].copy()
    updated[0] = corpus_vectors(1, 8, seed=7)[1][0]
    v2 = build_index("exact", [ids[i] for i in keep], updated)
    current_digest = content_digest(v2.ids, v2.vectors)
    with pytest.raises(IndexIntegrityError, match="stale"):
        load_index(tmp_path / "v1", expected_content_digest=current_digest)
    with pytest.raises(IndexIntegrityError, match="already exists"):
        save_index(
            v2, tmp_path / "v1"
        )  # in-place mutation of an existing version is refused
    save_index(v2, tmp_path / "v2")
    assert load_active(tmp_path)[0].ids == tuple(ids)  # still v1 until activation
    activate_index(tmp_path, "v2")
    loaded, manifest = load_active(tmp_path)
    assert "id-0005" not in loaded.ids and len(loaded.ids) == 29
    assert manifest["content_digest"] == current_digest
    assert loaded.search(updated[0], 1)[0][0] == "id-0000"
    assert load_index(tmp_path / "v1")[1]["count"] == 30
