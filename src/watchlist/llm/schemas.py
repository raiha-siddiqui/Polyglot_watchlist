"""Typed request, response and result contracts for grounded LLM use."""

from dataclasses import dataclass
import hashlib
import json
from typing import Literal

ENTITY_TYPES = ("person", "organisation")
PARSE_FIELDS = ("name", "alias", "entity_type", "country", "date", "identifier")
# Fields that could change retrieval or interpretation; a human confirms them.
# A model-chosen name is consequential too: it decides what is searched.
CONSEQUENTIAL_FIELDS = ("name", "alias", "entity_type", "country", "date", "identifier")
CLAIM_TYPES = (
    "candidate_count",
    "candidate_rank",
    "similarity_score",
    "matched_alias",
    "alias_source",
)


class LLMError(Exception):
    """A structured provider/replay failure; ``code`` is stable for reports."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ValidationFailure(Exception):
    """Model output that is not grounded, not schema-valid or not allowed."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def canonical(value: object) -> str:
    """One finite JSON representation for hashing receipts and packets."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LLMRequest:
    """A fully rendered prompt; the client sends exactly these strings."""

    task: Literal["parse", "explain", "judge"]
    prompt_id: str
    prompt_sha256: str
    system: str
    user: str
    max_output_tokens: int

    def size_chars(self) -> int:
        """Rendered request size in characters (system plus user text)."""
        return len(self.system) + len(self.user)

    def digest(self) -> str:
        """Replay key: binds task, frozen template and the rendered untrusted data."""
        return sha256_text(
            canonical(
                {
                    "task": self.task,
                    "prompt_id": self.prompt_id,
                    "prompt_sha256": self.prompt_sha256,
                    "system": self.system,
                    "user": self.user,
                    "max_output_tokens": self.max_output_tokens,
                }
            )
        )


@dataclass(frozen=True)
class LLMResponse:
    """Raw model text plus accounting fields; ``None`` means not measured."""

    text: str
    provider: str
    model: str
    request_sha256: str
    stop_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None
    evidence_kind: Literal["live_provider", "replayed_receipt", "fake_fixture"] = (
        "fake_fixture"
    )

    @property
    def response_sha256(self) -> str:
        return sha256_text(self.text)

    def accounting(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "request_sha256": self.request_sha256,
            "response_sha256": self.response_sha256,
            "stop_reason": self.stop_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "evidence_kind": self.evidence_kind,
        }


@dataclass(frozen=True)
class GroundedField:
    """One extracted value with the exact input span that supports it."""

    field: str
    value: str
    quote: str
    start: int
    end: int
    transform: str


@dataclass(frozen=True)
class StructuredQuery:
    """Parsed request. Nothing model-extracted feeds retrieval until a human confirms it."""

    source_text: str
    name: str | None
    aliases: tuple[str, ...]
    entity_type: str | None
    country: str | None
    dates: tuple[str, ...]
    identifiers: tuple[str, ...]
    method: Literal["llm_grounded", "deterministic", "deterministic_fallback"]
    fallback_reason: str | None
    grounding: tuple[GroundedField, ...] = ()
    ambiguities: tuple[str, ...] = ()
    requires_confirmation: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    model_call: dict | None = None
    prompt_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "source_text": self.source_text,
            "name": self.name,
            "aliases": list(self.aliases),
            "entity_type": self.entity_type,
            "country": self.country,
            "dates": list(self.dates),
            "identifiers": list(self.identifiers),
            "method": self.method,
            "fallback_reason": self.fallback_reason,
            "grounding": [vars(g) for g in self.grounding],
            "ambiguities": list(self.ambiguities),
            "requires_confirmation": list(self.requires_confirmation),
            "warnings": list(self.warnings),
            "model_call": self.model_call,
            "prompt_id": self.prompt_id,
        }


@dataclass(frozen=True)
class Claim:
    """One atomic, cited fact; ``text`` is rendered by code, never by the model."""

    claim_type: str
    citations: tuple[str, ...]
    values: tuple[tuple[str, object], ...]
    text: str


@dataclass(frozen=True)
class ExplanationResult:
    """Explanation output. It is advisory and is never written into review state."""

    template_version: str
    method: Literal["llm_grounded_claims", "deterministic_fallback"]
    claims: tuple[Claim, ...]
    text: str
    packet_sha256: str
    fallback_reason: str | None
    limitations: str
    model_call: dict | None = None
    prompt_id: str | None = None

    def to_dict(self) -> dict:
        """Serialized result; it contains no model-written prose, only code-rendered text."""
        return {
            "template_version": self.template_version,
            "method": self.method,
            "claims": [
                {
                    "claim_type": c.claim_type,
                    "citations": list(c.citations),
                    "values": dict(c.values),
                    "text": c.text,
                }
                for c in self.claims
            ],
            "text": self.text,
            "packet_sha256": self.packet_sha256,
            "fallback_reason": self.fallback_reason,
            "limitations": self.limitations,
            "model_call": self.model_call,
            "prompt_id": self.prompt_id,
        }
