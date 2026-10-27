"""Corrections, supersession and retention deletion for reviewer memory.

* **Withdrawal** stops future display of an annotation; the original row stays.
* **Supersession** links an original annotation to a *replacement* annotation
  from a later linked case. The replacement must share at least one exact
  ``(record_key, snapshot)`` with the original and have the same origin, so a
  correction can never merge two different records or launder demo history
  into human history. The original is preserved for explicit inspection.
* **Retention** deletes expired memory content (annotation payload, record
  rows, outbox payload and corrections) in one transaction, leaving a minimal
  tombstone (IDs, digests, dates) and a chained ``annotation_deleted`` event.
  ``plan_retention`` is a dry run (it may forecast a future ``now``);
  ``apply_retention`` re-plans under the lock, refuses a stale plan and refuses
  a plan dated later than the store clock (plus ``CLOCK_SKEW_SECONDS``), so a
  forecast can never delete records that have not yet expired.

Expiry from the 90-day visibility window is **not** deletion. Deleting from
memory does not delete other copies (the append-only v2 ledger and its
checkpoints, packets of later cases that froze the card, backups, exports); the
deletion manifest lists them explicitly instead of claiming erasure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import json
from typing import Any

from watchlist.case_memory.identifiers import validate_identifier
from watchlist.case_memory.state import (
    CorrectionError,
    MemoryInputError,
    NotFoundMemoryError,
    RetentionError,
    days,
    iso,
    parse_time,
)
from watchlist.case_memory.store import MemoryDb, MemoryStore, verify_events
from watchlist.review.contracts import ReviewError, _Contract, canonical, digest, text
from watchlist.review.validation import visible_text

ACTOR_MAX = 120
# Tolerated difference between a plan's ``planned_at`` and the applying clock.
CLOCK_SKEW_SECONDS = 300
REASON_MAX = 1000
DELETION_MANIFEST_SCHEMA = "memory-deletion-manifest-v1"
COPIES_NOT_DELETED = (
    "v2 review ledger (review.sqlite): case header, packet, submission and audit "
    "events are append-only and are not deleted by memory retention",
    "LangGraph checkpoints inside review.sqlite for the same cases",
    "packets of later cases that froze this annotation as a memory card",
    "backup sets created before this deletion (expire under the backup policy)",
    "anchor files, exports, traces, labels or trained derivatives made outside this store",
)


def _label(value: object, label: str, limit: int, *, lines: bool = False) -> str:
    try:
        value = text(value, label, limit=limit)
        return visible_text(value, label, allow_format=lines, allow_lines=lines)
    except ReviewError as exc:
        raise MemoryInputError(exc.message) from None


def _require_active(mdb: MemoryDb, annotation_id: str) -> Any:
    annotation = mdb.annotation(annotation_id)
    if annotation is None:
        tomb = mdb.row(
            "SELECT 1 FROM tombstones WHERE annotation_id=?", (annotation_id,)
        )
        raise NotFoundMemoryError(
            "annotation deleted by retention" if tomb else "unknown annotation",
            details={"annotation_id": annotation_id},
        )
    return annotation


def _correct(
    store: MemoryStore,
    kind: str,
    original_id: str,
    replacement_id: str | None,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    original_id = validate_identifier(original_id, "annotation_id")
    if replacement_id is not None:
        replacement_id = validate_identifier(
            replacement_id, "replacement_annotation_id"
        )
    actor = _label(actor, "actor", ACTOR_MAX)
    reason = _label(reason, "reason", REASON_MAX, lines=True)
    correction_id = digest(
        ["case-memory-correction", kind, original_id, replacement_id]
    )
    with store.session(write=True) as mdb:
        existing = mdb.row(
            "SELECT * FROM corrections WHERE original_annotation_id=?", (original_id,)
        )
        if existing is not None:
            same = (
                existing["kind"],
                existing["replacement_annotation_id"],
                existing["actor"],
                existing["reason"],
            ) == (kind, replacement_id, actor, reason)
            if same:
                return existing
            raise CorrectionError(
                "annotation already has a different correction",
                details={"annotation_id": original_id, "existing": existing["kind"]},
            )
        original = _require_active(mdb, original_id)
        keys = {(r.record_key, r.snapshot_sha256) for r in original.records}
        if replacement_id is not None:
            if replacement_id == original_id:
                raise CorrectionError("an annotation cannot supersede itself")
            replacement = _require_active(mdb, replacement_id)
            if mdb.row(
                "SELECT 1 FROM corrections WHERE original_annotation_id=?",
                (replacement_id,),
            ):
                raise CorrectionError("replacement annotation is itself corrected")
            shared = keys & {
                (r.record_key, r.snapshot_sha256) for r in replacement.records
            }
            if not shared:
                raise CorrectionError(
                    "replacement shares no exact record ID and snapshot with the "
                    "original; corrections never link different records",
                    details={"original": original_id, "replacement": replacement_id},
                )
            if replacement.origin != original.origin:
                raise CorrectionError(
                    "replacement origin must equal the original origin",
                    details={
                        "original": original.origin,
                        "replacement": replacement.origin,
                    },
                )
            if parse_time(replacement.reviewed_at, "replacement") < parse_time(
                original.reviewed_at, "original"
            ):
                raise CorrectionError("replacement must be reviewed after the original")
        with mdb.transaction():
            event = mdb.append_event(
                "annotation_withdrawn"
                if kind == "withdrawal"
                else "annotation_superseded",
                original_id,
                {
                    "correction_id": correction_id,
                    "kind": kind,
                    "original_annotation_id": original_id,
                    "replacement_annotation_id": replacement_id,
                    "reason_sha256": digest(reason),
                },
                keys=sorted(keys),
            )
            mdb.db.execute(
                "INSERT INTO corrections VALUES (?,?,?,?,?,?,?,?)",
                (
                    correction_id,
                    kind,
                    original_id,
                    replacement_id,
                    actor,
                    reason,
                    event["created_at"],
                    event["sequence"],
                ),
            )
        return mdb.row(
            "SELECT * FROM corrections WHERE correction_id=?", (correction_id,)
        )


def withdraw(
    store: MemoryStore, annotation_id: str, *, actor: str, reason: str
) -> dict[str, Any]:
    """Append a withdrawal: future panels hide the annotation; history keeps it."""
    return _correct(store, "withdrawal", annotation_id, None, actor, reason)


def supersede(
    store: MemoryStore,
    original_id: str,
    replacement_id: str,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Link an original annotation to a replacement from a later linked case."""
    return _correct(store, "supersession", original_id, replacement_id, actor, reason)


# ---------------------------------------------------------------------------
# Retention.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionPlan(_Contract):
    """Dry-run result: annotations whose retention period has ended at ``planned_at``."""

    planned_at: str
    cutoff: str
    annotation_retention_days: int
    candidates: tuple[dict[str, Any], ...]

    @property
    def plan_id(self) -> str:
        return digest(self.to_dict())


def _plan(mdb: MemoryDb, now: str, retention_days: int) -> RetentionPlan:
    moment = parse_time(now, "now")
    cutoff = moment - days(retention_days)
    expired = []
    for row in mdb.rows(
        "SELECT annotation_id, case_id, reviewed_at, payload_sha256 FROM annotations "
        "ORDER BY reviewed_at, annotation_id"
    ):
        if parse_time(row["reviewed_at"], "reviewed_at") <= cutoff:
            expired.append(row)
    return RetentionPlan(iso(moment), iso(cutoff), retention_days, tuple(expired))


def plan_retention(store: MemoryStore, *, now: str | None = None) -> RetentionPlan:
    """List expired memory annotations without changing anything (dry run).

    ``now`` may be a future time to *forecast* deletions; such a plan cannot be
    applied until the store clock reaches it (see ``apply_retention``).
    """
    with store.session(write=False) as mdb:
        return _plan(
            mdb, now or store.now(), store.config.retention.annotation_retention_days
        )


@dataclass(frozen=True)
class DeletionManifest(_Contract):
    """Verified record of one applied retention deletion (no notes or names)."""

    manifest_schema: str
    plan_id: str
    applied_at: str
    actor: str
    reason: str
    cutoff: str
    deleted: tuple[dict[str, Any], ...]
    rows_deleted: dict[str, int]
    verified: bool
    issues: tuple[str, ...]
    copies_not_deleted: tuple[str, ...]


def refuse_future_plan(planned_at: str, now: str) -> None:
    """Raise ``retention_invalid`` when a plan is dated after ``now`` + skew.

    Applying a forecast would delete annotations whose retention period has
    not ended; only plans dated at or before the applying clock are accepted.
    """
    planned = parse_time(planned_at, "planned_at")
    current = parse_time(now, "now")
    if planned > current + timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise RetentionError(
            "retention plan is dated in the future; a forecast cannot be applied",
            details={"planned_at": iso(planned), "now": iso(current)},
        )


def apply_retention(
    store: MemoryStore, plan: RetentionPlan, *, actor: str, reason: str
) -> DeletionManifest:
    """Delete exactly the planned annotations, leave tombstones, then verify.

    All deletions commit in one transaction with their events; a crash before
    commit deletes nothing. The plan is recomputed under the lock at its own
    ``planned_at`` time and must be identical, so a stale plan is refused. A
    plan dated later than the store clock (beyond ``CLOCK_SKEW_SECONDS``) is
    refused, so a future ``now`` can never delete unexpired annotations.
    """
    actor = _label(actor, "actor", ACTOR_MAX)
    reason = _label(reason, "reason", REASON_MAX, lines=True)
    refuse_future_plan(plan.planned_at, store.now())
    tables = ("corrections", "annotation_records", "annotations", "publication_intents")
    rows_deleted = dict.fromkeys(tables, 0)
    deleted: list[dict[str, Any]] = []
    with store.session(write=True) as mdb:
        current = _plan(mdb, plan.planned_at, plan.annotation_retention_days)
        if current != plan:
            raise RetentionError(
                "retention plan is stale; run plan_retention again",
                details={"plan_id": plan.plan_id, "current_plan_id": current.plan_id},
            )
        if (
            plan.annotation_retention_days
            != store.config.retention.annotation_retention_days
        ):
            raise RetentionError(
                "plan uses a different retention policy than the config"
            )
        policy = {
            "annotation_retention_days": plan.annotation_retention_days,
            "cutoff": plan.cutoff,
        }
        with mdb.transaction():
            for candidate in plan.candidates:
                annotation = _require_active(mdb, candidate["annotation_id"])
                keys = sorted(
                    {(r.record_key, r.snapshot_sha256) for r in annotation.records}
                )
                event = mdb.append_event(
                    "annotation_deleted",
                    annotation.annotation_id,
                    {
                        "annotation_id": annotation.annotation_id,
                        "case_id": annotation.case_id,
                        "payload_sha256": candidate["payload_sha256"],
                        "reviewed_at": annotation.reviewed_at,
                        "policy": policy,
                    },
                    keys=keys,
                )
                mdb.db.execute(
                    "INSERT INTO tombstones VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        annotation.annotation_id,
                        annotation.source_workspace_id,
                        annotation.case_id,
                        candidate["payload_sha256"],
                        canonical([list(k) for k in keys]),
                        annotation.reviewed_at,
                        event["created_at"],
                        canonical(policy),
                        event["sequence"],
                    ),
                )
                for table, sql in (
                    (
                        "corrections",
                        "DELETE FROM corrections WHERE original_annotation_id=? OR "
                        "replacement_annotation_id=?",
                    ),
                    (
                        "annotation_records",
                        "DELETE FROM annotation_records WHERE annotation_id=?",
                    ),
                    ("annotations", "DELETE FROM annotations WHERE annotation_id=?"),
                    (
                        "publication_intents",
                        "DELETE FROM publication_intents WHERE annotation_id=?",
                    ),
                ):
                    args = (annotation.annotation_id,) * sql.count("?")
                    rows_deleted[table] += mdb.db.execute(sql, args).rowcount
                deleted.append(
                    {
                        "annotation_id": annotation.annotation_id,
                        "case_id": annotation.case_id,
                        "payload_sha256": candidate["payload_sha256"],
                        "record_keys": [list(k) for k in keys],
                    }
                )
            mdb.append_event(
                "retention_applied",
                plan.plan_id,
                {
                    "plan_id": plan.plan_id,
                    "deleted_count": len(deleted),
                    "cutoff": plan.cutoff,
                    "reason_sha256": digest(reason),
                },
            )
            store.failpoint("during_retention")
        applied_at = mdb.clock()
        issues = _verify_deletion(mdb, deleted)
    return DeletionManifest(
        DELETION_MANIFEST_SCHEMA,
        plan.plan_id,
        applied_at,
        actor,
        reason,
        plan.cutoff,
        tuple(deleted),
        rows_deleted,
        not issues,
        tuple(issues),
        COPIES_NOT_DELETED,
    )


def _verify_deletion(mdb: MemoryDb, deleted: list[dict[str, Any]]) -> list[str]:
    """Post-commit check: content rows are gone, tombstones and events remain."""
    issues = []
    for item in deleted:
        annotation_id = item["annotation_id"]
        for table, column in (
            ("annotations", "annotation_id"),
            ("annotation_records", "annotation_id"),
            ("publication_intents", "annotation_id"),
            ("corrections", "original_annotation_id"),
            ("corrections", "replacement_annotation_id"),
        ):
            if mdb.row(f"SELECT 1 FROM {table} WHERE {column}=?", (annotation_id,)):
                issues.append(f"{table} still holds {annotation_id[:12]}")
        if not mdb.row(
            "SELECT 1 FROM tombstones WHERE annotation_id=?", (annotation_id,)
        ):
            issues.append(f"tombstone missing for {annotation_id[:12]}")
        for row in mdb.rows(
            "SELECT payload_json FROM memory_events WHERE event_type='annotation_published' "
            "AND target_id=?",
            (annotation_id,),
        ):
            if "reason" in json.loads(row["payload_json"]):
                issues.append("publication event holds free text")
    _, _, chain = verify_events(mdb.events())
    issues.extend(chain)
    return issues
