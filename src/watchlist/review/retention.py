"""Retention planning and deletion drills across review, memory and backup copies.

``plan`` is a dry run: it inventories every known content copy and lists what
the retention policy would delete. ``apply`` deletes only what a plan with the
same ``plan_id`` listed, writes a verified deletion manifest, and refuses stale
plans. Usage::

    python -m watchlist.review.retention plan --memory-config config/memory.json \
        --memory DIR [--backups DIR] [--workspace DIR --review-config config/review-v2.json]
    python -m watchlist.review.retention apply ... --now PLANNED_AT \
        --confirm-plan-id PLAN_ID --actor LABEL --reason TEXT --manifest NEW_FILE.json

``plan --now`` may forecast a future date. ``apply --now`` must repeat the
``planned_at`` of the plan being confirmed, and ``apply`` refuses any plan
dated later than the current clock (plus a small skew) with
``retention_invalid``, so a forecast can never delete unexpired records. The
deletion manifest file is created (exclusively) **before** anything is
deleted, so an unusable manifest path deletes nothing.

What is deleted, honestly:

* memory annotations past ``annotation_retention_days`` (tombstones + events
  remain; see ``watchlist.case_memory.lifecycle``);
* backup sets past ``backup_retention_days`` that carry a valid manifest and
  live directly under the given backups directory.

What is **not** deleted and is reported instead: v2 ledger cases (the ledger
is append-only; deleting its content needs a payload/receipt split owned by
the review service), LangGraph checkpoints, packets of later cases that froze a
memory card, anchors, exports, traces, labels and trained derivatives. File
deletion is ordinary filesystem removal, not secure erasure (SSDs, snapshots
and OS caches may retain bytes).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Callable, Sequence

from watchlist.review.backup import BackupError, read_manifest
from watchlist.review.contracts import ReviewError, _Contract, digest

RETENTION_SCHEMA = "review-retention-plan-v1"
DELETION_SCHEMA = "review-deletion-manifest-v1"

INVENTORY = (
    {
        "copy": "v2 review ledger (review.sqlite: cases, evidence, packets, submissions, audit)",
        "handling": "reported only; append-only, not deleted by this tool",
    },
    {
        "copy": "LangGraph checkpoints inside review.sqlite",
        "handling": "reported only; deleted together with the ledger generation, not per case",
    },
    {
        "copy": "memory store annotations, records, outbox payloads and corrections",
        "handling": "deleted after annotation_retention_days with tombstones",
    },
    {
        "copy": "frozen memory contexts inside later case packets",
        "handling": "reported only; part of the append-only ledger",
    },
    {
        "copy": "backup sets (backup-manifest.json directories)",
        "handling": "deleted after backup_retention_days",
    },
    {
        "copy": "anchor files, exports, traces, labels and trained derivatives",
        "handling": "outside this tool; owners must track and delete them",
    },
)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse(value: str, label: str):
    from watchlist.case_memory.state import parse_time

    return parse_time(value, label)


def _backup_sets(backups_root: Path) -> list[Path]:
    if backups_root.is_symlink() or not backups_root.is_dir():
        raise BackupError("backups directory is missing or a symlink")
    return sorted(
        p for p in backups_root.iterdir() if p.is_dir() and not p.is_symlink()
    )


@dataclass(frozen=True)
class RetentionReport(_Contract):
    """Dry-run plan over all copies; ``plan_id`` binds a later ``apply``."""

    report_schema: str
    planned_at: str
    policy: dict[str, int]
    memory_plan: dict[str, Any] | None
    expired_backups: tuple[dict[str, str], ...]
    unrecognized_backup_dirs: tuple[str, ...]
    ledger_cases_past_retention: tuple[str, ...]
    ledger_status: str
    inventory: tuple[dict[str, str], ...]

    @property
    def plan_id(self) -> str:
        return digest(self.to_dict())


def plan(
    retention: Any,
    *,
    memory: Any = None,
    backups_root: Path | str | None = None,
    service: Any = None,
    now: str | None = None,
) -> RetentionReport:
    """Inventory copies and list deletions due at ``now`` without changing anything.

    ``retention`` is a ``RetentionPolicy``; ``memory`` a ``MemoryStore``;
    ``service`` an optional read-only ``ReviewService`` whose old cases are
    reported (never deleted).
    """
    from watchlist.case_memory.lifecycle import plan_retention
    from watchlist.case_memory.state import days, iso

    moment = _parse(now or _now(), "now")
    memory_plan = None
    if memory is not None:
        memory_plan = plan_retention(memory, now=iso(moment)).to_dict()
    expired, unrecognized = [], []
    if backups_root is not None:
        for folder in _backup_sets(Path(os.path.abspath(Path(backups_root)))):
            try:
                manifest = read_manifest(folder)
                created = _parse(manifest.created_at, "backup created_at")
            except ReviewError:
                unrecognized.append(folder.name)
                continue
            if moment >= created + days(retention.backup_retention_days):
                expired.append(
                    {
                        "directory": folder.name,
                        "backup_id": manifest.backup_id,
                        "created_at": manifest.created_at,
                    }
                )
    ledger_cases: list[str] = []
    if service is not None:
        cutoff = moment - days(retention.annotation_retention_days)
        page, offset = service.config.operations.page_max, 0
        while True:
            summaries = service.list_cases(limit=page, offset=offset)
            if not summaries:
                break
            offset += len(summaries)
            ledger_cases.extend(
                s.case_id
                for s in summaries
                if _parse(s.created_at, "created_at") <= cutoff
            )
    return RetentionReport(
        RETENTION_SCHEMA,
        iso(moment),
        {
            "annotation_retention_days": retention.annotation_retention_days,
            "backup_retention_days": retention.backup_retention_days,
        },
        memory_plan,
        tuple(expired),
        tuple(unrecognized),
        tuple(ledger_cases),
        "blocked_append_only" if ledger_cases else "nothing_due",
        INVENTORY,
    )


def apply(
    report: RetentionReport,
    retention: Any,
    *,
    actor: str,
    reason: str,
    manifest_path: Path | str,
    memory: Any = None,
    backups_root: Path | str | None = None,
    service: Any = None,
    clock: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Apply a plan after re-planning at its ``planned_at`` time; write a manifest.

    Refuses (``retention_invalid``) a plan dated after ``clock()`` plus
    ``CLOCK_SKEW_SECONDS``; ``clock`` defaults to the memory store clock, or
    UTC wall time without a store. The manifest file is created exclusively
    before any deletion (an unusable path raises ``backup_failed`` and deletes
    nothing) and holds IDs, digests and verification results only (no notes or
    names). If a deletion step fails after the file was created, the manifest
    records the failure before the error is raised.
    """
    from watchlist.case_memory.lifecycle import refuse_future_plan

    now = (clock or (memory.now if memory is not None else _now))()
    refuse_future_plan(report.planned_at, now)
    current = plan(
        retention,
        memory=memory,
        backups_root=backups_root,
        service=service,
        now=report.planned_at,
    )
    if current.plan_id != report.plan_id:
        raise BackupError(
            "retention plan is stale; run plan again",
            details={"plan_id": report.plan_id, "current_plan_id": current.plan_id},
        )
    target = Path(manifest_path)
    if target.exists() or target.is_symlink():
        raise BackupError("deletion manifest path already exists")
    try:
        handle = target.open("x", encoding="utf-8")
    except OSError as exc:
        raise BackupError(
            f"cannot create the deletion manifest; nothing was deleted: {exc}",
            details={"manifest": str(target)},
        ) from None
    with handle:
        try:
            result = _apply_steps(report, memory, backups_root, actor, reason)
        except ReviewError as exc:
            failure = {
                "manifest_schema": DELETION_SCHEMA,
                "plan_id": report.plan_id,
                "planned_at": report.planned_at,
                "actor": actor,
                "verified": False,
                "failed": exc.to_dict(),
            }
            handle.write(json.dumps(failure, indent=2, sort_keys=True))
            raise
        handle.write(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return result


def _apply_steps(
    report: RetentionReport,
    memory: Any,
    backups_root: Path | str | None,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Delete the planned memory annotations and backup sets; build the manifest."""
    from watchlist.case_memory.lifecycle import RetentionPlan, apply_retention

    memory_manifest = None
    if report.memory_plan is not None and report.memory_plan["candidates"]:
        raw = report.memory_plan
        memory_manifest = apply_retention(
            memory,
            RetentionPlan(
                raw["planned_at"],
                raw["cutoff"],
                raw["annotation_retention_days"],
                tuple(raw["candidates"]),
            ),
            actor=actor,
            reason=reason,
        ).to_dict()
    removed, issues = [], []
    if report.expired_backups:
        root = Path(os.path.abspath(Path(backups_root)))
        for item in report.expired_backups:
            folder = root / item["directory"]
            if folder.parent != root or folder.is_symlink():
                issues.append(f"backup {item['directory']}: path escape refused")
                continue
            if read_manifest(folder).backup_id != item["backup_id"]:
                issues.append(
                    f"backup {item['directory']}: manifest changed; not deleted"
                )
                continue
            try:
                shutil.rmtree(folder)
            except OSError as exc:
                issues.append(f"backup {item['directory']}: deletion failed: {exc}")
            if folder.exists():
                issues.append(
                    f"backup {item['directory']}: still present after deletion"
                )
            else:
                removed.append(item)
    if memory_manifest is not None and not memory_manifest["verified"]:
        issues.extend(memory_manifest["issues"])
    return {
        "manifest_schema": DELETION_SCHEMA,
        "plan_id": report.plan_id,
        "planned_at": report.planned_at,
        "actor": actor,
        "memory": memory_manifest,
        "backups_deleted": removed,
        "ledger_cases_past_retention": list(report.ledger_cases_past_retention),
        "ledger_status": report.ledger_status,
        "verified": not issues,
        "issues": issues,
        "not_deleted": [
            i for i in INVENTORY if not i["handling"].startswith("deleted")
        ],
        "erasure_notice": (
            "Deletion removes rows and files through SQLite and the filesystem; it is "
            "not secure erasure of storage media, snapshots or caches."
        ),
    }


def _main(argv: Sequence[str] | None = None) -> int:
    from watchlist.case_memory.state import MemoryConfig
    from watchlist.case_memory.store import MemoryStore

    parser = argparse.ArgumentParser(prog="python -m watchlist.review.retention")
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--memory-config", required=True)
    parser.add_argument("--memory")
    parser.add_argument("--backups")
    parser.add_argument("--workspace")
    parser.add_argument("--review-config")
    parser.add_argument(
        "--now",
        help="plan: evaluation time (a future time is only a forecast); "
        "apply: the planned_at of the plan being confirmed (never in the future)",
    )
    parser.add_argument("--confirm-plan-id")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    parser.add_argument("--manifest")
    args = parser.parse_args(argv)
    try:
        config = MemoryConfig.load(args.memory_config)
        memory = (
            MemoryStore.open(args.memory, config, read_only=args.command == "plan")
            if args.memory
            else None
        )
        service = None
        if args.workspace:
            from watchlist.review.service import ReviewService

            if not args.review_config:
                parser.error("--review-config is required with --workspace")
            service = ReviewService.open(
                args.workspace, args.review_config, read_only=True
            )
        report = plan(
            config.retention,
            memory=memory,
            backups_root=args.backups,
            service=service,
            now=args.now,
        )
        if args.command == "plan":
            print(
                json.dumps(
                    {"plan_id": report.plan_id, **report.to_dict()},
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        if not (args.confirm_plan_id and args.actor and args.reason and args.manifest):
            parser.error(
                "apply needs --confirm-plan-id, --actor, --reason and --manifest"
            )
        if args.confirm_plan_id != report.plan_id:
            print(
                json.dumps(
                    {"error": "plan_id_mismatch", "current_plan_id": report.plan_id}
                )
            )
            return 2
        result = apply(
            report,
            config.retention,
            actor=args.actor,
            reason=args.reason,
            manifest_path=args.manifest,
            memory=memory,
            backups_root=args.backups,
            service=service,
        )
        print(json.dumps({"verified": result["verified"], "manifest": args.manifest}))
        return 0 if result["verified"] else 1
    except ReviewError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(_main())
