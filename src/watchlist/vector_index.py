"""Versioned alias-vector indexes: exact NumPy oracle and usearch HNSW (Step 12).

``ExactIndex`` is the correctness oracle (full cosine scan, the behavior of
``matching_embeddings``). ``HNSWIndex`` is an approximate index from usearch,
imported lazily; if usearch is missing a clear error is raised and nothing
falls back to another backend. Indexes are saved with a manifest (dimension,
metric, count, content digest, file hashes), built in a staging directory,
verified, and only then moved into place. Loading an HNSW index also checks
that the graph stores exactly the manifest's vectors under the expected keys,
and the ACTIVE pointer pins the manifest's own SHA-256. Graph bytes go through
Python file I/O (not usearch paths), so non-ASCII directories work on Windows.
Scores are raw cosine similarities.

``HashedCharNgramEncoder`` is a deterministic, non-semantic stand-in encoder
for tests and offline mechanics runs. It is not a language model.

CLI (fictional synthetic corpus only)::

    python -m watchlist.vector_index build --config config/vector-index.json --output DIR
    python -m watchlist.vector_index verify --index DIR/hnsw
"""

import argparse
from collections.abc import Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import time
from typing import Protocol

import numpy as np

from watchlist.normalize import normalize_name

INDEX_FORMAT = "watchlist-vector-index-v1"
ACTIVE_POINTER = "ACTIVE.json"


class IndexIntegrityError(ValueError):
    """A saved index is missing, incomplete, stale or tampered with."""


def unit_rows(values: np.ndarray, *, stored: bool = False) -> np.ndarray:
    """Return finite float32 unit rows; reject zero or corrupt vectors.

    ``stored=True`` accepts rows that are already unit length without
    rescaling them, so reloaded index bytes (and their digest) stay identical.
    """
    array = np.asarray(values, dtype=np.float32)
    if array.ndim != 2 or not np.isfinite(array).all():
        raise ValueError("vectors must be a finite two-dimensional matrix")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError("zero vector norm")
    if stored:
        if not np.allclose(norms, 1.0, atol=1e-4):
            raise ValueError("stored index vectors are not unit length")
        return array
    return (array / norms).astype(np.float32)


class HashedCharNgramEncoder:
    """Signed feature hashing of boundary-marked character n-grams.

    Deterministic across processes (BLAKE2b, not Python ``hash``). It measures
    surface character overlap only; different scripts share almost no n-grams.
    """

    def __init__(self, dimension: int = 256, ngram_range: tuple[int, int] = (2, 3)):
        if type(dimension) is not int or dimension < 8:
            raise ValueError("dimension must be an integer >= 8")
        low, high = ngram_range
        if type(low) is not int or type(high) is not int or not 1 <= low <= high:
            raise ValueError("invalid n-gram range")
        self.dimension, self.ngram_range = dimension, (low, high)
        self.config = {"dimension": dimension}
        self.metadata = {
            "encoder": "hashed-char-ngram-v1",
            "dimension": dimension,
            "ngram_range": [low, high],
            "normalization": "phase1-nfkc-casefold-whitespace-v1",
            "note": "deterministic non-semantic test encoder; not a language model",
        }

    def _vector(self, text: str) -> np.ndarray:
        marked = f"\x02{normalize_name(text)}\x03"
        vector = np.zeros(self.dimension, dtype=np.float32)
        for size in range(self.ngram_range[0], self.ngram_range[1] + 1):
            for start in range(max(1, len(marked) - size + 1)):
                digest = hashlib.blake2b(
                    marked[start : start + size].encode(), digest_size=8
                ).digest()
                value = int.from_bytes(digest, "little")
                vector[value % self.dimension] += 1.0 if value >> 63 else -1.0
        if not np.any(vector):
            vector[0] = (
                1.0  # all n-grams cancelled; keep a valid (uninformative) direction
            )
        return vector

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        return unit_rows(np.stack([self._vector(text) for text in texts]))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_digest(ids: Sequence[str], vectors: np.ndarray) -> str:
    """Digest of ordered IDs plus exact float32 bytes; changes force a rebuild."""
    digest = hashlib.sha256(json.dumps(list(ids), ensure_ascii=False).encode())
    digest.update(np.ascontiguousarray(vectors, dtype=np.float32).tobytes())
    return digest.hexdigest()


class VectorIndex(Protocol):
    backend: str
    ids: tuple[str, ...]
    vectors: np.ndarray

    def search(self, query: np.ndarray, k: int) -> list[tuple[str, float]]: ...


def _check_ids(ids: Sequence[str], count: int) -> tuple[str, ...]:
    result = tuple(ids)
    if (
        len(result) != count
        or len(set(result)) != count
        or any(not isinstance(item, str) or not item.strip() for item in result)
    ):
        raise ValueError(
            "index IDs must be distinct nonblank strings matching the vector rows"
        )
    return result


def _query(query: np.ndarray, dimension: int) -> np.ndarray:
    array = (
        np.asarray(query, dtype=np.float32).reshape(1, -1)
        if np.ndim(query) == 1
        else query
    )
    if array.shape != (1, dimension):
        raise ValueError("query vector dimension mismatch")
    return unit_rows(array)[0]


class ExactIndex:
    """Full cosine scan; ties order by ID. This is the correctness oracle."""

    backend = "exact"

    def __init__(
        self, ids: Sequence[str], vectors: np.ndarray, *, _stored: bool = False
    ):
        self.vectors = unit_rows(vectors, stored=_stored)
        self.ids = _check_ids(ids, len(self.vectors))
        order = sorted(range(len(self.ids)), key=self.ids.__getitem__)
        self._id_rank = np.empty(len(self.ids), dtype=np.int64)
        self._id_rank[order] = np.arange(len(self.ids))
        self.parameters: dict = {}

    def search(self, query: np.ndarray, k: int) -> list[tuple[str, float]]:
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError("k must be a positive integer")
        # Clip float32 rounding excursions; still raw cosine, not probability.
        scores = np.clip(self.vectors @ _query(query, self.vectors.shape[1]), -1.0, 1.0)
        order = np.lexsort((self._id_rank, -scores))[:k]
        return [(self.ids[i], float(scores[i])) for i in order]

    def memory_bytes(self) -> int:
        return int(self.vectors.nbytes)


def _usearch():
    try:
        from usearch.index import Index
    except ImportError as exc:
        raise RuntimeError(
            "HNSWIndex requires the index extra (usearch); no fallback backend is used"
        ) from exc
    return Index


HNSW_DEFAULTS = {"connectivity": 16, "expansion_add": 128, "expansion_search": 64}


class HNSWIndex:
    """usearch HNSW over cosine distance; integer keys map to alias IDs.

    Insertion is single-threaded so a build from the same inputs is
    reproducible. Returned rows are re-sorted by (-score, ID) for stable ties,
    but approximate search may still omit true neighbours; measure that.
    """

    backend = "hnsw"

    def __init__(
        self,
        ids: Sequence[str],
        vectors: np.ndarray,
        *,
        connectivity: int = 16,
        expansion_add: int = 128,
        expansion_search: int = 64,
        _index: object = None,
    ):
        Index = _usearch()
        self.vectors = unit_rows(vectors, stored=_index is not None)
        self.ids = _check_ids(ids, len(self.vectors))
        self.parameters = {
            "connectivity": connectivity,
            "expansion_add": expansion_add,
            "expansion_search": expansion_search,
        }
        for name, value in self.parameters.items():
            if type(value) is not int or value < 2:
                raise ValueError(f"{name} must be an integer >= 2")
        if _index is None:
            _index = Index(
                ndim=self.vectors.shape[1], metric="cos", dtype="f32", **self.parameters
            )
            if len(self.ids):
                _index.add(
                    np.arange(len(self.ids), dtype=np.uint64), self.vectors, threads=1
                )
        self._index = _index

    def search(self, query: np.ndarray, k: int) -> list[tuple[str, float]]:
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError("k must be a positive integer")
        vector = _query(query, self.vectors.shape[1])
        matches = self._index.search(vector, min(k, len(self.ids)), threads=1)
        rows = [
            (self.ids[int(key)], float(np.clip(1.0 - distance, -1.0, 1.0)))
            for key, distance in zip(matches.keys, matches.distances)
        ]
        return sorted(rows, key=lambda row: (-row[1], row[0]))

    def memory_bytes(self) -> int:
        return int(self._index.memory_usage)


def build_index(
    backend: str,
    ids: Sequence[str],
    vectors: np.ndarray,
    parameters: dict | None = None,
):
    """Construct a named backend. Unknown names are errors, never substitutions."""
    if backend == "exact":
        if parameters:
            raise ValueError("exact index takes no parameters")
        return ExactIndex(ids, vectors)
    if backend == "hnsw":
        return HNSWIndex(ids, vectors, **{**HNSW_DEFAULTS, **(parameters or {})})
    raise ValueError(f"unknown index backend: {backend}")


def save_index(
    index: object, destination: Path, *, metadata: dict | None = None
) -> dict:
    """Write files into a staging directory, then rename it into place.

    An existing destination is refused. A leftover staging directory means an
    earlier build was interrupted (process killed or KeyboardInterrupt); it is
    reported, not silently reused. An ordinary error while writing removes the
    staging directory this call created and re-raises.
    """
    destination = Path(destination)
    staging = destination.with_name(destination.name + ".staging")
    if destination.exists() or destination.is_symlink():
        raise IndexIntegrityError(
            "index destination already exists; choose a new version directory"
        )
    if staging.exists():
        raise IndexIntegrityError(
            f"interrupted build residue at {staging}; inspect and remove it first"
        )
    staging.mkdir(parents=True)
    try:
        manifest = _write_staging(index, staging, metadata)
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _write_staging(index: object, staging: Path, metadata: dict | None) -> dict:
    """Write IDs, vectors, graph bytes and finally the manifest into ``staging``."""
    files = {}
    ids_bytes = (
        json.dumps(list(index.ids), ensure_ascii=False, indent=0) + "\n"
    ).encode("utf-8")
    (staging / "ids.json").write_bytes(ids_bytes)
    files["ids.json"] = _sha256(ids_bytes)
    np.save(staging / "vectors.npy", index.vectors, allow_pickle=False)
    files["vectors.npy"] = _sha256((staging / "vectors.npy").read_bytes())
    if index.backend == "hnsw":
        graph = bytes(index._index.save())
        (staging / "index.usearch").write_bytes(graph)
        files["index.usearch"] = _sha256(graph)
    manifest = {
        "format": INDEX_FORMAT,
        "backend": index.backend,
        "metric": "cosine",
        "dimension": int(index.vectors.shape[1]),
        "count": len(index.ids),
        "content_digest": content_digest(index.ids, index.vectors),
        "parameters": index.parameters,
        "files": files,
        "metadata": metadata or {},
    }
    # The manifest is written last: its presence marks a complete build.
    with (staging / "manifest.json").open(
        "x", encoding="utf-8", newline="\n"
    ) as stream:
        stream.write(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
    return manifest


def _verify_graph(raw: object, vectors: np.ndarray) -> None:
    """The HNSW graph must hold keys 0..n-1 storing exactly the manifest vectors.

    File checksums alone cannot detect a graph rebuilt from other vectors and
    re-hashed into the manifest; this compares the stored vectors themselves.
    """
    count = len(vectors)
    keys = (
        np.asarray(raw.keys, dtype=np.uint64) if count else np.empty(0, dtype=np.uint64)
    )
    if not np.array_equal(np.sort(keys), np.arange(count, dtype=np.uint64)):
        raise IndexIntegrityError("HNSW graph keys differ from the stored ID rows")
    if count:
        stored = np.asarray(
            raw.get(np.arange(count, dtype=np.uint64)), dtype=np.float32
        )
        if stored.shape != vectors.shape or not np.allclose(
            stored, vectors, rtol=0.0, atol=1e-6
        ):
            raise IndexIntegrityError("HNSW graph vectors differ from vectors.npy")


def load_index(
    directory: Path,
    *,
    expected_content_digest: str | None = None,
    expected_manifest_sha256: str | None = None,
):
    """Load and verify an index; mismatches raise IndexIntegrityError.

    ``expected_content_digest`` binds the index to the caller's current alias
    vectors; a different digest means the index is stale and must be rebuilt.
    ``expected_manifest_sha256`` pins the exact manifest (used by ACTIVE.json).
    """
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise IndexIntegrityError(
            f"missing or incomplete index (no manifest): {directory}"
        )
    manifest_bytes = manifest_path.read_bytes()
    if (
        expected_manifest_sha256 is not None
        and _sha256(manifest_bytes) != expected_manifest_sha256
    ):
        raise IndexIntegrityError("manifest differs from the activated version")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("format") != INDEX_FORMAT or manifest.get("metric") != "cosine":
        raise IndexIntegrityError("unsupported index format or metric")
    expected_files = {"ids.json", "vectors.npy"} | (
        {"index.usearch"} if manifest["backend"] == "hnsw" else set()
    )
    if set(manifest["files"]) != expected_files:
        raise IndexIntegrityError("index file list differs from its backend")
    for name, digest in manifest["files"].items():
        path = directory / name
        if not path.is_file() or _sha256(path.read_bytes()) != digest:
            raise IndexIntegrityError(
                f"index file missing or checksum mismatch: {name}"
            )
    ids = json.loads((directory / "ids.json").read_text(encoding="utf-8"))
    vectors = np.load(directory / "vectors.npy", allow_pickle=False)
    if vectors.shape != (manifest["count"], manifest["dimension"]):
        raise IndexIntegrityError("vector shape differs from manifest")
    if content_digest(ids, vectors) != manifest["content_digest"]:
        raise IndexIntegrityError("content digest differs from manifest")
    if (
        expected_content_digest is not None
        and expected_content_digest != manifest["content_digest"]
    ):
        raise IndexIntegrityError(
            "stale index: reference vectors changed; rebuild a new version"
        )
    if manifest["backend"] == "exact":
        return ExactIndex(ids, vectors, _stored=True), manifest
    if manifest["backend"] != "hnsw":
        raise IndexIntegrityError("unknown backend in manifest")
    Index = _usearch()
    raw = Index(
        ndim=manifest["dimension"], metric="cos", dtype="f32", **manifest["parameters"]
    )
    raw.load((directory / "index.usearch").read_bytes())
    if len(raw) != manifest["count"] or raw.ndim != manifest["dimension"]:
        raise IndexIntegrityError("HNSW structure differs from manifest")
    _verify_graph(raw, vectors)
    return HNSWIndex(ids, vectors, **manifest["parameters"], _index=raw), manifest


def _version_dir(root: Path, version: object) -> Path:
    """A version is one plain directory name directly under ``root``."""
    if (
        not isinstance(version, str)
        or version in {"", ".", ".."}
        or Path(version).name != version
        or Path(version).is_absolute()
        or any(sep in version for sep in ("/", "\\", ":"))
    ):
        raise IndexIntegrityError(
            "index version must be a single directory name under the root"
        )
    directory = root / version
    if (
        not directory.is_dir()
        or directory.is_symlink()
        or directory.resolve().parent != root.resolve()
    ):
        raise IndexIntegrityError(
            f"index version is not a directory under the root: {version}"
        )
    return directory


def activate_index(root: Path, version: str) -> dict:
    """Verify ``root/version`` and atomically point ACTIVE.json at it."""
    root = Path(root)
    directory = _version_dir(root, version)
    _, manifest = load_index(directory)
    pointer = {
        "version": version,
        "content_digest": manifest["content_digest"],
        "backend": manifest["backend"],
        "manifest_sha256": _sha256((directory / "manifest.json").read_bytes()),
    }
    temporary = root / (ACTIVE_POINTER + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(pointer, sort_keys=True) + "\n")
    os.replace(temporary, root / ACTIVE_POINTER)
    return pointer


def load_active(root: Path):
    """Load the activated version; a missing pointer is an explicit error."""
    path = Path(root) / ACTIVE_POINTER
    if not path.is_file():
        raise IndexIntegrityError("no active index version")
    pointer = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pointer, dict) or not {
        "version",
        "content_digest",
        "manifest_sha256",
    } <= set(pointer):
        raise IndexIntegrityError(
            "ACTIVE pointer is incomplete; re-activate a verified version"
        )
    return load_index(
        _version_dir(Path(root), pointer["version"]),
        expected_content_digest=pointer["content_digest"],
        expected_manifest_sha256=pointer["manifest_sha256"],
    )


def _latency(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "observations": len(ordered),
        "median_ms": float(np.median(ordered)) if ordered else None,
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None,
        "p95_method": "nearest-rank",
    }


def measure_recall(
    approximate: object, exact: ExactIndex, queries: np.ndarray, k: int
) -> dict:
    """Recall@k of approximate vs exact neighbour IDs, plus per-query latency.

    Recall per query is |approx top-k ∩ exact top-k| / |exact top-k|. Exact ties
    at the cutoff are broken by ID, so a tie-equivalent approximate answer can
    count as a miss; the measurement is conservative in that respect.
    """
    if approximate.ids != exact.ids or not np.array_equal(
        approximate.vectors, exact.vectors
    ):
        raise ValueError("approximate and exact indexes must hold identical rows")
    queries = unit_rows(queries)
    recalls, approx_ms, exact_ms = [], [], []
    for vector in queries:
        start = time.perf_counter_ns()
        truth = exact.search(vector, k)
        middle = time.perf_counter_ns()
        found = approximate.search(vector, k)
        end = time.perf_counter_ns()
        exact_ms.append((middle - start) / 1e6)
        approx_ms.append((end - middle) / 1e6)
        truth_ids = {item for item, _ in truth}
        recalls.append(len(truth_ids & {item for item, _ in found}) / len(truth_ids))
    return {
        "k": k,
        "query_count": len(recalls),
        "mean_recall": float(np.mean(recalls)) if recalls else None,
        "min_recall": min(recalls) if recalls else None,
        "queries_below_one": sum(value < 1 for value in recalls),
        "approximate_latency": _latency(approx_ms),
        "exact_latency": _latency(exact_ms),
        "approximate_memory_bytes": approximate.memory_bytes(),
        "exact_memory_bytes": exact.memory_bytes(),
        "backend": approximate.backend,
        "parameters": approximate.parameters,
    }


def _encoder(config: dict) -> HashedCharNgramEncoder:
    if config.get("kind") != "hashed-char-ngram-v1":
        raise ValueError(
            "only the deterministic hashed-char-ngram-v1 encoder is supported offline here"
        )
    return HashedCharNgramEncoder(config["dimension"], tuple(config["ngram_range"]))


def build_from_config(config: dict, output: Path) -> dict:
    """Build exact and HNSW indexes over the fictional corpus, verify, measure, activate."""
    from watchlist.synthetic_corpus import corpus_digest, generate_corpus
    from watchlist.perturbations import variants

    output = Path(output)
    if output.exists():
        raise ValueError("output already exists; choose a new directory")
    corpus_config = config["corpus"]
    corpus = generate_corpus(corpus_config["entity_count"], corpus_config["seed"])
    encoder = _encoder(config["encoder"])
    aliases = [alias for entity in corpus.entities for alias in entity.aliases]
    start = time.perf_counter()
    vectors = encoder.encode([alias.name for alias in aliases])
    encode_seconds = time.perf_counter() - start
    ids = [alias.alias_id for alias in aliases]
    metadata = {
        "encoder": encoder.metadata,
        "corpus": corpus.manifest,
        "corpus_digest": corpus_digest(corpus),
    }
    output.mkdir(parents=True)
    timings, manifests = {}, {}
    for backend in ("exact", "hnsw"):
        start = time.perf_counter()
        index = build_index(
            backend, ids, vectors, config["hnsw"] if backend == "hnsw" else None
        )
        timings[backend] = time.perf_counter() - start
        manifests[backend] = save_index(index, output / backend, metadata=metadata)
    exact, _ = load_index(
        output / "exact", expected_content_digest=manifests["exact"]["content_digest"]
    )
    approximate, _ = load_index(
        output / "hnsw", expected_content_digest=manifests["hnsw"]["content_digest"]
    )
    # Queries: seeded deletion/swap perturbations of a fixed alias sample (no labels needed).
    sample = aliases[:: max(1, len(aliases) // config["query_count"])][
        : config["query_count"]
    ]
    texts = [
        next(
            (
                v["text"]
                for v in variants(alias.name, alias.alias_id, corpus_config["seed"])
                if v["kind"] == "deletion" and v["text"]
            ),
            alias.name,
        )
        for alias in sample
    ]
    query_vectors = encoder.encode(texts)
    measurements = {
        str(k): measure_recall(approximate, exact, query_vectors, k)
        for k in config["recall_k"]
    }
    pointer = activate_index(output, "hnsw")
    receipt = {
        "usage": "fictional synthetic mechanics only; not real-name evidence",
        "alias_count": len(ids),
        "entity_count": len(corpus.entities),
        "dimension": encoder.dimension,
        "encode_seconds": encode_seconds,
        "build_seconds": timings,
        "content_digest": manifests["exact"]["content_digest"],
        "recall_vs_exact": measurements,
        "active": pointer,
        "config": config,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    }
    with (output / "receipt.json").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=["build", "verify"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--index", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "build":
            if args.config is None or args.output is None:
                raise ValueError("build requires --config and --output")
            receipt = build_from_config(
                json.loads(args.config.read_text(encoding="utf-8")), args.output
            )
            print(
                json.dumps(
                    {
                        key: receipt[key]
                        for key in ("alias_count", "build_seconds", "recall_vs_exact")
                    },
                    indent=2,
                )
            )
        else:
            if args.index is None:
                raise ValueError("verify requires --index")
            _, manifest = load_index(args.index)
            print(
                json.dumps(
                    {
                        key: manifest[key]
                        for key in ("backend", "count", "dimension", "content_digest")
                    }
                )
            )
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
