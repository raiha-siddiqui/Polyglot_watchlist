"""Bounded CPU training mechanics, checkpoint resume and the fictional pilot.

The pilot trains a tiny, randomly initialised character-n-gram bi-encoder that
is defined here; it downloads nothing. A lazily imported sentence-transformers
adapter exists for later approved runs on verified local weights. Every run has
explicit step, wall-clock and memory caps, refuses to write into pretrained or
frozen directories, and writes checkpoints and artifacts atomically. Results on
generated fictional names demonstrate mechanics only.
"""

import argparse
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import importlib
import io
import json
import os
from pathlib import Path
import sys
import time
import zlib

import numpy as np
import torch
from torch import nn
import torch.nn.functional as F

from watchlist.entities import Entity
from watchlist.matching import rank_candidates
from watchlist.matching_embeddings import rank_vectors
from watchlist.model_artifacts import (
    ArtifactError,
    atomic_write_bytes,
    atomic_write_json,
    canonical_digest,
    file_sha256,
    is_protected_directory,
    verify_artifact,
    write_artifact,
)
from watchlist.normalize import normalize_name
from watchlist.training_data import (
    FICTIONAL_DOMAIN,
    TrainingPair,
    TrainingSet,
    _stable_int,
    build_training_pairs,
    fictional_assignments,
    generate_fictional_entities,
    group_aware_batches,
    partition_entities,
    verify_manifest,
)
from watchlist.training_losses import (
    NonFiniteLossError,
    binary_cross_entropy_loss,
    check_finite_gradients,
    multiple_negatives_ranking_loss,
)

CHECKPOINT_FORMAT = "training-checkpoint-v1"
CHAR_NGRAM = "char-ngram-bi-encoder-v1"
PAIR_SCORER = "char-ngram-pair-scorer-v1"
# Verified-local pretrained routes for later approved runs (Steps 14-15); never downloaded here.
E5_BI_ENCODER = "sentence-transformers-e5"
MINILM_CROSS_ENCODER = "cross-encoder-minilm"
IN_REPO = (CHAR_NGRAM, PAIR_SCORER)
OBJECTIVES = {
    "mnrl": (CHAR_NGRAM, E5_BI_ENCODER),
    "bce": (PAIR_SCORER, MINILM_CROSS_ENCODER),
}
# Stop caps may differ between a run and its resumption; the recipe may not.
CAP_FIELDS = ("max_steps", "max_wall_seconds", "max_memory_mb", "checkpoint_every")


class TrainingError(RuntimeError):
    """Explicit refusal for unsafe output paths, stale checkpoints or bad configs."""


class TruncationError(TrainingError):
    """A name would be cut by the tokenizer; silent truncation is never allowed."""


@dataclass(frozen=True)
class ModelConfig:
    """In-repo models use the hashing fields; pretrained routes use ``pretrained`` only.

    ``pretrained`` is the approved embedding/reranker protocol section (as in
    ``eval/configs/embeddings.json`` or ``reranking.json``) whose local files are
    verified against their acquisition receipt before loading.
    """

    architecture: str = CHAR_NGRAM
    buckets: int = 8192
    dimension: int = 64
    ngram_sizes: tuple[int, ...] = (2, 3, 4)
    pretrained: dict | None = None


@dataclass(frozen=True)
class TrainingConfig:
    objective: str = "mnrl"
    seed: int = 42
    batch_size: int = 32
    learning_rate: float = 0.01
    scale: float = 20.0
    use_hard_negatives: bool = True
    threads: int = 2
    max_steps: int = 200
    max_wall_seconds: float = 900.0
    max_memory_mb: int = 4096
    checkpoint_every: int = 50
    model: ModelConfig = field(default_factory=ModelConfig)

    def recipe_digest(self, init_artifact_digest: str | None = None) -> str:
        """Digest of everything except stop caps, plus the starting artifact; resume must match."""
        recipe = {k: v for k, v in asdict(self).items() if k not in CAP_FIELDS}
        return canonical_digest(
            {**recipe, "init_artifact_digest": init_artifact_digest}
        )


@dataclass(frozen=True)
class TrainingResult:
    status: str
    steps: int
    history: tuple[tuple[int, float], ...]
    wall_seconds: float
    peak_rss_bytes: int | None
    checkpoint: str | None
    lineage: dict = field(default_factory=dict)


def _positive_int(value: object, name: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise TrainingError(f"{name} must be an integer >= {minimum}")
    return value


def _positive_float(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < value < float("inf")
    ):
        raise TrainingError(f"{name} must be a positive finite number")
    return float(value)


def _reranking_module():
    """Lazy import of the reranker protocol; explicit refusal if it cannot load here."""
    try:
        reranking = importlib.import_module("watchlist.reranking")
    except ImportError as exc:
        raise TrainingError(
            f"cross-encoder route unavailable: watchlist.reranking failed to import ({exc})"
        ) from exc
    return reranking


def _validate_pretrained(model: ModelConfig) -> None:
    """Pretrained routes need the approved protocol section; in-repo models must not carry one."""
    if model.architecture in IN_REPO:
        if model.pretrained is not None:
            raise TrainingError(
                "in-repo architectures do not take a pretrained section"
            )
        return
    if not isinstance(model.pretrained, Mapping) or not isinstance(
        model.pretrained.get("model_path"), str
    ):
        raise TrainingError(
            f"{model.architecture} needs a pretrained protocol section with model_path"
        )
    try:
        if model.architecture == E5_BI_ENCODER:
            from watchlist.embeddings import validate_config

            validate_config(dict(model.pretrained))
        else:
            _reranking_module().validate_config(dict(model.pretrained))
    except ValueError as exc:
        raise TrainingError(str(exc)) from exc


def load_training_config(raw: Mapping) -> TrainingConfig:
    """Validate the JSON ``training``/``model`` sections into frozen dataclasses."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("model", {}), Mapping):
        raise TrainingError(
            "training config must be an object with an optional model object"
        )
    model_raw = dict(raw.get("model", {}))
    unknown = set(model_raw) - set(ModelConfig.__dataclass_fields__)
    unknown |= set(raw) - set(TrainingConfig.__dataclass_fields__)
    if unknown:
        raise TrainingError(f"unknown training config fields: {sorted(unknown)}")
    model = ModelConfig(
        **{
            **asdict(ModelConfig()),
            **model_raw,
            "ngram_sizes": tuple(model_raw.get("ngram_sizes", ModelConfig.ngram_sizes)),
        }
    )
    if not any(model.architecture in names for names in OBJECTIVES.values()):
        raise TrainingError("unsupported architecture")
    _validate_pretrained(model)
    _positive_int(model.buckets, "buckets", 16)
    _positive_int(model.dimension, "dimension", 2)
    if not model.ngram_sizes or any(
        type(n) is not int or not 1 <= n <= 6 for n in model.ngram_sizes
    ):
        raise TrainingError("ngram_sizes must be integers between 1 and 6")
    config = TrainingConfig(
        **{**{k: v for k, v in raw.items() if k != "model"}, "model": model}
    )
    if (
        config.objective not in OBJECTIVES
        or model.architecture not in OBJECTIVES[config.objective]
    ):
        raise TrainingError(
            "objective must be mnrl (bi-encoder) or bce (pair scorer/cross-encoder)"
        )
    for name in ("seed",):
        if type(getattr(config, name)) is not int:
            raise TrainingError("seed must be an integer")
    _positive_int(config.batch_size, "batch_size", 2)
    _positive_int(config.threads, "threads")
    _positive_int(config.max_steps, "max_steps")
    _positive_int(config.max_memory_mb, "max_memory_mb", 64)
    _positive_int(config.checkpoint_every, "checkpoint_every")
    for name in ("learning_rate", "scale", "max_wall_seconds"):
        _positive_float(getattr(config, name), name)
    if type(config.use_hard_negatives) is not bool:
        raise TrainingError("use_hard_negatives must be a boolean")
    return config


def char_ngrams(text: str, sizes: Sequence[int]) -> list[str]:
    """Boundary-marked character n-grams of the conservatively normalized name."""
    marked = f"<{normalize_name(text)}>"
    return [
        f"{n}:{marked[i : i + n]}"
        for n in sizes
        for i in range(max(len(marked) - n + 1, 1))
    ]


class CharNgramBiEncoder(nn.Module):
    """Hashed character n-gram bag, mean-pooled, projected and L2-normalised.

    ``zlib.crc32`` gives process-independent buckets (Python's ``hash`` is salted).
    """

    def __init__(self, config: ModelConfig):
        super().__init__()
        self.config = config
        self.embedding = nn.EmbeddingBag(config.buckets, config.dimension, mode="mean")
        self.projection = nn.Linear(config.dimension, config.dimension)
        self._cache: dict[str, list[int]] = {}

    def _buckets(self, text: str) -> list[int]:
        if text not in self._cache:
            self._cache[text] = [
                zlib.crc32(gram.encode("utf-8")) % self.config.buckets
                for gram in char_ngrams(text, self.config.ngram_sizes)
            ]
        return self._cache[text]

    def forward(self, texts: Sequence[str]) -> torch.Tensor:
        if not texts:
            raise ValueError("at least one text is required")
        ids = [self._buckets(text) for text in texts]
        offsets = torch.tensor(
            [0, *np.cumsum([len(i) for i in ids[:-1]]).tolist()], dtype=torch.long
        )
        flat = torch.tensor([b for row in ids for b in row], dtype=torch.long)
        return F.normalize(self.projection(self.embedding(flat, offsets)), dim=-1)


class CharNgramPairScorer(nn.Module):
    """Tiny cross-scoring head over a shared encoder; returns one raw logit per pair."""

    def __init__(self, config: ModelConfig):
        super().__init__()
        self.config = config
        self.encoder = CharNgramBiEncoder(config)
        self.head = nn.Linear(2 * config.dimension + 1, 1)

    def forward(
        self, queries: Sequence[str], candidates: Sequence[str]
    ) -> torch.Tensor:
        q, c = self.encoder(queries), self.encoder(candidates)
        features = torch.cat(
            [q * c, (q - c).abs(), (q * c).sum(-1, keepdim=True)], dim=-1
        )
        return self.head(features).squeeze(-1)


def _receipt_lineage(config: Mapping, receipt: Mapping) -> dict:
    """Identify the exact verified pretrained base without copying its bytes."""
    return {
        "model_id": receipt["model_id"],
        "revision": receipt["revision"],
        "model_path": str(config["model_path"]),
        "receipt_files_digest": canonical_digest(receipt["files"]),
    }


class SentenceTransformerBiEncoder(nn.Module):
    """Differentiable wrapper for a *verified local copy* of the approved E5 model.

    Selected by ``architecture: sentence-transformers-e5``; imported lazily and
    not exercised by the fictional pilot. Like ``E5Encoder`` it fixes
    ``max_seq_length`` and refuses any name the tokenizer would truncate.
    Weights are never fetched.
    """

    def __init__(self, embedding_config: dict):
        super().__init__()
        from watchlist.embeddings import verify_model

        receipt = verify_model(embedding_config)
        from sentence_transformers import SentenceTransformer

        self.prefix, self.max_tokens = (
            embedding_config["prefix"],
            embedding_config["max_tokens"],
        )
        self.model = SentenceTransformer(
            str(Path(embedding_config["model_path"]).resolve()),
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
            model_kwargs={"dtype": torch.float32, "use_safetensors": True},
        )
        self.model.max_seq_length = self.max_tokens
        self.pretrained_lineage = _receipt_lineage(embedding_config, receipt)

    def forward(self, texts: Sequence[str]) -> torch.Tensor:
        inputs = [self.prefix + normalize_name(t) for t in texts]
        lengths = self.model.tokenizer(inputs, truncation=False, padding=False)[
            "input_ids"
        ]
        if any(len(ids) > self.max_tokens for ids in lengths):
            raise TruncationError("name would be truncated by tokenizer")
        features = self.model.tokenize(inputs)
        return F.normalize(self.model(features)["sentence_embedding"], dim=-1)


class MiniLMCrossEncoder(nn.Module):
    """Trainable joint-pair scorer over a *verified local copy* of the approved MiniLM reranker.

    Selected by ``architecture: cross-encoder-minilm`` with the ``bce``
    objective; returns one raw logit per pair, refuses truncation and never
    downloads. Not exercised by the fictional pilot.
    """

    def __init__(self, reranker_config: dict):
        super().__init__()
        receipt = _reranking_module().verify_model(reranker_config)
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        path = str(Path(reranker_config["model_path"]).resolve())
        self.max_tokens = reranker_config["max_tokens"]
        self.tokenizer = AutoTokenizer.from_pretrained(
            path, local_files_only=True, trust_remote_code=False
        )
        self.model = AutoModelForSequenceClassification.from_pretrained(
            path,
            local_files_only=True,
            trust_remote_code=False,
            use_safetensors=True,
            dtype=torch.float32,
        )
        if self.model.config.num_labels != 1:
            raise TrainingError("expected a single raw relevance logit")
        self.pretrained_lineage = _receipt_lineage(reranker_config, receipt)

    def forward(
        self, queries: Sequence[str], candidates: Sequence[str]
    ) -> torch.Tensor:
        tokens = self.tokenizer(
            [normalize_name(q) for q in queries],
            [normalize_name(c) for c in candidates],
            truncation=False,
            padding=True,
            return_tensors="pt",
        )
        if tokens["input_ids"].shape[1] > self.max_tokens:
            raise TruncationError(
                "pair exceeds token limit; no silent truncation allowed"
            )
        return self.model(**tokens).logits[:, 0]


def build_model(config: TrainingConfig) -> nn.Module:
    """Seeded construction, so the same config always yields the same initial weights."""
    torch.manual_seed(config.seed)
    architecture = config.model.architecture
    if architecture == E5_BI_ENCODER:
        return SentenceTransformerBiEncoder(dict(config.model.pretrained))
    if architecture == MINILM_CROSS_ENCODER:
        return MiniLMCrossEncoder(dict(config.model.pretrained))
    if architecture == PAIR_SCORER:
        return CharNgramPairScorer(config.model)
    return CharNgramBiEncoder(config.model)


def model_lineage(
    config: TrainingConfig,
    model: nn.Module,
    init_manifest: Mapping | None = None,
    init_path: Path | None = None,
) -> dict:
    """Where the starting weights came from: random init, a pretrained receipt or a derived artifact."""
    lineage = {
        "architecture": config.model.architecture,
        "init_seed": config.seed,
        "pretrained_weights": getattr(model, "pretrained_lineage", None),
    }
    if init_manifest is not None:
        parent = init_manifest["derived_from"]
        return {
            **lineage,
            "base": "derived-artifact",
            "pretrained_weights": parent.get("pretrained_weights"),
            "init_artifact": {
                "manifest_digest": init_manifest["manifest_digest"],
                "path": str(init_path),
                "derived_from": parent,
            },
        }
    return {
        **lineage,
        "base": "pretrained" if lineage["pretrained_weights"] else "random-init",
    }


@contextmanager
def torch_runtime(threads: int):
    """Deterministic CPU settings for the run; previous global settings are restored."""
    previous = torch.get_num_threads(), torch.are_deterministic_algorithms_enabled()
    torch.set_num_threads(threads)
    torch.use_deterministic_algorithms(True)
    try:
        yield
    finally:
        torch.set_num_threads(previous[0])
        torch.use_deterministic_algorithms(previous[1])


def current_rss_bytes() -> int | None:
    """Resident memory on Windows (psapi) or Linux (/proc); ``None`` if unmeasurable."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                *(
                    (name, ctypes.c_size_t)
                    for name in (
                        "PeakWorkingSetSize",
                        "WorkingSetSize",
                        "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage",
                        "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage",
                        "PagefileUsage",
                        "PeakPagefileUsage",
                    )
                ),
            ]

        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(Counters),
                wintypes.DWORD,
            ]
            if psapi.GetProcessMemoryInfo(
                kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            ):
                return int(counters.WorkingSetSize)
        except (OSError, AttributeError):
            return None
        return None
    statm = Path("/proc/self/statm")
    if statm.exists() and hasattr(os, "sysconf"):
        return int(statm.read_text(encoding="utf-8").split()[1]) * os.sysconf(
            "SC_PAGE_SIZE"
        )
    return None


def guard_output_dir(output_dir: Path, protected: Sequence[Path] = ()) -> Path:
    """Refuse pretrained receipts, sealed artifacts and configured protected roots."""
    resolved = Path(output_dir).resolve()
    for root in protected:
        root = Path(root).resolve()
        if resolved == root or root in resolved.parents:
            raise TrainingError(f"refusing to write inside protected weights: {root}")
    if is_protected_directory(resolved):
        raise TrainingError(
            "refusing to write inside original pretrained or frozen artifact weights"
        )
    return resolved


def preflight(config: TrainingConfig, training_set: TrainingSet) -> dict:
    """Estimate parameter/optimizer memory against the cap before any step runs."""
    with torch_runtime(config.threads):
        parameters = sum(p.numel() for p in build_model(config).parameters())
    # float32 weights + gradients + two Adam moments; activations are tiny here.
    estimated = parameters * 4 * 4
    rss = current_rss_bytes()
    cap = config.max_memory_mb * 1024 * 1024
    batches = len(
        group_aware_batches(
            training_set.pairs, config.batch_size, seed=config.seed, epoch=0
        )
    )
    return {
        "parameters": parameters,
        "estimated_training_state_bytes": estimated,
        "process_rss_bytes": rss,
        "memory_cap_bytes": cap,
        "within_cap": estimated + (rss or 0) < cap,
        "rss_measurable": rss is not None,
        "pairs": len(training_set.pairs),
        "positives": len(training_set.positives),
        "contrastive_batches_per_epoch": batches,
        "recipe_digest": config.recipe_digest(),
        "data_manifest_digest": training_set.manifest["digest"],
    }


class _BatchSchedule:
    """Deterministic batch for any step index, so a resumed run sees identical data."""

    def __init__(self, config: TrainingConfig, training_set: TrainingSet):
        self.config, self.pairs, self._epochs = config, training_set.pairs, {}
        self.hard: dict[str, list[TrainingPair]] = {}
        for pair in training_set.pairs:
            if pair.kind == "hard_negative":
                self.hard.setdefault(pair.query_alias_id, []).append(pair)

    def _epoch(self, epoch: int) -> list[tuple[TrainingPair, ...]]:
        if epoch not in self._epochs:
            if self.config.objective == "mnrl":
                batches = group_aware_batches(
                    self.pairs,
                    self.config.batch_size,
                    seed=self.config.seed,
                    epoch=epoch,
                )
            else:
                order = sorted(
                    self.pairs,
                    key=lambda p: (
                        _stable_int(self.config.seed, epoch, p.pair_id),
                        p.pair_id,
                    ),
                )
                size = self.config.batch_size
                batches = [
                    tuple(order[i : i + size]) for i in range(0, len(order), size)
                ]
            if not batches:
                raise TrainingError("training data yields no valid batch")
            self._epochs[epoch] = batches
        return self._epochs[epoch]

    def batch(self, step: int) -> tuple[TrainingPair, ...]:
        epoch = 0
        while step >= len(self._epoch(epoch)):
            step -= len(self._epoch(epoch))
            epoch += 1
        return self._epoch(epoch)[step]


def batch_loss(
    model: nn.Module,
    config: TrainingConfig,
    batch: Sequence[TrainingPair],
    hard: Mapping[str, list[TrainingPair]],
) -> torch.Tensor:
    """Objective-specific loss for one batch; raises ``NonFiniteLossError``."""
    if config.objective == "bce":
        logits = model([p.query_text for p in batch], [p.candidate_text for p in batch])
        return binary_cross_entropy_loss(
            logits, torch.tensor([float(p.label) for p in batch])
        )
    groups = [p.query_group_id for p in batch]
    extra = (
        [n for p in batch for n in hard.get(p.query_alias_id, [])]
        if config.use_hard_negatives
        else []
    )
    return multiple_negatives_ranking_loss(
        model([p.query_text for p in batch]),
        model([p.candidate_text for p in batch]),
        scale=config.scale,
        group_ids=groups,
        hard_negatives=model([n.candidate_text for n in extra]) if extra else None,
        hard_negative_group_ids=[n.candidate_group_id for n in extra]
        if extra
        else None,
    )


def _checkpoint_paths(output_dir: Path) -> tuple[Path, Path]:
    return output_dir / "checkpoints", output_dir / "checkpoints" / "latest.json"


def save_checkpoint(
    output_dir: Path,
    *,
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    step: int,
    history: list,
    elapsed: float,
    config: TrainingConfig,
    data_digest: str,
    init_digest: str | None = None,
) -> Path:
    """Write a new step file, then atomically repoint ``latest.json`` to it.

    A crash before the pointer update leaves the previous complete checkpoint
    authoritative; superseded step files are removed only afterwards.
    """
    folder, pointer = _checkpoint_paths(output_dir)
    buffer = io.BytesIO()
    torch.save(
        {
            "format": CHECKPOINT_FORMAT,
            "step": step,
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "torch_rng": torch.get_rng_state(),
            "history": [[s, v] for s, v in history],
            "elapsed_seconds": elapsed,
            "recipe_digest": config.recipe_digest(init_digest),
            "data_digest": data_digest,
        },
        buffer,
    )
    path = folder / f"step-{step:06d}.pt"
    atomic_write_bytes(path, buffer.getvalue())
    atomic_write_json(
        pointer,
        {
            "format": CHECKPOINT_FORMAT,
            "file": path.name,
            "step": step,
            "sha256": file_sha256(path),
            "recipe_digest": config.recipe_digest(init_digest),
            "data_digest": data_digest,
        },
    )
    for stale in folder.glob("step-*.pt"):
        if stale.name != path.name:
            stale.unlink(missing_ok=True)
    return path


def load_checkpoint(
    output_dir: Path,
    config: TrainingConfig,
    data_digest: str,
    init_digest: str | None = None,
) -> dict:
    """Verify pointer, hash, recipe (including the starting artifact) and data, then load with ``weights_only``."""
    folder, pointer = _checkpoint_paths(output_dir)
    if not pointer.is_file():
        raise TrainingError("no checkpoint to resume")
    try:
        info = json.loads(pointer.read_text(encoding="utf-8"))
        path = folder / info["file"]
        if (
            Path(info["file"]).name != info["file"]
            or file_sha256(path) != info["sha256"]
        ):
            raise TrainingError("checkpoint file is corrupt or partial")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TrainingError("checkpoint pointer is corrupt or partial") from exc
    if (
        info["recipe_digest"] != config.recipe_digest(init_digest)
        or info["data_digest"] != data_digest
    ):
        raise TrainingError(
            "checkpoint was produced by a different recipe, starting artifact or training data"
        )
    state = torch.load(io.BytesIO(path.read_bytes()), weights_only=True)
    if state.get("format") != CHECKPOINT_FORMAT or state.get("step") != info["step"]:
        raise TrainingError("checkpoint contents disagree with its pointer")
    return state


def train(
    config: TrainingConfig,
    training_set: TrainingSet,
    output_dir: Path,
    *,
    resume: bool = False,
    protected: Sequence[Path] = (),
    init_artifact: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    memory_probe: Callable[[], int | None] = current_rss_bytes,
) -> tuple[nn.Module, TrainingResult]:
    """Run Adam on CPU until ``max_steps``, the wall-clock cap, the memory cap or a non-finite value.

    Stop statuses are explicit: ``completed_max_steps``, ``stopped_deadline``,
    ``stopped_memory_cap`` or ``stopped_nonfinite``. A non-finite loss or
    gradient never reaches the optimizer, so the saved state stays finite.
    ``init_artifact`` continues from a verified derived artifact; its manifest
    digest is part of the resume recipe and of the returned lineage.
    """
    data_digest = verify_manifest(training_set.manifest)
    if config.model.pretrained is not None:
        protected = (*protected, Path(config.model.pretrained["model_path"]))
    output_dir = guard_output_dir(output_dir, protected)
    init_manifest = (
        verify_artifact(init_artifact) if init_artifact is not None else None
    )
    init_digest = (
        init_manifest["manifest_digest"] if init_manifest is not None else None
    )
    if not resume and _checkpoint_paths(output_dir)[1].exists():
        raise TrainingError(
            "checkpoint already exists; pass resume=True or choose a new directory"
        )
    start = clock()
    with torch_runtime(config.threads):
        model = build_model(config)
        lineage = model_lineage(config, model, init_manifest, init_artifact)
        if init_artifact is not None:
            model.load_state_dict(
                load_artifact_state(init_artifact, config, expected_digest=init_digest)
            )
        optimizer = torch.optim.Adam(model.parameters(), lr=config.learning_rate)
        step, history, elapsed_before = 0, [], 0.0
        if resume:
            state = load_checkpoint(output_dir, config, data_digest, init_digest)
            model.load_state_dict(state["model"])
            optimizer.load_state_dict(state["optimizer"])
            torch.set_rng_state(state["torch_rng"])
            step, elapsed_before = state["step"], float(state["elapsed_seconds"])
            history = [(int(s), float(v)) for s, v in state["history"]]
        schedule = _BatchSchedule(config, training_set)
        status, peak = "completed_max_steps", None
        model.train()
        while step < config.max_steps:
            if elapsed_before + clock() - start >= config.max_wall_seconds:
                status = "stopped_deadline"
                break
            rss = memory_probe()
            peak = rss if peak is None or (rss is not None and rss > peak) else peak
            if rss is not None and rss > config.max_memory_mb * 1024 * 1024:
                status = "stopped_memory_cap"
                break
            optimizer.zero_grad(set_to_none=True)
            try:
                loss = batch_loss(model, config, schedule.batch(step), schedule.hard)
                loss.backward()
                check_finite_gradients(model.parameters())
            except NonFiniteLossError:
                status = "stopped_nonfinite"
                break
            optimizer.step()
            step += 1
            history.append((step, float(loss.detach())))
            if step % config.checkpoint_every == 0 and step < config.max_steps:
                save_checkpoint(
                    output_dir,
                    model=model,
                    optimizer=optimizer,
                    step=step,
                    history=history,
                    elapsed=elapsed_before + clock() - start,
                    config=config,
                    data_digest=data_digest,
                    init_digest=init_digest,
                )
        elapsed = elapsed_before + clock() - start
        path = save_checkpoint(
            output_dir,
            model=model,
            optimizer=optimizer,
            step=step,
            history=history,
            elapsed=elapsed,
            config=config,
            data_digest=data_digest,
            init_digest=init_digest,
        )
    model.eval()
    model.lineage = lineage
    return model, TrainingResult(
        status, step, tuple(history), elapsed, peak, str(path), lineage
    )


def export_artifact(
    model: nn.Module,
    config: TrainingConfig,
    training_set: TrainingSet,
    directory: Path,
    metrics: Mapping,
    *,
    protected: Sequence[Path] = (),
) -> dict:
    """Save weights and seal them in a derived manifest; existing artifacts are refused.

    Lineage comes from ``train`` (stored on the model), so an artifact continued
    from another artifact or a pretrained base records that origin.
    """
    directory = guard_output_dir(directory, protected)
    lineage = getattr(model, "lineage", None) or model_lineage(config, model)
    if directory.exists() and any(directory.iterdir()):
        raise ArtifactError("artifact directory is not empty; refusing to overwrite")
    buffer = io.BytesIO()
    torch.save(model.state_dict(), buffer)
    atomic_write_bytes(directory / "weights.pt", buffer.getvalue())
    return write_artifact(
        directory,
        payload_files=["weights.pt"],
        config=asdict(config),
        data_manifest_digest=training_set.manifest["digest"],
        metrics=metrics,
        derived_from=lineage,
        packages={"torch": torch.__version__, "numpy": np.__version__},
    )


def load_artifact_state(
    directory: Path,
    config: TrainingConfig | None = None,
    *,
    expected_digest: str | None = None,
) -> dict:
    """Verify an artifact's hashes, check architecture compatibility, then load tensors.

    Only a ``weights.pt`` listed (and therefore hashed) in the manifest is loaded.
    """
    manifest = verify_artifact(directory)
    if expected_digest is not None and manifest["manifest_digest"] != expected_digest:
        raise ArtifactError("artifact changed after it was verified")
    if "weights.pt" not in manifest["files"]:
        raise ArtifactError(
            "artifact manifest does not list weights.pt; refusing unhashed weights"
        )
    if config is not None and manifest["config"]["model"] != json.loads(
        json.dumps(asdict(config.model))
    ):
        raise ArtifactError(
            "artifact architecture differs from the requested model config"
        )
    return torch.load(
        io.BytesIO((Path(directory) / "weights.pt").read_bytes()), weights_only=True
    )


def load_artifact_model(directory: Path) -> nn.Module:
    """Rebuild a verified derived model in evaluation mode."""
    manifest = verify_artifact(directory)
    config = load_training_config(manifest["config"])
    with torch_runtime(config.threads):
        model = build_model(config)
        model.load_state_dict(load_artifact_state(directory, config))
    model.eval()
    return model


def _encode(model: nn.Module, texts: list[str]) -> np.ndarray:
    if isinstance(model, MiniLMCrossEncoder):
        raise TrainingError(
            "a cross-encoder scores pairs; it has no single-name embedding"
        )
    encoder = model.encoder if isinstance(model, CharNgramPairScorer) else model
    with torch.no_grad():
        return encoder(texts).numpy().astype(np.float32)


def heldout_view(
    entities: Sequence[Entity],
) -> tuple[tuple[Entity, ...], dict[str, str]]:
    """Withhold each multi-alias entity's last alias (by ID) as its query text."""
    view, queries = [], {}
    for entity in sorted(entities, key=lambda e: e.entity_id):
        aliases = sorted(entity.aliases, key=lambda a: a.alias_id)
        if len(aliases) > 1:
            queries[entity.entity_id] = aliases[-1].name
            aliases = aliases[:-1]
        view.append(
            Entity(
                entity.entity_id,
                entity.name,
                entity.entity_type,
                tuple(aliases),
                entity.synthetic,
                entity.source,
                entity.provenance,
            )
        )
    return tuple(view), queries


def retrieval_metrics(
    model: nn.Module | None, entities: Sequence[Entity], *, top_k: int = 5
) -> dict:
    """Held-out-alias Recall@1/@k and MRR@k; ``model=None`` uses RapidFuzz."""
    view, queries = heldout_view(entities)
    if model is not None:
        vectors = _encode(model, [a.name for e in view for a in e.aliases])
    hits1 = hitsk = 0
    reciprocal = 0.0
    for entity_id, text in sorted(queries.items()):
        if model is None:
            ranked = rank_candidates(text, view, top_k)
        else:
            ranked = rank_vectors(_encode(model, [text]), vectors, view, top_k)
        ids = [c.entity_id for c in ranked]
        if entity_id in ids:
            rank = ids.index(entity_id) + 1
            hits1 += rank == 1
            hitsk += 1
            reciprocal += 1 / rank
    n = len(queries)
    return {
        "queries": n,
        "index_entities": len(view),
        "recall_at_1": hits1 / n if n else None,
        f"recall_at_{top_k}": hitsk / n if n else None,
        f"mrr_at_{top_k}": reciprocal / n if n else None,
        "hits_at_1": hits1,
        f"hits_at_{top_k}": hitsk,
    }


def selected_record_rows(
    model: nn.Module,
    entities: Sequence[Entity],
    assignments: Mapping,
    partition: str,
    *,
    seed: int = 42,
) -> list[dict]:
    """Top-1 selected-record score and label per query for calibration research.

    Present queries search the held-out view containing the target; for half of
    the entities (hash-selected) an extra absent query searches the same view
    with the target removed. ``label`` is 1 only when the selected record is the
    query's target. The score is a raw cosine, not a probability.
    """
    members = partition_entities(entities, assignments, partition)
    view, queries = heldout_view(members)
    vectors = _encode(model, [a.name for e in view for a in e.aliases])
    offsets, start = {}, 0
    for entity in view:
        offsets[entity.entity_id] = (start, start + len(entity.aliases))
        start += len(entity.aliases)
    rows = []
    for entity_id, text in sorted(queries.items()):
        query = _encode(model, [text])
        cases = [("present", view, vectors)]
        if _stable_int(seed, "absent", entity_id) % 2 == 0:
            low, high = offsets[entity_id]
            cases.append(
                (
                    "absent",
                    tuple(e for e in view if e.entity_id != entity_id),
                    np.concatenate([vectors[:low], vectors[high:]]),
                )
            )
        for presence, index, matrix in cases:
            top = rank_vectors(query, matrix, index, 1)[0]
            rows.append(
                {
                    "query_id": f"{entity_id}:{presence}",
                    "group_id": assignments[entity_id]["group_id"],
                    "partition": partition,
                    "domain": FICTIONAL_DOMAIN,
                    "presence": presence,
                    "query_entity_id": entity_id,
                    "selected_entity_id": top.entity_id,
                    "score": round(float(top.similarity_score), 8),
                    "label": int(presence == "present" and top.entity_id == entity_id),
                    "score_kind": "raw_cosine_char_ngram_v1",
                }
            )
    return rows


def _probe_loss(
    model: nn.Module,
    config: TrainingConfig,
    training_set: TrainingSet,
    batches: int = 8,
) -> float:
    """Mean loss over the first epoch-0 batches, without gradient updates."""
    schedule = _BatchSchedule(config, training_set)
    with torch.no_grad():
        values = [
            float(batch_loss(model, config, schedule.batch(i), schedule.hard))
            for i in range(min(batches, len(schedule._epoch(0))))
        ]
    return sum(values) / len(values)


def _pilot_inputs(
    raw: Mapping,
) -> tuple[TrainingConfig, tuple[Entity, ...], dict, TrainingSet]:
    data = raw["data"]
    config = load_training_config(raw["training"])
    entities = generate_fictional_entities(data["entity_count"], data["generator_seed"])
    assignments = fictional_assignments(entities, data["split_seed"])
    training_set = build_training_pairs(
        entities,
        assignments,
        seed=data["split_seed"],
        max_positives_per_group=data["max_positives_per_group"],
        hard_negatives_per_positive=data["hard_negatives_per_positive"],
        easy_negatives_per_positive=data["easy_negatives_per_positive"],
    )
    return config, entities, assignments, training_set


def run_pilot(raw: Mapping, root: Path = Path(".")) -> dict:
    """Bounded fictional pilot: before/after loss and dev recall, artifact, calibration rows."""
    config, entities, assignments, training_set = _pilot_inputs(raw)
    protected = [root / p for p in raw.get("protected_paths", [])]
    output = root / raw["output_dir"]
    check = preflight(config, training_set)
    if not check["within_cap"]:
        raise TrainingError("preflight memory estimate exceeds the configured cap")
    development = partition_entities(entities, assignments, "development")
    with torch_runtime(config.threads):
        initial = build_model(config).eval()
        before = {
            "probe_loss": _probe_loss(initial, config, training_set),
            "development": retrieval_metrics(initial, development),
        }
    model, result = train(config, training_set, output / "run", protected=protected)
    with torch_runtime(config.threads):
        after = {
            "probe_loss": _probe_loss(model, config, training_set),
            "development": retrieval_metrics(model, development),
        }
        rows = [
            row
            for partition in ("calibration_fit", "threshold_selection")
            for row in selected_record_rows(
                model, entities, assignments, partition, seed=raw["data"]["split_seed"]
            )
        ]
    # A cap can stop the run before its first step; record that instead of indexing an empty history.
    metrics = {
        "status": result.status,
        "steps": result.steps,
        "wall_seconds": round(result.wall_seconds, 3),
        "peak_rss_bytes": result.peak_rss_bytes,
        "first_step_loss": result.history[0][1] if result.history else None,
        "last_step_loss": result.history[-1][1] if result.history else None,
        "before": before,
        "after": after,
        "rapidfuzz_development": retrieval_metrics(None, development),
        "evidence": "fictional generated names; mechanics only, not a performance estimate",
    }
    manifest = export_artifact(
        model, config, training_set, output / "artifact", metrics, protected=protected
    )
    rows_path = root / raw["selected_records_path"]
    atomic_write_json(
        rows_path,
        {
            "version": "selected-records-v1",
            "artifact_manifest_digest": manifest["manifest_digest"],
            "evidence": metrics["evidence"],
            "rows": rows,
        },
    )
    return {
        "preflight": check,
        "metrics": metrics,
        "artifact": str(output / "artifact"),
        "artifact_manifest_digest": manifest["manifest_digest"],
        "selected_records": str(rows_path),
        "selected_record_count": len(rows),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preflight", "pilot"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = json.loads(args.config.read_text(encoding="utf-8"))
        if args.action == "preflight":
            config, _, _, training_set = _pilot_inputs(raw)
            report = preflight(config, training_set)
        else:
            report = run_pilot(raw)
    except (OSError, ValueError, KeyError, TrainingError, ArtifactError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
