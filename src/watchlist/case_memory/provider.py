"""``ContextProvider`` that attaches prior dated annotations to new review packets.

Plug into the one v2 backend::

    store = MemoryStore.open(memory_dir, MemoryConfig.load("config/memory.json"))
    service = ReviewService.open(workspace, review_config,
                                 context_providers=[MemoryContextProvider(store)])

The service freezes the returned JSON into ``packet.context['case_memory']`` and
the packet digest, so the reviewer's form is bound to exactly the history that
was shown. The provider receives only ``ContextRequest`` (candidate entity IDs
in ranking order and the retriever descriptor); it cannot change candidates,
ranks, scores or outcomes, and it never sees or stores the query for lookup.

Lookup keys are ``(registered namespace of the snapshot, exact entity ID,
snapshot fingerprint)``. An unregistered snapshot yields an explicit
``unregistered_snapshot`` status, never an empty history; an unavailable or
corrupt store raises, which the service reports as ``context_provider_failed``
(the case stays recoverable).

Source binding at lookup time: the service supplies labels from frozen alias
evidence. Missing bindings or labels outside the snapshot's registered
namespace yield source_mismatch and no cards, even if an adapter reuses a
fingerprint incorrectly. Lookup never uses a query name.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Callable, Mapping, Sequence

from watchlist.case_memory.identifiers import validate_identifier
from watchlist.case_memory.lookup import build_context, check_freshness_in_session
from watchlist.case_memory.state import (
    MEMORY_NOTICE,
    PROVIDER_NAME,
    PROVIDER_VERSION,
)
from watchlist.case_memory.store import MemoryStore
from watchlist.review.contracts import (
    ContextRequest,
    ReviewPacket,
    StaleSubmissionError,
)


class MemoryContextProvider:
    """Display-only reviewer-memory context (implements ``ContextProvider``)."""

    name = PROVIDER_NAME
    version = PROVIDER_VERSION

    def __init__(self, store: MemoryStore, *, clock: Callable[[], str] | None = None):
        self._store = store
        self._clock = clock or store.now

    def provide(self, request: ContextRequest) -> Mapping[str, Any]:
        snapshot = request.retriever.reference_sha256
        namespace = self._store.registry.namespace_for_snapshot(snapshot)
        if namespace is None:
            return {
                "status": "unregistered_snapshot",
                "notice": MEMORY_NOTICE,
                "snapshot_sha256": snapshot,
                "message": (
                    "Memory is not available for this reference snapshot because it is "
                    "not registered. This is not an empty history."
                ),
                "keys": [],
            }
        mismatch = _source_mismatch(request, namespace.sources)
        if mismatch:
            return {
                "status": "source_mismatch",
                "notice": MEMORY_NOTICE,
                "snapshot_sha256": snapshot,
                "namespace_id": namespace.namespace_id,
                "message": (
                    "Memory is not shown: candidate evidence comes from a source that "
                    "does not belong to this snapshot's registered namespace. This is "
                    "not an empty history."
                ),
                "mismatched_sources": mismatch,
                "keys": [],
            }
        refs = self._store.registry.refs(
            snapshot,
            [
                validate_identifier(e, "candidate entity ID")
                for e in request.candidate_entity_ids
            ],
        )
        with self._store.session(write=False) as mdb:
            return build_context(
                mdb,
                refs,
                policy=self._store.config.policy,
                case_origin=request.origin,
                now=self._clock(),
                namespace_id=namespace.namespace_id,
                snapshot_sha256=snapshot,
                exclude_case_id=request.case_id,
            )

    @contextmanager
    def guard_review(self, packet: ReviewPacket):
        """Keep memory stable from the freshness check through durable acceptance.

        Lock order is review workspace then memory store, the same as packet
        creation. An already accepted review is replayed before this hook.
        """
        context = memory_context(packet)
        if context is None:
            yield
            return
        with self._store.session(write=False) as mdb:
            freshness = check_freshness_in_session(
                mdb, context, self._store.config.policy, now=self._clock()
            )
            if not freshness.fresh:
                raise StaleSubmissionError(
                    "reviewer memory changed or expired; start a new case to review fresh context",
                    details={"case_id": packet.case_id, "next_action": "start_case"},
                )
            yield


def _source_mismatch(request: ContextRequest, allowed: Sequence[str]) -> list[str]:
    """Refuse absent source binding or any foreign evidence source."""
    sources = request.candidate_sources
    if sources is None or (request.candidate_entity_ids and not sources):
        return ["missing_source_binding"]
    return sorted({str(s) for s in sources if s not in allowed})


def memory_context(packet: ReviewPacket | None) -> dict[str, Any] | None:
    """The frozen memory context of a packet, or None when the case had no provider."""
    if packet is None:
        return None
    entry = packet.context.get(PROVIDER_NAME)
    return None if entry is None else entry["data"]


def render_context_text(context: Mapping[str, Any] | None) -> str:
    """Deterministic plain-text rendering for terminals; free text is JSON-escaped."""
    if context is None:
        return "Reviewer memory: not attached to this case."
    lines = [MEMORY_NOTICE]
    if context.get("status") != "ok":
        lines.append(
            f"Reviewer memory status: {context.get('status')}. {context.get('message', '')}"
        )
        return "\n".join(lines)
    lines.append(
        f"Lookup at {context['lookup_at']} (memory event {context['memory_head']['sequence']}); "
        f"displayable origins: {', '.join(context['displayable_origins']) or 'none'}."
    )
    lines.extend(f"Warning: {w}" for w in context.get("warnings", []))
    for key in context["keys"]:
        omitted = {k: v for k, v in key["omitted"].items() if v}
        lines.append(
            f"- {key['namespace_id']}:{key['source_entity_id']} - {key['shown_count']} shown of "
            f"{key['eligible_count']} eligible; omitted {json.dumps(omitted, sort_keys=True)}"
            + (
                "; differing earlier outcomes exist"
                if key["differing_outcomes"]
                else ""
            )
        )
        for card in key["cards"]:
            lines.append(
                f"    {card['reviewed_at']} case {card['case_id']} origin {card['origin']}: "
                f"{card['outcome']} ({card['relation']}). Opinion: "
                f"{json.dumps(card['reviewer_opinion'], ensure_ascii=True)} "
                f"[label {json.dumps(card['actor_label'], ensure_ascii=True)}]"
            )
    return "\n".join(lines)
