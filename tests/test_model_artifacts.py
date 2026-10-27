"""Derived artifacts are sealed, verified on load and never overwrite originals."""

import json
from pathlib import Path

import pytest

from watchlist import model_artifacts
from watchlist.model_artifacts import (
    ArtifactError,
    atomic_write_bytes,
    canonical_digest,
    is_protected_directory,
    verify_artifact,
    write_artifact,
)

DIGEST = "a" * 64


def sealed(directory: Path, **overrides) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "weights.bin").write_bytes(b"fictional weights")
    arguments = {
        "payload_files": ["weights.bin"],
        "config": {"seed": 1},
        "data_manifest_digest": DIGEST,
        "metrics": {"loss": 0.5},
        "derived_from": {"base": "random-init"},
        **overrides,
    }
    return write_artifact(directory, **arguments)


def test_round_trip_records_lineage_and_is_not_a_pretrained_original(
    tmp_path: Path,
) -> None:
    manifest = sealed(tmp_path / "artifact", packages={"torch": "x"})
    assert verify_artifact(tmp_path / "artifact") == manifest
    assert manifest["is_pretrained_original"] is False
    assert manifest["artifact_kind"] == "derived-trained-model"
    assert manifest["data_manifest_digest"] == DIGEST
    assert "training.py" in manifest["code"] and manifest["environment"][
        "packages"
    ] == {"torch": "x"}
    assert manifest["files"]["weights.bin"]["bytes"] == len(b"fictional weights")


def test_tampered_payload_or_manifest_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "artifact"
    sealed(directory)
    (directory / "weights.bin").write_bytes(b"fictional weightz")
    with pytest.raises(ArtifactError, match="checksum"):
        verify_artifact(directory)
    (directory / "weights.bin").write_bytes(b"fictional weights")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    (directory / "manifest.json").write_text(
        json.dumps({**manifest, "metrics": {"loss": 0.0}}), encoding="utf-8"
    )
    with pytest.raises(ArtifactError, match="digest"):
        verify_artifact(directory)


def test_resealed_pretrained_claim_or_extra_fields_are_refused(tmp_path: Path) -> None:
    directory = tmp_path / "artifact"
    manifest = sealed(directory)
    body = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    body["is_pretrained_original"] = True
    (directory / "manifest.json").write_text(
        json.dumps({**body, "manifest_digest": canonical_digest(body)}),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError, match="pretrained"):
        verify_artifact(directory)
    (directory / "manifest.json").write_text(
        json.dumps({**manifest, "extra": 1}), encoding="utf-8"
    )
    with pytest.raises(ArtifactError, match="schema"):
        verify_artifact(directory)
    (directory / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactError, match="JSON"):
        verify_artifact(directory)


def test_unsealed_missing_or_escaping_payloads_are_refused(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="incomplete"):
        verify_artifact(tmp_path)
    with pytest.raises(ArtifactError, match="missing or outside"):
        sealed(tmp_path / "a", payload_files=["../weights.bin"])
    with pytest.raises(ArtifactError, match="digest"):
        sealed(tmp_path / "b", data_manifest_digest="short")
    with pytest.raises(ArtifactError, match="at least one"):
        sealed(tmp_path / "c", payload_files=[])


def test_frozen_artifacts_and_pretrained_directories_are_not_overwritten(
    tmp_path: Path,
) -> None:
    sealed(tmp_path / "artifact")
    with pytest.raises(ArtifactError, match="already sealed"):
        sealed(tmp_path / "artifact")
    original = tmp_path / "e5-small"
    original.mkdir()
    (original / "acquisition.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ArtifactError, match="pretrained"):
        sealed(original / "derived")
    assert is_protected_directory(original / "derived") and is_protected_directory(
        tmp_path / "artifact"
    )
    unrelated = tmp_path / "other"
    unrelated.mkdir()
    (unrelated / "manifest.json").write_text('{"snapshot": 1}', encoding="utf-8")
    assert not is_protected_directory(unrelated / "child")


def test_atomic_write_leaves_previous_file_when_rename_fails(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "state.bin"
    atomic_write_bytes(target, b"old")

    def broken(*_args):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(model_artifacts.os, "replace", broken)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.bin"]


def reseal_files(directory: Path, files: dict) -> None:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    body = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    body["files"] = files
    (directory / "manifest.json").write_text(
        json.dumps({**body, "manifest_digest": canonical_digest(body)}),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "name",
    [
        "/etc/weights.bin",
        "C:/weights.bin",
        "C:weights.bin",
        r"\\host\share\w",
        r"..\weights.bin",
        "../weights.bin",
        "weights:stream",
        "folder/NUL.bin",
        "CON",
        "COM1.pt",
        "weights?.bin",
        "folder./weights.bin",
        "folder /weights.bin",
        "folder//weights.bin",
        "./weights.bin",
        r"\weights.bin",
        "",
    ],
)
def test_payload_names_must_be_plain_relative_paths(tmp_path: Path, name: str) -> None:
    directory = tmp_path / "artifact"
    manifest = sealed(directory)
    reseal_files(directory, {name: manifest["files"]["weights.bin"]})
    with pytest.raises(ArtifactError, match="plain relative"):
        verify_artifact(directory)
    with pytest.raises(ArtifactError, match="missing or outside"):
        sealed(tmp_path / "other", payload_files=[name])


def test_resealed_empty_file_list_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "artifact"
    sealed(directory)
    reseal_files(directory, {})
    with pytest.raises(ArtifactError, match="no payload"):
        verify_artifact(directory)


@pytest.mark.parametrize(
    "info",
    [
        {},
        {"bytes": True, "sha256": "a" * 64},
        {"bytes": -1, "sha256": "a" * 64},
        {"bytes": 1, "sha256": "not-a-digest"},
    ],
)
def test_malformed_payload_metadata_is_an_artifact_error(tmp_path, info):
    directory = tmp_path / "artifact"
    sealed(directory)
    reseal_files(directory, {"weights.bin": info})
    with pytest.raises(ArtifactError, match="metadata"):
        verify_artifact(directory)


def test_symbolic_link_payload_is_refused_even_when_its_hash_matches(tmp_path):
    directory = tmp_path / "artifact"
    sealed(directory)
    payload = directory / "weights.bin"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(payload.read_bytes())
    payload.unlink()
    try:
        payload.symlink_to(outside)
    except OSError:
        pytest.skip("creating symbolic links requires OS permission")
    with pytest.raises(ArtifactError, match="symbolic"):
        verify_artifact(directory)
    other = tmp_path / "unsealed"
    other.mkdir()
    (other / "weights.bin").symlink_to(outside)
    with pytest.raises(ArtifactError, match="symbolic"):
        write_artifact(
            other,
            payload_files=["weights.bin"],
            config={},
            data_manifest_digest=DIGEST,
            metrics={},
            derived_from={},
        )
