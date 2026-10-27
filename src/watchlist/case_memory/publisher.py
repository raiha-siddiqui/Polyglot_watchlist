"""Replay-safe outbox publication of completed v2 reviews into memory.

Two durable steps, each idempotent:

1. ``enqueue`` reads completed cases **through the v2 ReviewService** (read-only
   ``list_cases``/``get_case``/``verify_audit``; no review logic is copied),
   projects each into an ``Annotation`` and stores one publication intent per
   case (the outbox row). Same case + same payload is a no-op; same case +
   different payload is quarantined, never overwritten.
2. ``drain`` publishes each pending intent in **one** memory transaction:
   annotation row, exact-key record rows, the ``annotation_published`` event
   and key-revision increments commit together or not at all.

A crash before an intent commits leaves nothing; a crash after it leaves a
pending intent that the next ``drain`` publishes; a crash inside the publish
transaction is rolled back by SQLite; a crash after commit is a no-op replay.
Therefore retries produce no duplicates and lose nothing.

One bad entry never blocks the others. ``enqueue`` refuses a submission ID
that another case already uses (v2 enforces submission IDs per workspace
only) by quarantining it before any outbox row is written. If an intent still
cannot be published, ``drain`` rolls back that one publication, records a
``publication_blocked`` quarantine row and event in its own transaction, and
keeps draining. Blocked intents are excluded from "pending" and reported as
``blocked`` by ``drain``, ``MemoryStore.verify`` and the context provider. The review ledger is
read before the memory lock is taken and released first (lock order: review,
then memory). Cases tombstoned by retention are never republished.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Iterable

from watchlist.case_memory.identifiers import Registry, record_key, validate_identifier
from watchlist.case_memory.state import (
    ANNOTATION_SCHEMA,
    Annotation,
    AnnotationRecord,
    CaseMemoryError,
    MemoryInputError,
    MemoryIntegrityError,
    PublicationConflictError,
    annotation_id_for,
)
from watchlist.case_memory.store import (
    BLOCKED_REASON,
    PENDING_INTENTS_WHERE,
    MemoryDb,
    MemoryStore,
    blocked_intent_count,
)
from watchlist.review.contracts import (
    CaseView,
    ReviewError,
    _Contract,
    canonical,
    digest,
)


def annotation_from_case(
    view: CaseView, workspace_id: str, registry: Registry
) -> Annotation:
    """Project one completed, verified v2 case into a memory annotation.

    Records come only from the frozen candidate evidence: exact entity IDs and
    alias IDs, the snapshot fingerprint of the retriever's reference data and
    the registered namespace that owns it. Evidence whose ``source`` label
    belongs to another namespace is refused (no cross-source links).
    """
    workspace_id = validate_identifier(workspace_id, "workspace_id")
    if view.status != "completed" or view.review is None or view.packet is None:
        raise MemoryInputError(
            "only completed cases with an accepted review are published",
            details={"case_id": view.case_id, "status": view.status},
        )
    if view.final_receipt is None or view.packet_sha256 is None:
        raise MemoryInputError(
            "completed case lacks its final audit receipt",
            details={"case_id": view.case_id},
        )
    packet, form = view.packet, view.review.submission
    retrieval = packet.retrieval
    snapshot = retrieval.retriever.reference_sha256
    namespace = registry.require_namespace(snapshot)
    evidence_by_entity: dict[str, list[Any]] = {}
    for item in retrieval.evidence:
        registry.check_source(namespace, item.source)
        evidence_by_entity.setdefault(item.entity_id, []).append(item)
    records = []
    for candidate in retrieval.candidates:
        if form.outcome == "candidate_for_follow_up":
            relation = (
                "selected_for_follow_up"
                if candidate.entity_id == form.candidate_entity_id
                else "considered_without_selection"
            )
        elif form.outcome == "unresolved":
            relation = "unresolved_context"
        else:
            relation = "considered_without_selection"
        aliases = [e.alias_id for e in evidence_by_entity.get(candidate.entity_id, [])]
        records.append(
            AnnotationRecord(
                record_key(namespace.namespace_id, candidate.entity_id),
                namespace.namespace_id,
                validate_identifier(candidate.entity_id, "candidate.entity_id"),
                snapshot,
                relation,
                candidate.rank,
                candidate.score,
                candidate.score_kind,
                validate_identifier(candidate.matched_alias_id, "matched_alias_id"),
                tuple(validate_identifier(a, "alias_id") for a in aliases),
            )
        )
    annotation = Annotation(
        ANNOTATION_SCHEMA,
        annotation_id_for(workspace_id, view.case_id),
        workspace_id,
        view.case_id,
        form.submission_id,
        view.header.origin,
        form.outcome,
        form.candidate_entity_id,
        form.reason,
        form.actor,
        view.review.submitted_at,
        view.header.created_at,
        view.header.workflow.version,
        view.header.workflow.config_sha256,
        view.packet_sha256,
        view.final_receipt.sequence,
        view.final_receipt.event_sha256,
        retrieval.retriever.kind,
        retrieval.retriever.name,
        retrieval.retriever.version,
        namespace.namespace_id,
        snapshot,
        tuple(records),
    )
    # Round-trip through the strict parser so stored payloads are always valid.
    return Annotation.from_dict(annotation.to_dict())


@dataclass(frozen=True)
class PublicationReport(_Contract):
    """Counts from one ``enqueue``/``drain`` pass; nothing here is a review outcome."""

    workspace_id: str
    scanned: int
    not_completed: int
    enqueued: int
    already_enqueued: int
    published: int
    pending: int
    tombstoned: int
    quarantined: tuple[dict[str, str], ...]
    blocked: int = 0


class Publisher:
    """Publishes completed cases of one registered v2 workspace into one store."""

    def __init__(self, store: MemoryStore, service: Any, workspace_id: str):
        self._store = store
        self._service = service
        self._workspace_id = validate_identifier(workspace_id, "workspace_id")

    # -- step 0: read completed cases through the review service ------------------

    def _completed_views(
        self, case_ids: Iterable[str] | None
    ) -> tuple[int, int, list[tuple[str, CaseView | None, str | None]]]:
        wanted = None if case_ids is None else set(case_ids)
        page = self._service.config.operations.page_max
        scanned = not_completed = 0
        result: list[tuple[str, CaseView | None, str | None]] = []
        offset = 0
        while True:
            summaries = self._service.list_cases(limit=page, offset=offset)
            if not summaries:
                break
            offset += len(summaries)
            for summary in summaries:
                if wanted is not None and summary.case_id not in wanted:
                    continue
                scanned += 1
                if summary.status != "completed":
                    not_completed += 1
                    continue
                verification = self._service.verify_audit(summary.case_id)
                if not verification.ok:
                    result.append((summary.case_id, None, "audit_verification_failed"))
                    continue
                result.append(
                    (summary.case_id, self._service.get_case(summary.case_id), None)
                )
        return scanned, not_completed, result

    # -- step 1: outbox --------------------------------------------------------------

    def _quarantine(
        self, mdb: MemoryDb, case_id: str, reason: str, detail: object
    ) -> dict[str, str]:
        return quarantine(mdb, case_id, self._workspace_id, reason, detail)

    def enqueue(self, case_ids: Iterable[str] | None = None) -> dict[str, Any]:
        """Record one publication intent per completed case (idempotent)."""
        scanned, not_completed, views = self._completed_views(case_ids)
        self._store.register_source_workspace(self._workspace_id)
        built: list[tuple[str, Annotation | None, str | None, object]] = []
        for case_id, view, problem in views:
            if problem is not None:
                built.append((case_id, None, problem, problem))
                continue
            try:
                built.append(
                    (
                        case_id,
                        annotation_from_case(
                            view, self._workspace_id, self._store.registry
                        ),
                        None,
                        None,
                    )
                )
            except CaseMemoryError as exc:
                built.append((case_id, None, exc.code, exc.to_dict()))
            except ReviewError as exc:
                built.append((case_id, None, "invalid_annotation", exc.to_dict()))
        counts = {"enqueued": 0, "already": 0, "tombstoned": 0, "quarantined": []}
        with self._store.session(write=True) as mdb:
            for case_id, annotation, problem, detail in built:
                if mdb.row("SELECT 1 FROM tombstones WHERE case_id=?", (case_id,)):
                    counts["tombstoned"] += 1
                    continue
                if annotation is None:
                    counts["quarantined"].append(
                        self._quarantine(mdb, case_id, problem, detail)
                    )
                    continue
                payload = annotation.to_dict()
                sha = digest(payload)
                existing = mdb.row(
                    "SELECT source_workspace_id, payload_sha256 FROM publication_intents "
                    "WHERE case_id=?",
                    (case_id,),
                )
                if existing is not None:
                    if existing["payload_sha256"] == sha:
                        counts["already"] += 1
                        continue
                    reason = (
                        "case_published_from_other_workspace"
                        if existing["source_workspace_id"] != self._workspace_id
                        else "payload_conflict"
                    )
                    counts["quarantined"].append(
                        self._quarantine(mdb, case_id, reason, {"payload_sha256": sha})
                    )
                    continue
                if _submission_in_use(mdb, annotation.submission_id, case_id):
                    counts["quarantined"].append(
                        self._quarantine(
                            mdb,
                            case_id,
                            "duplicate_submission_id",
                            {"submission_id": annotation.submission_id},
                        )
                    )
                    continue
                with mdb.transaction():
                    mdb.db.execute(
                        "INSERT INTO publication_intents VALUES (?,?,?,?,?,?,?)",
                        (
                            case_id,
                            self._workspace_id,
                            annotation.annotation_id,
                            annotation.final_event_sha256,
                            canonical(payload),
                            sha,
                            mdb.clock(),
                        ),
                    )
                counts["enqueued"] += 1
                self._store.failpoint("after_enqueue")
        return {"scanned": scanned, "not_completed": not_completed, **counts}

    # -- step 2: publish ---------------------------------------------------------------

    def drain(self) -> dict[str, Any]:
        """Publish every pending intent; each publication is one atomic transaction.

        An intent that cannot be published (``publication_conflict`` or an
        integrity failure) is rolled back, quarantined as ``publication_blocked``
        in its own transaction and skipped, so later intents still publish.
        """
        published = 0
        newly_blocked: list[dict[str, str]] = []
        with self._store.session(write=True) as mdb:
            pending = mdb.rows(
                f"SELECT * FROM publication_intents i WHERE {PENDING_INTENTS_WHERE} "
                "ORDER BY enqueued_at, case_id"
            )
            for intent in pending:
                try:
                    _publish_intent(mdb, intent, self._store.failpoint)
                except (PublicationConflictError, MemoryIntegrityError) as exc:
                    newly_blocked.append(
                        quarantine(
                            mdb,
                            intent["case_id"],
                            intent["source_workspace_id"],
                            BLOCKED_REASON,
                            exc.to_dict(),
                        )
                    )
                    continue
                published += 1
                self._store.failpoint("after_publish_commit")
            remaining = mdb.row(
                "SELECT COUNT(*) AS n FROM publication_intents i "
                f"WHERE {PENDING_INTENTS_WHERE}"
            )["n"]
            blocked = blocked_intent_count(mdb)
        return {
            "published": published,
            "pending": remaining,
            "blocked": blocked,
            "newly_blocked": newly_blocked,
        }

    def publish(self, case_ids: Iterable[str] | None = None) -> PublicationReport:
        """``enqueue`` then ``drain``; safe to repeat after any crash."""
        enq = self.enqueue(case_ids)
        drained = self.drain()
        return PublicationReport(
            self._workspace_id,
            enq["scanned"],
            enq["not_completed"],
            enq["enqueued"],
            enq["already"],
            drained["published"],
            drained["pending"],
            enq["tombstoned"],
            tuple(enq["quarantined"]) + tuple(drained["newly_blocked"]),
            drained["blocked"],
        )

    def receipt(self, case_id: str) -> dict[str, Any] | None:
        """The logical publication receipt of one case, or None while unpublished."""
        with self._store.session(write=False) as mdb:
            return publication_receipt(mdb, case_id)


def quarantine(
    mdb: MemoryDb, case_id: str, workspace_id: str, reason: str, detail: object
) -> dict[str, str]:
    """Record one quarantine row and its chained event (idempotent, own transaction)."""
    detail_sha = digest(detail)
    with mdb.transaction():
        cursor = mdb.db.execute(
            "INSERT OR IGNORE INTO quarantine VALUES (?,?,?,?,?)",
            (case_id, workspace_id, reason, detail_sha, mdb.clock()),
        )
        if cursor.rowcount:
            mdb.append_event(
                "publication_quarantined",
                case_id,
                {
                    "case_id": case_id,
                    "source_workspace_id": workspace_id,
                    "reason_code": reason,
                    "detail_sha256": detail_sha,
                },
            )
    return {"case_id": case_id, "reason_code": reason}


def _submission_in_use(mdb: MemoryDb, submission_id: str, case_id: str) -> bool:
    """True when another case already holds this submission ID (intent or annotation)."""
    if mdb.row(
        "SELECT 1 FROM annotations WHERE submission_id=? AND case_id != ?",
        (submission_id, case_id),
    ):
        return True
    return (
        mdb.row(
            "SELECT 1 FROM publication_intents WHERE case_id != ? AND "
            "json_extract(payload_json, '$.submission_id') = ?",
            (case_id, submission_id),
        )
        is not None
    )


def publication_receipt(mdb: MemoryDb, case_id: str) -> dict[str, Any] | None:
    row = mdb.row(
        "SELECT a.annotation_id, a.payload_sha256, a.published_sequence, e.event_sha256 "
        "FROM annotations a JOIN memory_events e ON e.sequence = a.published_sequence "
        "WHERE a.case_id=?",
        (case_id,),
    )
    return None if row is None else {"case_id": case_id, **row}


def _publish_intent(mdb: MemoryDb, intent: dict[str, Any], failpoint: Any) -> None:
    value = json.loads(intent["payload_json"])
    if digest(value) != intent["payload_sha256"]:
        raise MemoryIntegrityError(
            "publication intent checksum mismatch",
            details={"case_id": intent["case_id"]},
        )
    annotation = Annotation.from_dict(value)
    if annotation.annotation_id != intent["annotation_id"]:
        raise MemoryIntegrityError("publication intent identifier mismatch")
    keys = [(r.record_key, r.snapshot_sha256) for r in annotation.records]
    with mdb.transaction():
        event = mdb.append_event(
            "annotation_published",
            annotation.annotation_id,
            {
                "annotation_id": annotation.annotation_id,
                "case_id": annotation.case_id,
                "source_workspace_id": annotation.source_workspace_id,
                "payload_sha256": intent["payload_sha256"],
                "final_event_sha256": annotation.final_event_sha256,
                "origin": annotation.origin,
                "outcome": annotation.outcome,
            },
            keys=keys,
        )
        try:
            mdb.db.execute(
                "INSERT INTO annotations VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    annotation.annotation_id,
                    annotation.source_workspace_id,
                    annotation.case_id,
                    annotation.submission_id,
                    annotation.origin,
                    annotation.outcome,
                    annotation.reviewed_at,
                    intent["payload_json"],
                    intent["payload_sha256"],
                    event["sequence"],
                ),
            )
        except Exception as exc:  # sqlite3.IntegrityError: duplicate submission etc.
            raise PublicationConflictError(
                f"annotation could not be published: {exc}",
                details={"case_id": annotation.case_id},
            ) from None
        mdb.db.executemany(
            "INSERT INTO annotation_records VALUES (?,?,?,?,?,?,?)",
            [
                (
                    annotation.annotation_id,
                    r.record_key,
                    r.namespace_id,
                    r.source_entity_id,
                    r.snapshot_sha256,
                    r.relation,
                    r.rank,
                )
                for r in annotation.records
            ],
        )
        failpoint("before_publish_commit")
