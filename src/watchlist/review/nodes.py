"""v2 graph nodes, the deterministic explanation template and retriever adapters.

Only ``retrieve``, ``prepare_review`` and ``audit`` write the ledger, each in
one short transaction and idempotently. ``retrieve`` reuses saved ledger
evidence whenever it exists, so a replayed node never retrieves twice.
``human_review`` is unconditional and side-effect free before its interrupt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from importlib.metadata import version
import json
from pathlib import Path
from typing import Any, Callable, Sequence

from langgraph.types import interrupt

from watchlist.entities import Entity, load_entities
from watchlist.normalize import normalize_name
from watchlist.review.contracts import (
    EXPLANATION_TEMPLATE,
    LIMITATION,
    PACKET_SCHEMA,
    AcceptedReview,
    AliasEvidence,
    Candidate,
    ContextProvider,
    ContextProviderError,
    ContextRequest,
    Explanation,
    ReentrantCallError,
    RetrievalError,
    RetrievalResult,
    Retriever,
    RetrieverDescriptor,
    RetrieverMismatchError,
    RetrieverSettings,
    ReviewError,
    ReviewPacket,
    StorageIntegrityError,
    WorkflowPolicy,
    digest,
)
from watchlist.review.recovery import final_payload
from watchlist.review.storage import Ledger
from watchlist.review.validation import validate_context, validate_retrieval


def _no_failpoint(stage: str) -> None:
    return None


@dataclass(frozen=True)
class NodeDeps:
    """Runtime resources injected into nodes; never serialized into checkpoints.

    ``retriever_factory`` is called only when retrieval must actually run, so
    recovery after evidence exists never constructs or calls a retriever.
    """

    ledger: Ledger
    policy: WorkflowPolicy
    retriever_factory: Callable[[], Retriever]
    context_providers: tuple[ContextProvider, ...] = ()
    clock: Callable[[], str] = field(default=lambda: "")
    failpoint: Callable[[str], None] = _no_failpoint


def render_explanation(packet: ReviewPacket, review: AcceptedReview) -> Explanation:
    """Render only saved facts and the explicitly attributed reviewer opinion."""
    form = review.submission
    retrieval = packet.retrieval
    refs = [
        ref
        for c in retrieval.candidates
        if c.entity_id == form.candidate_entity_id
        for ref in c.evidence_ids
    ]
    message = (
        f"Saved retrieval ({retrieval.retriever.kind}: {retrieval.retriever.name}) returned "
        f"{len(retrieval.candidates)} candidate records for review. "
        f"Reviewer label {json.dumps(form.actor, ensure_ascii=True)} (origin: {form.origin}) "
        f"recorded {form.outcome} with selected record {json.dumps(form.candidate_entity_id, ensure_ascii=True)}. "
        f"Reviewer opinion: {json.dumps(form.reason, ensure_ascii=True)}. "
        f"Evidence references: {', '.join(refs) or 'saved candidate packet; no selected record'}. "
        f"Packet SHA-256: {form.packet_sha256}."
    )
    return Explanation(EXPLANATION_TEMPLATE, message, tuple(refs), LIMITATION)


def make_nodes(deps: NodeDeps) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    ledger = deps.ledger

    def normalize(state):
        header = ledger.header(state["case_id"])
        normalized = normalize_name(header.name)
        if normalized != header.normalized_name:
            raise StorageIntegrityError(
                "saved normalized name differs from the current normalizer",
                details={"case_id": header.case_id},
            )
        return {"normalized_name": normalized}

    def retrieve(state):
        case_id = state["case_id"]
        saved = ledger.evidence(case_id)
        if saved is not None:
            return {"evidence_sha256": digest(saved.to_dict())}
        header = ledger.header(case_id)
        retriever = deps.retriever_factory()
        try:
            current = retriever.descriptor()
        except ReviewError:
            raise
        except Exception as exc:
            raise RetrievalError(
                f"retriever descriptor failed: {type(exc).__name__}"
            ) from exc
        if current != header.retriever:
            raise RetrieverMismatchError(
                "configured retriever differs from the one frozen in the case",
                details={
                    "case_id": case_id,
                    "case_retriever": header.retriever.to_dict(),
                    "current_retriever": current.to_dict(),
                },
            )
        try:
            raw = retriever.search(header.normalized_name, top_k=deps.policy.top_k)
        except ReviewError:
            raise
        except Exception as exc:
            raise RetrievalError(
                f"retriever failed: {type(exc).__name__}: {str(exc)[:200]}",
                details={"case_id": case_id},
            ) from exc
        try:
            result = validate_retrieval(
                raw, expected=header.retriever, top_k=deps.policy.top_k
            )
        except ReviewError as exc:
            raise RetrievalError(
                f"retriever returned invalid evidence: {exc.message}",
                details={"case_id": case_id},
            ) from exc
        sha = ledger.record_evidence(case_id, result, deps.clock())
        deps.failpoint("after_evidence")
        return {"evidence_sha256": sha}

    def format_packet(state):
        case_id = state["case_id"]
        saved = ledger.packet(case_id)
        if saved is not None:
            packet, sha = saved
            return {"packet": packet.to_dict(), "packet_sha256": sha}
        header = ledger.header(case_id)
        evidence = ledger.evidence(case_id)
        if evidence is None or digest(evidence.to_dict()) != state["evidence_sha256"]:
            raise StorageIntegrityError(
                "formatted packet lacks its saved evidence",
                details={"case_id": case_id},
            )
        names = tuple(provider.name for provider in deps.context_providers)
        if names != header.context_providers:
            raise ContextProviderError(
                "context providers differ from those recorded for the case",
                details={
                    "case_id": case_id,
                    "case": list(header.context_providers),
                    "current": list(names),
                },
            )
        request = ContextRequest(
            case_id,
            header.origin,
            header.name,
            header.normalized_name,
            tuple(c.entity_id for c in evidence.candidates),
            header.retriever,
            tuple(sorted({item.source for item in evidence.evidence})),
        )
        context = {}
        for provider in deps.context_providers:
            try:
                data = provider.provide(request)
            except Exception as exc:
                # Never pass a provider's own error code through as if the
                # service raised it (e.g. a stray stale_submission).
                cause = exc.code if isinstance(exc, ReviewError) else type(exc).__name__
                retryable = False if isinstance(exc, ReentrantCallError) else None
                raise ContextProviderError(
                    f"context provider {provider.name} failed: {cause}",
                    details={
                        "case_id": case_id,
                        "provider": provider.name,
                        "cause": cause,
                    },
                    retryable=retryable,
                ) from exc
            try:
                context[provider.name] = {
                    "version": provider.version,
                    "data": validate_context(
                        provider.name, data, deps.policy.context_max_bytes
                    ),
                }
            except ReviewError as exc:
                raise ContextProviderError(
                    exc.message, details={"case_id": case_id}
                ) from exc
        packet = ReviewPacket(
            PACKET_SCHEMA,
            case_id,
            header.workflow,
            header.origin,
            header.name,
            header.normalized_name,
            evidence,
            context,
            LIMITATION,
        )
        return {"packet": packet.to_dict(), "packet_sha256": packet.sha256}

    def prepare_review(state):
        packet = ReviewPacket.from_dict(state["packet"])
        if packet.sha256 != state["packet_sha256"]:
            raise StorageIntegrityError(
                "checkpoint packet digest mismatch",
                details={"case_id": state["case_id"]},
            )
        ledger.record_packet(state["case_id"], packet, deps.clock())
        deps.failpoint("after_packet")
        return {"review_status": "pending"}

    def human_review(state):
        # Must stay unconditional and side-effect-free before interrupt().
        value = interrupt(
            {
                "case_id": state["case_id"],
                "packet_sha256": state["packet_sha256"],
                "message": "Explicit human review required. " + LIMITATION,
            }
        )
        saved = ledger.submission(state["case_id"])
        if saved is None or saved.to_dict() != value:
            raise StorageIntegrityError(
                "resume value does not match the accepted submission",
                details={"case_id": state["case_id"]},
            )
        return {"review": value, "review_status": "recorded"}

    def explain(state):
        deps.failpoint("during_explanation")
        explanation = render_explanation(
            ReviewPacket.from_dict(state["packet"]),
            AcceptedReview.from_dict(state["review"]),
        )
        return {"explanation": explanation.to_dict()}

    def audit(state):
        payload = final_payload(
            state["packet_sha256"], state["review"], state["explanation"]
        )
        receipt = ledger.finalize(state["case_id"], payload, deps.clock())
        deps.failpoint("after_final_audit")
        return {
            "audit_status": "committed",
            "final_event_sha256": receipt.event_sha256,
            "final_event_sequence": receipt.sequence,
        }

    return {
        "normalize": normalize,
        "retrieve": retrieve,
        "format_packet": format_packet,
        "prepare_review": prepare_review,
        "human_review": human_review,
        "explain": explain,
        "audit": audit,
    }


# ---------------------------------------------------------------------------
# Retriever adapters.
# ---------------------------------------------------------------------------


def _attribution(entity: Entity) -> str:
    if entity.synthetic:
        return "Authored fictional fixture; not real persons or organizations."
    provenance = entity.provenance
    return f"{provenance.provider}; publisher: {provenance.publisher}; terms: {provenance.license_url}"


def _evidence_for(entity: Entity) -> tuple[list[AliasEvidence], tuple[str, ...]]:
    """All alias spellings of one record, with readable stable evidence IDs."""
    items = [
        AliasEvidence(
            f"{entity.entity_id}/{alias.alias_id}",
            entity.entity_id,
            alias.alias_id,
            alias.name,
            entity.source,
            entity.synthetic,
            _attribution(entity),
            asdict(entity.provenance) if entity.provenance else None,
        )
        for alias in entity.aliases
    ]
    return items, tuple(item.evidence_id for item in items)


def reference_digest(entities: Sequence[Entity]) -> str:
    return digest([asdict(entity) for entity in entities])


class RapidFuzzRetriever:
    """Default adapter: deterministic Levenshtein ranking over packaged fictional data.

    Scores are ``normalized_levenshtein_similarity_x100`` (0-100), not
    probabilities. Ties break by entity ID, as in ``watchlist.matching``.
    """

    def __init__(
        self,
        entities: Sequence[Entity] | None = None,
        *,
        dataset: str = "packaged-fictional",
    ):
        self._entities = tuple(load_entities() if entities is None else entities)
        self._dataset = dataset
        self._descriptor = RetrieverDescriptor(
            "rapidfuzz",
            "watchlist.matching.rank_candidates",
            f"rapidfuzz-{version('rapidfuzz')}",
            reference_digest(self._entities),
            {
                "dataset": dataset,
                "score_kind": "normalized_levenshtein_similarity_x100",
            },
        )

    def descriptor(self) -> RetrieverDescriptor:
        return self._descriptor

    def search(self, normalized_name: str, *, top_k: int) -> RetrievalResult:
        from watchlist.matching import rank_candidates

        by_id = {entity.entity_id: entity for entity in self._entities}
        candidates, evidence = [], []
        for rank, match in enumerate(
            rank_candidates(normalized_name, self._entities, top_k=top_k), 1
        ):
            items, refs = _evidence_for(by_id[match.entity_id])
            evidence.extend(items)
            candidates.append(
                Candidate(
                    rank,
                    match.entity_id,
                    match.display_name,
                    match.entity_type,
                    match.matched_alias_id,
                    match.matched_alias,
                    float(match.similarity_score),
                    "normalized_levenshtein_similarity_x100",
                    refs,
                )
            )
        return RetrievalResult(
            self._descriptor, top_k, tuple(candidates), tuple(evidence)
        )


class E5Retriever:
    """Optional adapter around the existing local E5 encoder and exact ranker.

    Nothing heavy is imported until ``descriptor()`` (model receipt check) or
    ``search()`` (model load) runs. No network access or download is attempted;
    a missing local model is a ``RetrievalError``. Not exercised by the unit
    tests, which use fakes; opt-in integration requires approved local weights.
    """

    def __init__(
        self,
        embedding_config: Path | str,
        entities: Sequence[Entity] | None = None,
        *,
        dataset: str = "packaged-fictional",
    ):
        self._config_path = Path(embedding_config)
        self._entities = None if entities is None else tuple(entities)
        self._dataset = dataset
        self._descriptor: RetrieverDescriptor | None = None
        self._encoder = None

    def _reference(self) -> tuple[Entity, ...]:
        if self._entities is None:
            self._entities = load_entities()
        return self._entities

    def _config(self) -> dict[str, Any]:
        try:
            return json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RetrievalError(f"cannot read embedding config: {exc}") from None

    def descriptor(self) -> RetrieverDescriptor:
        if self._descriptor is None:
            config = self._config()
            try:
                from watchlist.embeddings import verify_model

                receipt = verify_model(config["model"])
            except (OSError, ValueError, KeyError) as exc:
                raise RetrievalError(
                    f"local E5 model is unavailable or unverified: {exc}"
                ) from None
            self._descriptor = RetrieverDescriptor(
                "e5",
                str(config["model"].get("model_id")),
                f"e5-{config['model'].get('revision')}",
                reference_digest(self._reference()),
                {
                    "dataset": self._dataset,
                    "score_kind": "cosine_similarity",
                    "model": config["model"],
                    "model_receipt_sha256": digest(receipt),
                    "cache_path": str(config.get("cache_path")),
                },
            )
        return self._descriptor

    def search(self, normalized_name: str, *, top_k: int) -> RetrievalResult:
        from watchlist.embeddings import E5Encoder, cached_aliases
        from watchlist.matching_embeddings import rank_vectors

        descriptor = self.descriptor()
        config = self._config()
        entities = self._reference()
        if self._encoder is None:
            self._encoder = E5Encoder(config["model"])
        vectors, _ = cached_aliases(entities, self._encoder, Path(config["cache_path"]))
        ranked = (
            rank_vectors(
                self._encoder.encode([normalized_name]), vectors, entities, top_k=top_k
            )
            if entities
            else []
        )
        by_id = {entity.entity_id: entity for entity in entities}
        candidates, evidence = [], []
        for rank, match in enumerate(ranked, 1):
            items, refs = _evidence_for(by_id[match.entity_id])
            evidence.extend(items)
            candidates.append(
                Candidate(
                    rank,
                    match.entity_id,
                    match.display_name,
                    match.entity_type,
                    match.matched_alias_id,
                    match.matched_alias,
                    float(match.similarity_score),
                    "cosine_similarity",
                    refs,
                )
            )
        return RetrievalResult(descriptor, top_k, tuple(candidates), tuple(evidence))


def make_retriever(settings: RetrieverSettings) -> Retriever:
    """Build the configured default adapter; E5 stays lazy until used."""
    if settings.kind == "rapidfuzz":
        return RapidFuzzRetriever(dataset=settings.dataset)
    return E5Retriever(settings.embedding_config, dataset=settings.dataset)
