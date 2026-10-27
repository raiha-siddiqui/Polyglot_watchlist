"""Training mechanics on tiny fictional data: updates, caps, resume and safe writes."""

import json
from pathlib import Path
import sys
import types

import pytest

torch = pytest.importorskip("torch")

from watchlist import training  # noqa: E402
from watchlist.model_artifacts import (
    ArtifactError,
    canonical_digest,
    file_sha256,  # noqa: E402
    verify_artifact,
)
from watchlist.training import (
    TrainingError,
    build_model,
    current_rss_bytes,  # noqa: E402
    export_artifact,
    load_artifact_model,
    load_artifact_state,
    load_checkpoint,
    load_training_config,
    preflight,
    retrieval_metrics,
    save_checkpoint,
    selected_record_rows,
    train,
)
from watchlist.training_data import (
    build_training_pairs,
    fictional_assignments,  # noqa: E402
    generate_fictional_entities,
    partition_entities,
)
from watchlist.training_losses import NonFiniteLossError  # noqa: E402

TINY = {
    "batch_size": 8,
    "learning_rate": 0.02,
    "threads": 1,
    "max_steps": 12,
    "checkpoint_every": 4,
    "model": {"buckets": 512, "dimension": 16, "ngram_sizes": [2, 3]},
}


def config(**overrides):
    return load_training_config({**TINY, **overrides})


@pytest.fixture(scope="module")
def corpus():
    entities = generate_fictional_entities(80, seed=5)
    assignments = fictional_assignments(entities, 42)
    return entities, assignments, build_training_pairs(entities, assignments, seed=42)


def state_equal(left: dict, right: dict) -> bool:
    return left.keys() == right.keys() and all(
        torch.equal(left[k], right[k]) for k in left
    )


def test_weights_change_and_loss_decreases(corpus, tmp_path: Path) -> None:
    _, _, data = corpus
    initial = build_model(config()).state_dict()
    model, result = train(config(max_steps=30), data, tmp_path / "run")
    assert result.status == "completed_max_steps" and result.steps == 30
    assert not state_equal(initial, model.state_dict())
    losses = [loss for _, loss in result.history]
    assert all(torch.isfinite(torch.tensor(losses)))
    assert sum(losses[-5:]) / 5 < sum(losses[:5]) / 5


def test_seed_controls_initialisation() -> None:
    assert state_equal(
        build_model(config()).state_dict(), build_model(config()).state_dict()
    )
    assert not state_equal(
        build_model(config()).state_dict(), build_model(config(seed=7)).state_dict()
    )


def test_resume_reproduces_an_uninterrupted_run_bitwise(corpus, tmp_path: Path) -> None:
    _, _, data = corpus
    straight, full = train(config(), data, tmp_path / "straight")
    _, half = train(config(max_steps=6), data, tmp_path / "resumed")
    assert half.steps == 6
    resumed, rest = train(config(), data, tmp_path / "resumed", resume=True)
    assert rest.steps == 12 and rest.history == full.history
    assert state_equal(straight.state_dict(), resumed.state_dict())


def test_resume_refuses_changed_recipe_data_corruption_and_silent_restart(
    corpus, tmp_path: Path
) -> None:
    entities, assignments, data = corpus
    train(config(max_steps=4), data, tmp_path / "run")
    with pytest.raises(TrainingError, match="already exists"):
        train(config(max_steps=4), data, tmp_path / "run")
    with pytest.raises(TrainingError, match="different recipe"):
        train(config(learning_rate=0.5), data, tmp_path / "run", resume=True)
    other = build_training_pairs(entities, assignments, seed=9)
    with pytest.raises(
        TrainingError, match="different recipe, starting artifact or training data"
    ):
        train(config(), other, tmp_path / "run", resume=True)
    checkpoint = next((tmp_path / "run" / "checkpoints").glob("step-*.pt"))
    checkpoint.write_bytes(checkpoint.read_bytes()[:-10] + b"corrupted!")
    with pytest.raises(TrainingError, match="corrupt"):
        train(config(), data, tmp_path / "run", resume=True)
    with pytest.raises(TrainingError, match="no checkpoint"):
        train(config(), data, tmp_path / "never", resume=True)


def test_interrupted_checkpoint_write_keeps_previous_checkpoint(
    corpus, tmp_path: Path, monkeypatch
) -> None:
    _, _, data = corpus
    model, _ = train(config(max_steps=4), data, tmp_path / "run")
    optimizer = torch.optim.Adam(model.parameters())

    def crash(*_args, **_kwargs):
        raise OSError("simulated crash before pointer update")

    monkeypatch.setattr(training, "atomic_write_json", crash)
    with pytest.raises(OSError, match="simulated"):
        save_checkpoint(
            tmp_path / "run",
            model=model,
            optimizer=optimizer,
            step=99,
            history=[],
            elapsed=0.0,
            config=config(),
            data_digest=data.manifest["digest"],
        )
    monkeypatch.undo()
    assert (
        load_checkpoint(tmp_path / "run", config(), data.manifest["digest"])["step"]
        == 4
    )


def test_deadline_and_memory_caps_stop_explicitly(corpus, tmp_path: Path) -> None:
    _, _, data = corpus
    ticks = iter(range(0, 10_000, 10))
    _, result = train(
        config(max_wall_seconds=25.0),
        data,
        tmp_path / "deadline",
        clock=lambda: float(next(ticks)),
    )
    assert result.status == "stopped_deadline" and 0 < result.steps < 12
    _, result = train(config(), data, tmp_path / "memory", memory_probe=lambda: 10**12)
    assert result.status == "stopped_memory_cap" and result.steps == 0


def test_non_finite_loss_stops_before_any_bad_update(
    corpus, tmp_path: Path, monkeypatch
) -> None:
    _, _, data = corpus
    real = training.batch_loss
    calls = []

    def flaky(*args):
        calls.append(1)
        if len(calls) == 3:
            raise NonFiniteLossError("simulated NaN loss")
        return real(*args)

    monkeypatch.setattr(training, "batch_loss", flaky)
    model, result = train(config(), data, tmp_path / "run")
    assert result.status == "stopped_nonfinite" and result.steps == 2
    assert all(torch.isfinite(p).all() for p in model.parameters())


def test_refuses_to_write_into_original_or_frozen_weights(
    corpus, tmp_path: Path
) -> None:
    _, _, data = corpus
    original = tmp_path / "e5-small"
    original.mkdir()
    (original / "acquisition.json").write_text("{}", encoding="utf-8")
    with pytest.raises(TrainingError, match="pretrained"):
        train(config(max_steps=1), data, original / "run")
    with pytest.raises(TrainingError, match="protected"):
        train(
            config(max_steps=1),
            data,
            tmp_path / "models" / "x",
            protected=[tmp_path / "models"],
        )
    model, _ = train(config(max_steps=2), data, tmp_path / "run")
    export_artifact(model, config(), data, tmp_path / "artifact", {"loss": 1.0})
    with pytest.raises((ArtifactError, TrainingError)):
        export_artifact(model, config(), data, tmp_path / "artifact", {"loss": 1.0})
    with pytest.raises(TrainingError):
        train(config(max_steps=1), data, tmp_path / "artifact" / "run")


def test_artifact_reload_is_equivalent_and_init_source_stays_unchanged(
    corpus, tmp_path: Path
) -> None:
    _, _, data = corpus
    model, _ = train(config(max_steps=3), data, tmp_path / "run")
    manifest = export_artifact(
        model, config(), data, tmp_path / "artifact", {"loss": 1.0}
    )
    assert manifest["derived_from"]["pretrained_weights"] is None
    before = file_sha256(tmp_path / "artifact" / "weights.pt")
    reloaded = load_artifact_model(tmp_path / "artifact")
    texts = ["Fictional Name", "Фиктивное имя"]
    with torch.no_grad():
        assert torch.equal(model(texts), reloaded(texts))
    continued, result = train(
        config(max_steps=2),
        data,
        tmp_path / "continued",
        init_artifact=tmp_path / "artifact",
    )
    assert result.steps == 2 and not state_equal(
        continued.state_dict(), model.state_dict()
    )
    assert file_sha256(tmp_path / "artifact" / "weights.pt") == before
    verify_artifact(tmp_path / "artifact")


def test_bce_objective_trains_the_pair_scorer(corpus, tmp_path: Path) -> None:
    _, _, data = corpus
    bce = config(
        objective="bce",
        model={**TINY["model"], "architecture": "char-ngram-pair-scorer-v1"},
    )
    initial = build_model(bce).state_dict()
    model, result = train(bce, data, tmp_path / "run")
    assert result.status == "completed_max_steps"
    assert not state_equal(initial, model.state_dict())


@pytest.mark.parametrize(
    "raw",
    [
        {**TINY, "unknown": 1},
        {**TINY, "objective": "bce"},
        {**TINY, "max_steps": 0},
        {**TINY, "learning_rate": float("nan")},
        {**TINY, "model": {"ngram_sizes": [9]}},
        {**TINY, "seed": "42"},
    ],
)
def test_invalid_training_configs_are_refused(raw: dict) -> None:
    with pytest.raises(TrainingError):
        load_training_config(raw)


def test_preflight_measurement_and_memory_probe(corpus) -> None:
    _, _, data = corpus
    report = preflight(config(), data)
    assert report["parameters"] == 512 * 16 + 16 * 16 + 16
    assert report["data_manifest_digest"] == data.manifest["digest"]
    if sys.platform == "win32" or Path("/proc/self/statm").exists():
        assert isinstance(current_rss_bytes(), int) and current_rss_bytes() > 0


def test_selected_record_rows_and_retrieval_metrics(corpus) -> None:
    entities, assignments, _ = corpus
    model = build_model(config()).eval()
    rows = selected_record_rows(model, entities, assignments, "development")
    assert rows and {row["partition"] for row in rows} == {"development"}
    assert all(row["label"] == 0 for row in rows if row["presence"] == "absent")
    assert all(
        row["selected_entity_id"] != row["query_entity_id"]
        for row in rows
        if row["presence"] == "absent"
    )
    assert all(
        row["label"] == int(row["selected_entity_id"] == row["query_entity_id"])
        for row in rows
        if row["presence"] == "present"
    )
    development = partition_entities(entities, assignments, "development")
    metrics = retrieval_metrics(model, development)
    assert metrics["queries"] == len(development) and 0 <= metrics["recall_at_1"] <= 1
    assert json.loads(json.dumps(metrics)) == metrics


def test_sentence_transformer_path_verifies_local_weights_before_import(
    tmp_path: Path, monkeypatch
) -> None:
    from watchlist.embeddings import MODEL_ID, REVISION

    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    embedding_config = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "device": "cpu",
        "dtype": "float32",
        "dimension": 384,
        "prefix": "query: ",
        "max_tokens": 512,
        "normalization": "phase1-nfkc-casefold-whitespace-v1",
        "pooling": "mean",
        "batch_size": 8,
        "model_path": str(tmp_path / "absent"),
    }
    with pytest.raises(OSError):
        training.SentenceTransformerBiEncoder(embedding_config)


def reseal_manifest(directory: Path, **changes) -> None:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    body = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    body.update(changes)
    (directory / "manifest.json").write_text(
        json.dumps({**body, "manifest_digest": canonical_digest(body)}),
        encoding="utf-8",
    )


def test_continued_training_records_and_enforces_artifact_lineage(
    corpus, tmp_path: Path
) -> None:
    _, _, data = corpus
    model, _ = train(config(max_steps=2), data, tmp_path / "run")
    first = export_artifact(model, config(), data, tmp_path / "a1", {"loss": 1.0})
    assert first["derived_from"]["base"] == "random-init"
    continued, result = train(
        config(max_steps=4), data, tmp_path / "continued", init_artifact=tmp_path / "a1"
    )
    assert (
        result.lineage["init_artifact"]["manifest_digest"] == first["manifest_digest"]
    )
    second = export_artifact(
        continued, config(max_steps=4), data, tmp_path / "a2", {"loss": 0.5}
    )
    lineage = second["derived_from"]
    assert (
        lineage["base"] == "derived-artifact" and lineage["pretrained_weights"] is None
    )
    assert lineage["init_artifact"]["manifest_digest"] == first["manifest_digest"]
    assert lineage["init_artifact"]["derived_from"] == first["derived_from"]
    # Resuming must use the same starting artifact (or none) as the interrupted run.
    with pytest.raises(TrainingError, match="starting artifact"):
        train(config(max_steps=6), data, tmp_path / "continued", resume=True)
    with pytest.raises(TrainingError, match="starting artifact"):
        train(
            config(max_steps=6),
            data,
            tmp_path / "continued",
            resume=True,
            init_artifact=tmp_path / "a2",
        )
    _, resumed = train(
        config(max_steps=6),
        data,
        tmp_path / "continued",
        resume=True,
        init_artifact=tmp_path / "a1",
    )
    assert resumed.steps == 6 and resumed.lineage == result.lineage
    assert config().recipe_digest() != config().recipe_digest(first["manifest_digest"])


def test_weights_not_listed_in_the_manifest_are_never_loaded(
    corpus, tmp_path: Path
) -> None:
    _, _, data = corpus
    model, _ = train(config(max_steps=1), data, tmp_path / "run")
    manifest = export_artifact(
        model, config(), data, tmp_path / "artifact", {"loss": 1.0}
    )
    (tmp_path / "artifact" / "notes.txt").write_text("fictional", encoding="utf-8")
    reseal_manifest(
        tmp_path / "artifact",
        files={
            "notes.txt": {
                "bytes": (tmp_path / "artifact" / "notes.txt").stat().st_size,
                "sha256": file_sha256(tmp_path / "artifact" / "notes.txt"),
            }
        },
    )
    with pytest.raises(ArtifactError, match="weights.pt"):
        load_artifact_state(tmp_path / "artifact", config())
    reseal_manifest(tmp_path / "artifact", files={})
    with pytest.raises(ArtifactError, match="no payload"):
        load_artifact_model(tmp_path / "artifact")
    assert manifest["files"]["weights.pt"]["sha256"] == file_sha256(
        tmp_path / "artifact" / "weights.pt"
    )


def pilot_raw(**training_overrides) -> dict:
    return {
        "data": {
            "generator": "fictional-syllable-generator-v1",
            "entity_count": 80,
            "generator_seed": 5,
            "split_seed": 42,
            "max_positives_per_group": 4,
            "hard_negatives_per_positive": 1,
            "easy_negatives_per_positive": 1,
        },
        "training": {**TINY, **training_overrides},
        "output_dir": "models/pilot",
        "selected_records_path": "rows/selected-records.json",
        "protected_paths": ["models/original"],
    }


def test_pilot_records_a_run_stopped_before_its_first_step(tmp_path: Path) -> None:
    report = training.run_pilot(pilot_raw(max_wall_seconds=1e-9), root=tmp_path)
    metrics = report["metrics"]
    assert metrics["status"] == "stopped_deadline" and metrics["steps"] == 0
    assert metrics["first_step_loss"] is None and metrics["last_step_loss"] is None
    assert (
        verify_artifact(tmp_path / "models" / "pilot" / "artifact")["metrics"]["steps"]
        == 0
    )
    complete = training.run_pilot(pilot_raw(max_steps=3), root=tmp_path / "second")[
        "metrics"
    ]
    assert complete["steps"] == 3 and complete["first_step_loss"] is not None


# --- Pretrained routes, exercised with in-memory fakes: nothing is downloaded or loaded from disk. ---

E5_PROTOCOL = {
    "model_id": "intfloat/multilingual-e5-small",
    "revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
    "device": "cpu",
    "dtype": "float32",
    "dimension": 384,
    "prefix": "query: ",
    "max_tokens": 512,
    "normalization": "phase1-nfkc-casefold-whitespace-v1",
    "pooling": "mean",
    "batch_size": 8,
}
RECEIPT = {
    "model_id": E5_PROTOCOL["model_id"],
    "revision": E5_PROTOCOL["revision"],
    "files": {"model.safetensors": {"bytes": 1, "sha256": "f" * 64}},
}


def text_features(texts) -> torch.Tensor:
    return torch.tensor(
        [
            [len(t) / 10, sum(map(ord, t)) % 97 / 97, t.count("a") / 3, 1.0]
            for t in texts
        ]
    )


class FakeSentenceTransformer(torch.nn.Module):
    def __init__(self, path, **kwargs):
        super().__init__()
        assert (
            kwargs["local_files_only"] is True and kwargs["trust_remote_code"] is False
        )
        self.linear = torch.nn.Linear(4, 8)
        self.max_seq_length = 128
        self.tokenizer = lambda texts, truncation, padding: {
            "input_ids": [[0] * (4 * len(t)) for t in texts]
        }

    def tokenize(self, texts):
        return {"features": text_features(texts)}

    def forward(self, features):
        return {"sentence_embedding": self.linear(features["features"])}


def e5_config(tmp_path: Path, **overrides):
    return config(
        **{
            "model": {
                "architecture": "sentence-transformers-e5",
                "pretrained": {**E5_PROTOCOL, "model_path": str(tmp_path / "e5-small")},
            },
            "max_steps": 2,
            **overrides,
        }
    )


def test_sentence_transformers_route_is_reachable_and_refuses_truncation(
    corpus, tmp_path: Path, monkeypatch
) -> None:
    import watchlist.embeddings

    _, _, data = corpus
    monkeypatch.setattr(watchlist.embeddings, "verify_model", lambda _config: RECEIPT)
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=FakeSentenceTransformer),
    )
    model = build_model(e5_config(tmp_path))
    assert (
        isinstance(model, training.SentenceTransformerBiEncoder)
        and model.model.max_seq_length == 512
    )
    with pytest.raises(training.TruncationError):
        model(["a" * 150])
    trained, result = train(e5_config(tmp_path), data, tmp_path / "run")
    assert result.steps == 2 and result.lineage["base"] == "pretrained"
    assert result.lineage["pretrained_weights"]["revision"] == E5_PROTOCOL["revision"]
    manifest = export_artifact(
        trained, e5_config(tmp_path), data, tmp_path / "artifact", {"loss": 1.0}
    )
    assert manifest["derived_from"]["pretrained_weights"][
        "receipt_files_digest"
    ] == canonical_digest(RECEIPT["files"])
    with pytest.raises(TrainingError, match="protected"):
        train(e5_config(tmp_path), data, tmp_path / "e5-small" / "run")


class FakePairModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.linear = torch.nn.Linear(1, 1)
        self.config = types.SimpleNamespace(num_labels=1)

    def forward(self, input_ids):
        return types.SimpleNamespace(
            logits=self.linear(input_ids.float().mean(-1, keepdim=True))
        )


def fake_pair_tokenizer(left, right, truncation, padding, return_tensors):
    assert truncation is False
    width = max(2 * (len(a) + len(b)) for a, b in zip(left, right))
    return {"input_ids": torch.tensor([[len(a)] * width for a in left])}


def test_cross_encoder_route_trains_with_bce_and_refuses_truncation(
    corpus, tmp_path: Path, monkeypatch
) -> None:
    _, _, data = corpus
    protocol = {"max_tokens": 512, "model_path": str(tmp_path / "mmarco-minilm")}
    reranking = types.SimpleNamespace(
        validate_config=lambda _c: None, verify_model=lambda _c: RECEIPT
    )
    transformers = types.SimpleNamespace(
        AutoTokenizer=types.SimpleNamespace(
            from_pretrained=lambda *_a, **_k: fake_pair_tokenizer
        ),
        AutoModelForSequenceClassification=types.SimpleNamespace(
            from_pretrained=lambda *_a, **_k: FakePairModel()
        ),
    )
    monkeypatch.setitem(sys.modules, "watchlist.reranking", reranking)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    model_section = {"architecture": "cross-encoder-minilm", "pretrained": protocol}
    bce = config(objective="bce", max_steps=2, model=model_section)
    model = build_model(bce)
    assert isinstance(model, training.MiniLMCrossEncoder) and model(
        ["Ana"], ["Anna"]
    ).shape == (1,)
    with pytest.raises(training.TruncationError):
        model(["a" * 150], ["b" * 150])
    _, result = train(bce, data, tmp_path / "run")
    assert (
        result.status == "completed_max_steps"
        and result.lineage["base"] == "pretrained"
    )
    monkeypatch.setitem(sys.modules, "watchlist.reranking", None)
    with pytest.raises(TrainingError, match="cross-encoder route unavailable"):
        load_training_config({**TINY, "objective": "bce", "model": model_section})


@pytest.mark.parametrize(
    "model",
    [
        {"architecture": "sentence-transformers-e5"},
        {
            "architecture": "sentence-transformers-e5",
            "pretrained": {**E5_PROTOCOL, "model_path": "x", "max_tokens": 9},
        },
        {
            "architecture": "char-ngram-bi-encoder-v1",
            "pretrained": {**E5_PROTOCOL, "model_path": "x"},
        },
    ],
)
def test_pretrained_route_configs_are_validated(model: dict) -> None:
    with pytest.raises(TrainingError):
        load_training_config({**TINY, "model": model})
    with pytest.raises(TrainingError):
        load_training_config(
            {
                **TINY,
                "objective": "bce",
                "model": {**model, "architecture": "sentence-transformers-e5"},
            }
        )
