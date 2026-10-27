"""Exact-ID lookup of prior dated annotations, explicit inspection and freshness.

Every query is an indexed equality search on ``(record_key, snapshot_sha256)``;
there is no name, alias, query-text or similarity search anywhere in this
module. Results keep differing outcomes side by side: there is no majority
vote, newest-wins rule or confidence boost.

Default panel (``lookup_keys``): per candidate, eligible annotations sorted by
``reviewed_at`` descending then ``annotation_id``, capped at
``policy.cards_per_candidate``, with explicit omission counts. Eligible means:
published, not withdrawn or superseded, origin displayable for the new case's
origin, reviewed no later than the lookup time and younger than the
visibility window (``now >= reviewed_at + visibility_days`` is expired).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any, Sequence

from watchlist.case_memory.identifiers import RecordRef, validate_identifier
from watchlist.case_memory.state import (
    CONTINUITY_NOTICE,
    MEMORY_NOTICE,
    Annotation,
    MemoryInputError,
    MemoryIntegrityError,
    MemoryPolicy,
    NotFoundMemoryError,
    days,
    iso,
    parse_time,
)
from watchlist.case_memory.store import (
    PENDING_INTENTS_WHERE,
    MemoryDb,
    MemoryStore,
    blocked_intent_count,
)
from watchlist.review.contracts import _Contract, digest

INTERPRETATION = {
    "selected_for_follow_up": (
        "This record was selected for further follow-up in that earlier case; this "
        "does not verify that the earlier or the new query identifies it."
    ),
    "considered_without_selection": (
        "This record was displayed in that earlier case but not selected; this is not "
        "a verified negative and not a clearance."
    ),
    "unresolved_context": (
        "The earlier review remained unresolved; the record was neither accepted nor "
        "rejected."
    ),
}
OMISSION_REASONS = (
    "withdrawn",
    "superseded",
    "expired",
    "origin_not_displayable",
    "after_lookup_time",
    "current_case",
    "over_cap",
)


def _corrections(
    mdb: MemoryDb, annotation_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    if not annotation_ids:
        return {}
    marks = ",".join("?" * len(annotation_ids))
    return {
        r["original_annotation_id"]: r
        for r in mdb.rows(
            f"SELECT * FROM corrections WHERE original_annotation_id IN ({marks})",
            tuple(annotation_ids),
        )
    }


def _supersedes(mdb: MemoryDb, annotation_ids: Sequence[str]) -> dict[str, str]:
    if not annotation_ids:
        return {}
    marks = ",".join("?" * len(annotation_ids))
    return {
        r["replacement_annotation_id"]: r["original_annotation_id"]
        for r in mdb.rows(
            "SELECT original_annotation_id, replacement_annotation_id FROM corrections "
            f"WHERE replacement_annotation_id IN ({marks})",
            tuple(annotation_ids),
        )
    }


def _key_rows(mdb: MemoryDb, ref: RecordRef) -> list[tuple[Annotation, dict[str, Any]]]:
    """All published annotations recorded for one exact key, verified row by row."""
    result = []
    for row in mdb.rows(
        "SELECT r.*, a.payload_json, a.payload_sha256 FROM annotation_records r "
        "JOIN annotations a ON a.annotation_id = r.annotation_id "
        "WHERE r.record_key=? AND r.snapshot_sha256=? ORDER BY r.annotation_id",
        (ref.record_key, ref.snapshot_sha256),
    ):
        # Defence in depth: the indexed key must agree with the exact stored IDs.
        if (row["namespace_id"], row["source_entity_id"]) != (
            ref.namespace_id,
            ref.source_entity_id,
        ):
            raise MemoryIntegrityError(
                "memory record key does not match its stored identifiers",
                details={"annotation_id": row["annotation_id"]},
            )
        value = json.loads(row["payload_json"])
        if digest(value) != row["payload_sha256"]:
            raise MemoryIntegrityError(
                "saved annotation checksum mismatch",
                details={"annotation_id": row["annotation_id"]},
            )
        annotation = Annotation.from_dict(value)
        record = next(
            (
                r
                for r in annotation.records
                if r.record_key == ref.record_key
                and r.snapshot_sha256 == ref.snapshot_sha256
            ),
            None,
        )
        if record is None or record.source_entity_id != ref.source_entity_id:
            raise MemoryIntegrityError(
                "annotation payload lacks the indexed record",
                details={"annotation_id": row["annotation_id"]},
            )
        result.append((annotation, record.to_dict()))
    return result


def _status(
    annotation: Annotation,
    corrections: dict[str, dict[str, Any]],
    *,
    policy: MemoryPolicy,
    case_origin: str | None,
    now: datetime,
    exclude_case_id: str | None,
) -> str:
    """``eligible`` or the first omission reason (fixed precedence)."""
    correction = corrections.get(annotation.annotation_id)
    if correction is not None:
        return "withdrawn" if correction["kind"] == "withdrawal" else "superseded"
    reviewed = parse_time(annotation.reviewed_at, "reviewed_at")
    if exclude_case_id is not None and annotation.case_id == exclude_case_id:
        return "current_case"
    if reviewed > now:
        return "after_lookup_time"
    if now >= reviewed + days(policy.visibility_days):
        return "expired"
    if case_origin is not None and annotation.origin not in policy.display_origins.get(
        case_origin, ()
    ):
        return "origin_not_displayable"
    return "eligible"


def _card(
    annotation: Annotation,
    record: dict[str, Any],
    *,
    status: str,
    supersedes: str | None,
    correction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    card = {
        "annotation_id": annotation.annotation_id,
        "status": status,
        "source_workspace_id": annotation.source_workspace_id,
        "case_id": annotation.case_id,
        "case_created_at": annotation.case_created_at,
        "reviewed_at": annotation.reviewed_at,
        "origin": annotation.origin,
        "outcome": annotation.outcome,
        "relation": record["relation"],
        "interpretation": INTERPRETATION[record["relation"]],
        "reviewer_opinion": annotation.reason,
        "actor_label": annotation.actor,
        "workflow_version": annotation.workflow_version,
        "retriever": {
            "kind": annotation.retriever_kind,
            "name": annotation.retriever_name,
            "version": annotation.retriever_version,
        },
        "earlier_rank": record["rank"],
        "earlier_score": record["score"],
        "earlier_score_kind": record["score_kind"],
        "earlier_matched_alias_id": record["matched_alias_id"],
        "packet_sha256": annotation.packet_sha256,
        "final_event_sha256": annotation.final_event_sha256,
        "annotation_sha256": annotation.sha256,
        "supersedes_annotation_id": supersedes,
    }
    if correction is not None:
        card["correction"] = {
            "kind": correction["kind"],
            "replacement_annotation_id": correction["replacement_annotation_id"],
            "reason": correction["reason"],
            "actor_label": correction["actor"],
            "created_at": correction["created_at"],
        }
    return card


def lookup_keys(
    mdb: MemoryDb,
    refs: Sequence[RecordRef],
    *,
    policy: MemoryPolicy,
    case_origin: str,
    now: datetime,
    exclude_case_id: str | None = None,
) -> list[dict[str, Any]]:
    """Default-panel history for each exact key, in the given (ranking) order."""
    revisions = mdb.key_revisions([(r.record_key, r.snapshot_sha256) for r in refs])
    result = []
    for ref in refs:
        rows = _key_rows(mdb, ref)
        ids = [a.annotation_id for a, _ in rows]
        corrections = _corrections(mdb, ids)
        supersedes = _supersedes(mdb, ids)
        omitted = dict.fromkeys(OMISSION_REASONS, 0)
        eligible = []
        for annotation, record in rows:
            status = _status(
                annotation,
                corrections,
                policy=policy,
                case_origin=case_origin,
                now=now,
                exclude_case_id=exclude_case_id,
            )
            if status == "eligible":
                eligible.append((annotation, record))
            else:
                omitted[status] += 1
        eligible.sort(
            key=lambda item: (
                -parse_time(item[0].reviewed_at, "reviewed_at").timestamp(),
                item[0].annotation_id,
            )
        )
        shown = eligible[: policy.cards_per_candidate]
        omitted["over_cap"] = len(eligible) - len(shown)
        outcomes = sorted({a.outcome for a, _ in eligible})
        result.append(
            {
                **ref.to_dict(),
                "revision": revisions[(ref.record_key, ref.snapshot_sha256)],
                "eligible_count": len(eligible),
                "shown_count": len(shown),
                "omitted": omitted,
                "eligible_outcomes": outcomes,
                "differing_outcomes": len(outcomes) > 1,
                "cards": [
                    _card(
                        a,
                        r,
                        status="eligible",
                        supersedes=supersedes.get(a.annotation_id),
                    )
                    for a, r in shown
                ],
            }
        )
    return result


def build_context(
    mdb: MemoryDb,
    refs: Sequence[RecordRef],
    *,
    policy: MemoryPolicy,
    case_origin: str,
    now: str,
    namespace_id: str,
    snapshot_sha256: str,
    exclude_case_id: str | None = None,
) -> dict[str, Any]:
    """The JSON object frozen into ``packet.context['case_memory']['data']``."""
    sequence, head = mdb.head()
    pending = mdb.row(
        f"SELECT COUNT(*) AS n FROM publication_intents i WHERE {PENDING_INTENTS_WHERE}"
    )["n"]
    blocked = blocked_intent_count(mdb)
    warnings = []
    if pending:
        warnings.append(
            f"{pending} completed review(s) are not yet published to memory"
        )
    if blocked:
        warnings.append(
            f"{blocked} completed review(s) could not be published to memory "
            "(quarantined; an operator must resolve them)"
        )
    moment = parse_time(now, "lookup time")
    return {
        "status": "ok",
        "notice": MEMORY_NOTICE,
        "lookup_at": iso(moment),
        "policy_sha256": policy.sha256,
        "memory_head": {"sequence": sequence, "event_sha256": head},
        "namespace_id": namespace_id,
        "snapshot_sha256": snapshot_sha256,
        "case_origin": case_origin,
        "displayable_origins": list(policy.display_origins.get(case_origin, ())),
        "visibility_days": policy.visibility_days,
        "pending_publications": pending,
        "blocked_publications": blocked,
        "warnings": warnings,
        "keys": lookup_keys(
            mdb,
            refs,
            policy=policy,
            case_origin=case_origin,
            now=moment,
            exclude_case_id=exclude_case_id,
        ),
    }


# ---------------------------------------------------------------------------
# Explicit inspection (never part of the default panel).
# ---------------------------------------------------------------------------


def inspect_history(
    store: MemoryStore,
    ref: RecordRef,
    *,
    now: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Every annotation for one exact key, with status labels, paginated.

    Also counts annotations for the same namespaced ID in *other* snapshot
    fingerprints, labelled "identifier continuity not established"; those are
    listed for explicit inspection only.
    """
    policy = store.config.policy
    limit = policy.page_max if limit is None else limit
    if type(limit) is not int or not 1 <= limit <= policy.page_max:
        raise MemoryInputError(f"limit must be from 1 to {policy.page_max}")
    if type(offset) is not int or offset < 0:
        raise MemoryInputError("offset must be a nonnegative integer")
    moment = parse_time(now or store.now(), "now")
    with store.session(write=False) as mdb:
        rows = _key_rows(mdb, ref)
        ids = [a.annotation_id for a, _ in rows]
        corrections = _corrections(mdb, ids)
        supersedes = _supersedes(mdb, ids)
        rows.sort(
            key=lambda item: (
                -parse_time(item[0].reviewed_at, "reviewed_at").timestamp(),
                item[0].annotation_id,
            )
        )
        page = rows[offset : offset + limit]
        others = mdb.rows(
            "SELECT snapshot_sha256, COUNT(*) AS n FROM annotation_records WHERE "
            "record_key=? AND snapshot_sha256 != ? GROUP BY snapshot_sha256 "
            "ORDER BY snapshot_sha256",
            (ref.record_key, ref.snapshot_sha256),
        )
        revision = mdb.key_revisions([(ref.record_key, ref.snapshot_sha256)])
    return {
        **ref.to_dict(),
        "notice": MEMORY_NOTICE,
        "revision": revision[(ref.record_key, ref.snapshot_sha256)],
        "total": len(rows),
        "offset": offset,
        "limit": limit,
        "entries": [
            _card(
                a,
                r,
                status=_status(
                    a,
                    corrections,
                    policy=policy,
                    case_origin=None,
                    now=moment,
                    exclude_case_id=None,
                ),
                supersedes=supersedes.get(a.annotation_id),
                correction=corrections.get(a.annotation_id),
            )
            for a, r in page
        ],
        "other_snapshots": {
            "notice": CONTINUITY_NOTICE,
            "snapshots": [
                {"snapshot_sha256": o["snapshot_sha256"], "annotation_count": o["n"]}
                for o in others
            ],
        },
    }


def get_annotation(store: MemoryStore, annotation_id: str) -> dict[str, Any]:
    """One annotation with its corrections, or a not-found / deleted-by-retention error."""
    annotation_id = validate_identifier(annotation_id, "annotation_id")
    with store.session(write=False) as mdb:
        annotation = mdb.annotation(annotation_id)
        if annotation is None:
            tomb = mdb.row(
                "SELECT annotation_id, case_id, deleted_at FROM tombstones "
                "WHERE annotation_id=?",
                (annotation_id,),
            )
            raise NotFoundMemoryError(
                "annotation deleted by retention" if tomb else "unknown annotation",
                details={"annotation_id": annotation_id, "tombstone": tomb},
            )
        corrections = mdb.rows(
            "SELECT * FROM corrections WHERE original_annotation_id=? OR "
            "replacement_annotation_id=? ORDER BY event_sequence",
            (annotation_id, annotation_id),
        )
    return {
        "notice": MEMORY_NOTICE,
        "annotation": annotation.to_dict(),
        "corrections": corrections,
    }


@dataclass(frozen=True)
class FreshnessReport(_Contract):
    """Whether a frozen memory context still matches current memory.

    ``changed_keys`` lists keys whose revision moved (including keys that had
    no history when the context was frozen); ``ineligible_cards`` lists shown
    annotations that are now withdrawn, superseded, expired or deleted.
    """

    fresh: bool
    checked_at: str
    changed_keys: tuple[str, ...]
    ineligible_cards: tuple[str, ...]


def check_freshness(
    store: MemoryStore, context: dict[str, Any], *, now: str | None = None
) -> FreshnessReport:
    """Inspect freshness; review acceptance uses the same check under both locks."""
    with store.session(write=False) as mdb:
        return check_freshness_in_session(
            mdb, context, store.config.policy, now=now or store.now()
        )


def check_freshness_in_session(
    mdb: MemoryDb, context: dict[str, Any], policy: MemoryPolicy, *, now: str
) -> FreshnessReport:
    """Check revisions and visibility with the caller holding the memory lock."""
    if context.get("status") != "ok":
        return FreshnessReport(True, iso(parse_time(now, "now")), (), ())
    moment = parse_time(now, "now")
    if context.get("policy_sha256") != policy.sha256:
        return FreshnessReport(
            False,
            iso(moment),
            tuple(k["source_entity_id"] for k in context["keys"]),
            (),
        )
    changed, ineligible = [], []
    pairs = [(k["record_key"], k["snapshot_sha256"]) for k in context["keys"]]
    current = mdb.key_revisions(pairs)
    for key in context["keys"]:
        pair = (key["record_key"], key["snapshot_sha256"])
        if current[pair] != key["revision"]:
            changed.append(key["source_entity_id"])
        ids = [card["annotation_id"] for card in key["cards"]]
        corrections = _corrections(mdb, ids)
        for card in key["cards"]:
            annotation = mdb.annotation(card["annotation_id"])
            if annotation is None:
                ineligible.append(card["annotation_id"])
                continue
            status = _status(
                annotation,
                corrections,
                policy=policy,
                case_origin=context.get("case_origin"),
                now=moment,
                exclude_case_id=None,
            )
            if status != "eligible":
                ineligible.append(card["annotation_id"])
    return FreshnessReport(
        not changed and not ineligible,
        iso(moment),
        tuple(changed),
        tuple(ineligible),
    )
