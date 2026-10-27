"""Bounded acquisition and inference-only reranking for the approved MiniLM."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import time
from typing import Protocol, Sequence
from urllib.request import urlopen

import numpy as np

from watchlist.embeddings import file_hash
from watchlist.entities import Entity
from watchlist.normalize import normalize_name

MODEL_ID = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
FILES = (
    "README.md",
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
LICENSE_URL = "https://www.apache.org/licenses/LICENSE-2.0.txt"
DOWNLOAD_CAP = 600_000_000


class PairScorer(Protocol):
    """Allow deterministic test scorers without importing or loading Torch."""

    def score(self, pairs: list[tuple[str, str]]) -> np.ndarray: ...


def validate_config(config: dict) -> None:
    """Reject a substituted checkpoint or a changed inference protocol."""
    fixed = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "license": "apache-2.0",
        "device": "cpu",
        "dtype": "float32",
        "batch_size": 8,
        "max_tokens": 512,
        "normalization": "phase1-nfkc-casefold-whitespace-v1",
        "score": "raw_logit",
        "threads": 4,
    }
    if any(config.get(k) != v for k, v in fixed.items()):
        raise ValueError("configuration differs from the approved reranker protocol")


def verify_model(config: dict) -> dict:
    """Verify the complete local receipt and every file; never use the network."""
    validate_config(config)
    root = Path(config["model_path"])
    receipt = json.loads((root / "acquisition.json").read_text())
    if (
        receipt["model_id"] != MODEL_ID
        or receipt["revision"] != REVISION
        or receipt["license"] != "apache-2.0"
        or set(receipt["files"]) != set(FILES) | {"LICENSE-2.0.txt"}
        or receipt["total_bytes_with_preflight"] > DOWNLOAD_CAP
    ):
        raise ValueError("model receipt differs from approved acquisition")
    for name, info in receipt["files"].items():
        if (root / name).stat().st_size != info["bytes"] or file_hash(
            root / name
        ) != info["sha256"]:
            raise ValueError(f"model checksum/size mismatch: {name}")
    return receipt


def fetch_model(config: dict, prior_metadata_bytes: int = 0) -> dict:
    """Acquire only pinned files, verify license/hashes, enforce the total byte cap.

    Incomplete directories are deliberately not retried automatically. The caller
    accounts for any separately authorized preflight metadata transfer.
    """
    validate_config(config)
    if (
        type(prior_metadata_bytes) is not int
        or not 0 <= prior_metadata_bytes < DOWNLOAD_CAP
    ):
        raise ValueError("invalid prior acquisition byte count")
    root = Path(config["model_path"])
    if (root / "acquisition.json").exists():
        return verify_model(config)
    if root.exists() and any(root.iterdir()):
        raise ValueError("incomplete model directory; inspect before retrying")
    url = f"https://huggingface.co/api/models/{MODEL_ID}/revision/{REVISION}?blobs=true"
    with urlopen(url, timeout=60) as response:
        metadata_bytes = response.read(8_000_001)
    if len(metadata_bytes) > 8_000_000:
        raise ValueError("metadata exceeds 8 MB cap")
    metadata = json.loads(metadata_bytes)
    if (
        metadata["sha"] != REVISION
        or metadata.get("cardData", {}).get("license") != "apache-2.0"
    ):
        raise ValueError("remote revision or license mismatch")
    items = {r["rfilename"]: r for r in metadata["siblings"]}
    selected_bytes = sum(items[name]["size"] for name in FILES)
    received = prior_metadata_bytes + len(metadata_bytes)
    if selected_bytes + received + 100_000 > DOWNLOAD_CAP:
        raise ValueError("approved 600 MB download cap exceeded")
    root.mkdir(parents=True, exist_ok=True)
    receipt = {
        "model_id": MODEL_ID,
        "revision": REVISION,
        "license": "apache-2.0",
        "license_url": LICENSE_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "prior_metadata_bytes": prior_metadata_bytes,
        "metadata_bytes": len(metadata_bytes),
        "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
        "files": {},
    }
    # Small model card first: independently check the pinned license declaration.
    for name in (*FILES[:2], "LICENSE-2.0.txt", *FILES[2:]):
        external = name == "LICENSE-2.0.txt"
        expected_size = 100_000 if external else items[name]["size"]
        address = (
            LICENSE_URL
            if external
            else f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{name}"
        )
        temporary = root / (name + ".partial")
        size = 0
        with urlopen(address, timeout=60) as response, temporary.open("wb") as stream:
            while chunk := response.read(min(1024 * 1024, expected_size - size + 1)):
                size += len(chunk)
                received += len(chunk)
                if size > expected_size or received > DOWNLOAD_CAP:
                    raise ValueError("download exceeds advertised size or approved cap")
                stream.write(chunk)
        if not external and size != expected_size:
            raise ValueError(
                f"incomplete model file {name}: {size}/{expected_size} bytes"
            )
        digest = file_hash(temporary)
        if not external:
            item = items[name]
            if "lfs" in item:
                if digest != item["lfs"]["sha256"]:
                    raise ValueError("upstream file checksum mismatch")
            else:
                blob = b"blob " + str(size).encode() + b"\0" + temporary.read_bytes()
                if hashlib.sha1(blob).hexdigest() != item["blobId"]:
                    raise ValueError("upstream Git blob checksum mismatch")
        if name == "README.md" and "license: apache-2.0" not in temporary.read_text():
            raise ValueError("pinned model card does not declare approved license")
        if external and "Apache License" not in temporary.read_text():
            raise ValueError("invalid license text")
        temporary.replace(root / name)
        receipt["files"][name] = {"bytes": size, "sha256": digest, "source": address}
        print(f"Acquired {name}: {size} bytes", flush=True)
    receipt["total_bytes_with_preflight"] = received
    (root / "acquisition.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def peak_memory_bytes() -> int:
    """Report process high-water RSS in bytes (macOS returns bytes, Linux KiB)."""
    import resource  # POSIX-only; imported lazily so Windows can import this module.

    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


class MiniLMScorer:
    """Joint pair inference with no prefix, sigmoid, training or hidden downloads."""

    def __init__(self, config: dict, deadline: float | None = None):
        receipt = verify_model(config)
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        torch.manual_seed(42)
        torch.set_num_threads(config["threads"])
        torch.use_deterministic_algorithms(True)
        self.torch = torch
        self.config = config
        self.deadline = deadline
        start = time.perf_counter()
        path = str(Path(config["model_path"]).resolve())
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
        self.model.to("cpu").eval()
        if self.model.config.num_labels != 1:
            raise ValueError("expected a single raw relevance logit")
        self.load_seconds = time.perf_counter() - start
        self.metadata = {
            "config": config,
            "receipt": receipt,
            "packages": {
                p: version(p) for p in ("torch", "transformers", "tokenizers", "numpy")
            },
        }
        self.check_budget()

    def check_budget(self) -> None:
        """Stop between batches if the approved runtime/memory envelope is exceeded."""
        if self.deadline is not None and time.perf_counter() > self.deadline:
            raise TimeoutError("30-minute inference budget exceeded")
        if peak_memory_bytes() > 4_000_000_000:
            raise MemoryError("4 GB process memory planning budget exceeded")

    def score(self, pairs: list[tuple[str, str]]) -> np.ndarray:
        """Normalize copies, reject truncation, and return one finite raw logit per pair."""
        output = []
        for start in range(0, len(pairs), self.config["batch_size"]):
            self.check_budget()
            batch = pairs[start : start + self.config["batch_size"]]
            left = [normalize_name(a) for a, _ in batch]
            right = [normalize_name(b) for _, b in batch]
            tokens = self.tokenizer(
                left, right, truncation=False, padding=True, return_tensors="pt"
            )
            if tokens["input_ids"].shape[1] > self.config["max_tokens"]:
                raise ValueError(
                    "pair exceeds token limit; no silent truncation allowed"
                )
            with self.torch.inference_mode():
                logits = self.model(**tokens).logits
            if tuple(logits.shape) != (len(batch), 1):
                raise ValueError("unexpected pair-score shape")
            output.extend(logits[:, 0].cpu().numpy().tolist())
            self.check_budget()
        scores = np.asarray(output, dtype=np.float32)
        if scores.shape != (len(pairs),) or not np.isfinite(scores).all():
            raise ValueError("invalid pair scores")
        return scores


def rerank(
    query: str,
    candidate_ids: Sequence[str],
    entities: Sequence[Entity],
    scorer: PairScorer,
) -> list[dict]:
    """Reorder exactly the supplied IDs using only aliases in the provided view."""
    normalize_name(query)
    by_id = {e.entity_id: e for e in entities}
    if len(by_id) != len(entities) or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("duplicate entity/candidate IDs")
    if not set(candidate_ids) <= set(by_id):
        raise ValueError("candidate not present in frozen reference view")
    if any(not by_id[eid].aliases for eid in candidate_ids):
        raise ValueError("candidate has no available aliases")
    evidence = [
        (by_id[eid], a)
        for eid in candidate_ids
        for a in sorted(by_id[eid].aliases, key=lambda a: a.alias_id)
    ]
    scores = (
        np.asarray(scorer.score([(query, a.name) for _, a in evidence]))
        if evidence
        else np.array([])
    )
    if scores.shape != (len(evidence),) or not np.isfinite(scores).all():
        raise ValueError("invalid pair score count or values")
    best = {}
    for (entity, alias), score in zip(evidence, scores):
        row = {
            "entity_id": entity.entity_id,
            "display_name": entity.name,
            "entity_type": entity.entity_type,
            "matched_alias_id": alias.alias_id,
            "matched_alias": alias.name,
            "raw_logit": float(score),
            "source": entity.source,
            "synthetic": entity.synthetic,
            "provenance": asdict(entity.provenance) if entity.provenance else None,
            "available_aliases": [asdict(a) for a in entity.aliases],
        }
        old = best.get(entity.entity_id)
        if old is None or (-row["raw_logit"], alias.alias_id) < (
            -old["raw_logit"],
            old["matched_alias_id"],
        ):
            best[entity.entity_id] = row
    return sorted(best.values(), key=lambda r: (-r["raw_logit"], r["entity_id"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["fetch", "verify"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        receipt = (
            fetch_model(config["model"], config.get("prior_metadata_bytes", 0))
            if args.action == "fetch"
            else verify_model(config["model"])
        )
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                k: receipt[k]
                for k in (
                    "model_id",
                    "revision",
                    "license",
                    "total_bytes_with_preflight",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
