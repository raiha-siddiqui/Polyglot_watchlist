"""Backup, restore drills and audit-head anchors for v2 review + memory stores.

Backup (``create_backup``)
    Quiesces writers by holding the review workspace lock and then the memory
    store lock (the same order every other component uses), copies each SQLite
    file with the SQLite online-backup API into a **new** directory, then
    records per-file SHA-256, a logical content digest and row counts in
    ``backup-manifest.json`` (written last; a set without it is incomplete).

Restore (``restore_backup``)
    Verifies the backup set first, then copies into a **new** destination with
    the backup API, recreates lock files, and verifies the restored copy
    (identity, schema, integrity, logical digest; with a review config also
    every case's audit chain through the read-only ``ReviewService``) before
    reporting success. ``dry_run`` restores into a temporary directory, verifies
    and removes it. Activation (pointing services at the restored directory)
    is a deliberate operator step; nothing is overwritten in place.

Anchors (``export_anchor`` / ``verify_anchor``)
    Export the audit-chain head of every case, a catalogue digest and the memory
    event head. Re-verifying later detects truncated or replaced chain tails and
    removed cases. This is **limited tamper evidence**, not tamper-proofing: an
    anchor kept on the same writable disk is only a local receipt, and an owner
    who rewrites both the stores and the anchor is not detected.

Nothing here contacts a network or external service.
"""

from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Callable
from uuid import uuid4

from watchlist.review.contracts import (
    AuditAnchor,
    ReviewError,
    _Contract,
    canonical,
    digest,
    loads_strict,
)
from watchlist.review.locking import LOCK_NAME, create_lock_file, workspace_lock

BACKUP_FORMAT = "review-backup-v1"
ANCHOR_FORMAT = "review-anchor-v1"
MANIFEST_NAME = "backup-manifest.json"
ANCHOR_NOTICE = (
    "Limited tamper evidence only: this anchor records audit-chain heads at export "
    "time. Keep it on separate media or with a separate custodian; on the same "
    "writable disk it is only a local receipt. It is not tamper-proof storage and "
    "does not authenticate reviewers."
)
_MEMBERS = {
    "review": ("review.sqlite", "review"),
    "memory": ("memory.sqlite", "memory"),
}


class BackupError(ReviewError):
    """A backup, restore or anchor operation failed or did not verify."""

    code = "backup_failed"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _restrict(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _new_directory(
    path: Path | str, label: str, *, forbidden: tuple[Path, ...] = ()
) -> Path:
    """Require a missing or empty, non-symlink directory outside ``forbidden`` trees."""
    target = Path(os.path.abspath(Path(path)))
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise BackupError(f"{label} must be a directory path, not a file or symlink")
    if target.exists() and any(target.iterdir()):
        raise BackupError(f"{label} must be a new (missing or empty) directory")
    for other in forbidden:
        other = Path(os.path.abspath(other))
        if target == other or other in target.parents or target in other.parents:
            raise BackupError(
                f"{label} must not overlap a source directory",
                details={"destination": str(target), "source": str(other)},
            )
    return target


def file_sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _plain(value: Any) -> Any:
    return value.hex() if isinstance(value, bytes) else value


def logical_digest(db: sqlite3.Connection) -> tuple[str, dict[str, int]]:
    """Digest of every table's rows (order-independent) plus row counts."""
    tables = [
        r[0]
        for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE "
            "'sqlite_%' ORDER BY name"
        )
    ]
    content, counts = {}, {}
    for table in tables:
        rows = sorted(
            canonical([_plain(v) for v in row])
            for row in db.execute(f'SELECT * FROM "{table}"')
        )
        content[table] = rows
        counts[table] = len(rows)
    meta = [
        db.execute("PRAGMA application_id").fetchone()[0],
        db.execute("PRAGMA user_version").fetchone()[0],
    ]
    return digest({"pragmas": meta, "tables": content}), counts


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)


def _copy(source: Path, target: Path) -> None:
    """SQLite online-backup copy into a file that must not exist yet."""
    if target.exists():
        raise BackupError(
            "backup target file already exists", details={"file": str(target)}
        )
    with closing(_ro(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    _restrict(target)


def _check_identity(kind: str, path: Path) -> list[str]:
    """Owned-schema identity checks for one copied database; returns issues."""
    issues = []
    try:
        if kind == "review":
            from watchlist.review import storage

            with closing(storage.connect(path.parent, read_only=True)) as db:
                storage.verify_identity(db)
        else:
            from watchlist.case_memory import store as memory_store

            with closing(memory_store.connect(path.parent, read_only=True)) as db:
                memory_store.verify_identity(db)
                result = memory_store._verify(memory_store.MemoryDb(db, _utc_now))
                issues.extend(f"memory: {i}" for i in result.issues)
        with closing(_ro(path)) as db:
            if [r[0] for r in db.execute("PRAGMA integrity_check")] != ["ok"]:
                issues.append(f"{kind}: sqlite integrity_check failed")
    except ReviewError as exc:
        issues.append(f"{kind}: {exc.code}: {exc.message}")
    except sqlite3.Error as exc:
        issues.append(f"{kind}: unreadable database: {exc}")
    return issues


@dataclass(frozen=True)
class BackupManifest(_Contract):
    """What a backup set contains; written last so incomplete sets are detectable."""

    backup_format: str
    backup_id: str
    created_at: str
    members: dict[str, dict[str, Any]]
    notes: tuple[str, ...]

    @classmethod
    def from_dict(cls, data: object) -> BackupManifest:
        if not isinstance(data, dict) or set(data) != set(cls.__dataclass_fields__):
            raise BackupError("backup manifest fields mismatch")
        if data["backup_format"] != BACKUP_FORMAT:
            raise BackupError("unsupported backup format")
        members = data["members"]
        if (
            not isinstance(members, dict)
            or not members
            or not set(members) <= set(_MEMBERS)
        ):
            raise BackupError("backup manifest members are invalid")
        return cls(
            BACKUP_FORMAT,
            str(data["backup_id"]),
            str(data["created_at"]),
            members,
            tuple(data["notes"]),
        )


def create_backup(
    destination: Path | str,
    *,
    workspace: Path | str | None = None,
    memory_store: Path | str | None = None,
    lock_timeout: float = 5.0,
    clock: Callable[[], str] | None = None,
) -> BackupManifest:
    """Copy a v2 workspace and/or memory store into a new backup directory.

    Both locks are held for the whole copy, so the set is one coherent
    generation. A hot journal from a crashed writer is refused: open the store
    writable once so SQLite can roll it back, then retry.
    """
    sources = {
        kind: Path(os.path.abspath(Path(path)))
        for kind, path in (("review", workspace), ("memory", memory_store))
        if path is not None
    }
    if not sources:
        raise BackupError("nothing to back up: give a workspace and/or memory store")
    target = _new_directory(
        destination, "backup destination", forbidden=tuple(sources.values())
    )
    for kind, root in sources.items():
        name = _MEMBERS[kind][0]
        for member in (root / LOCK_NAME, root / name):
            if member.is_symlink() or not member.is_file():
                raise BackupError(f"{kind} source is incomplete: {member.name} missing")
    clock = clock or _utc_now
    members: dict[str, dict[str, Any]] = {}
    target.mkdir(parents=True, exist_ok=True)
    _restrict(target)
    with ExitStack() as stack:
        for kind in ("review", "memory"):  # fixed lock order
            if kind in sources:
                try:
                    stack.enter_context(
                        workspace_lock(sources[kind], timeout=lock_timeout)
                    )
                except ReviewError as exc:
                    raise BackupError(
                        f"cannot quiesce {kind} writers: {exc.message}",
                        details={"cause": exc.code},
                        retryable=exc.retryable,
                    ) from None
        for kind, root in sources.items():
            name, folder = _MEMBERS[kind]
            hot = [
                s for s in ("-journal", "-wal", "-shm") if (root / (name + s)).exists()
            ]
            if hot:
                raise BackupError(
                    f"{kind} store has an interrupted transaction journal; open it "
                    "writable once before backing up",
                    details={"files": [name + s for s in hot]},
                )
            issues = _check_identity(kind, root / name)
            if issues:
                raise BackupError(
                    f"{kind} source failed verification", details={"issues": issues}
                )
            (target / folder).mkdir()
            _restrict(target / folder)
            copied = target / folder / name
            _copy(root / name, copied)
            with closing(_ro(copied)) as db:
                logical, counts = logical_digest(db)
            members[kind] = {
                "file": f"{folder}/{name}",
                "sha256": file_sha256(copied),
                "bytes": copied.stat().st_size,
                "logical_sha256": logical,
                "row_counts": counts,
            }
    manifest = BackupManifest(
        BACKUP_FORMAT,
        str(uuid4()),
        clock(),
        members,
        (
            "Copies made with the SQLite online-backup API while writers were quiesced.",
            "Lock files are not copied; restore recreates them in a new directory.",
            "Backups contain reviewer notes and fictional query text; they expire "
            "under the backup retention policy and are deleted only by an explicit drill.",
        ),
    )
    path = target / MANIFEST_NAME
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    _restrict(path)
    return manifest


@dataclass(frozen=True)
class BackupVerification(_Contract):
    ok: bool
    backup_id: str | None
    issues: tuple[str, ...]


def read_manifest(backup_dir: Path | str) -> BackupManifest:
    path = Path(backup_dir) / MANIFEST_NAME
    if path.is_symlink() or not path.is_file():
        raise BackupError("backup set has no manifest (incomplete or not a backup)")
    try:
        return BackupManifest.from_dict(loads_strict(path.read_text(encoding="utf-8")))
    except BackupError:
        raise
    except ReviewError as exc:
        raise BackupError(f"backup manifest is invalid: {exc.message}") from None


def verify_backup(backup_dir: Path | str) -> BackupVerification:
    """Check file hashes, logical digests and owned-schema identity of a backup set."""
    root = Path(os.path.abspath(Path(backup_dir)))
    try:
        manifest = read_manifest(root)
    except BackupError as exc:
        return BackupVerification(False, None, (exc.message,))
    issues = []
    for kind, member in manifest.members.items():
        expected = _MEMBERS[kind]
        if member.get("file") != f"{expected[1]}/{expected[0]}":
            issues.append(f"{kind}: unexpected member path")
            continue
        path = root / member["file"]
        if path.is_symlink() or not path.is_file():
            issues.append(f"{kind}: file missing")
            continue
        if file_sha256(path) != member.get("sha256"):
            issues.append(f"{kind}: file SHA-256 differs from the manifest")
            continue
        try:
            with closing(_ro(path)) as db:
                logical, _ = logical_digest(db)
        except sqlite3.Error as exc:
            issues.append(f"{kind}: unreadable: {exc}")
            continue
        if logical != member.get("logical_sha256"):
            issues.append(f"{kind}: logical digest differs from the manifest")
        issues.extend(_check_identity(kind, path))
    return BackupVerification(not issues, manifest.backup_id, tuple(issues))


@dataclass(frozen=True)
class RestoreReport(_Contract):
    """Outcome of a restore or restore drill; ``elapsed_seconds`` is measured."""

    ok: bool
    dry_run: bool
    backup_id: str
    destination: str | None
    restored: dict[str, str]
    case_count: int | None
    elapsed_seconds: float
    issues: tuple[str, ...]


def _deep_review_check(review_root: Path, review_config: Any) -> tuple[int, list[str]]:
    """Open the restored workspace read-only and verify every case's audit chain."""
    from watchlist.review.service import ReviewService

    issues: list[str] = []
    service = ReviewService.open(review_root, review_config, read_only=True)
    page = service.config.operations.page_max
    offset = count = 0
    while True:
        summaries = service.list_cases(limit=page, offset=offset)
        if not summaries:
            break
        offset += len(summaries)
        for summary in summaries:
            count += 1
            result = service.verify_audit(summary.case_id)
            if not result.ok:
                issues.append(f"case {summary.case_id}: {'; '.join(result.issues)}")
    return count, issues


def restore_backup(
    backup_dir: Path | str,
    destination: Path | str | None = None,
    *,
    dry_run: bool = False,
    review_config: Any = None,
) -> RestoreReport:
    """Restore a verified backup set into a new directory and verify the result.

    ``destination`` gets ``review/`` and/or ``memory/`` subdirectories, each a
    complete store with a fresh lock file. With ``dry_run`` the restore goes to
    a temporary directory that is removed afterwards. Raises ``BackupError``
    when the backup set itself is invalid; otherwise returns a report whose
    ``ok`` is False if the restored copy did not verify.
    """
    started = time.perf_counter()
    source = Path(os.path.abspath(Path(backup_dir)))
    check = verify_backup(source)
    if not check.ok:
        raise BackupError(
            "backup set failed verification", details={"issues": list(check.issues)}
        )
    manifest = read_manifest(source)
    if dry_run:
        scratch = Path(tempfile.mkdtemp(prefix="watchlist-restore-drill-"))
        target = scratch / "restored"
    else:
        if destination is None:
            raise BackupError("a restore destination is required unless dry_run is set")
        scratch = None
        target = _new_directory(destination, "restore destination", forbidden=(source,))
    issues: list[str] = []
    restored: dict[str, str] = {}
    case_count = None
    try:
        target.mkdir(parents=True, exist_ok=True)
        _restrict(target)
        for kind, member in manifest.members.items():
            name, folder = _MEMBERS[kind]
            root = target / folder
            root.mkdir()
            _restrict(root)
            create_lock_file(root)
            _copy(source / member["file"], root / name)
            with closing(_ro(root / name)) as db:
                logical, counts = logical_digest(db)
            if logical != member["logical_sha256"] or counts != member["row_counts"]:
                issues.append(f"{kind}: restored content differs from the manifest")
            issues.extend(_check_identity(kind, root / name))
            restored[kind] = str(root)
        if review_config is not None and "review" in manifest.members and not issues:
            try:
                case_count, deep = _deep_review_check(target / "review", review_config)
                issues.extend(deep)
            except ReviewError as exc:
                issues.append(f"review: {exc.code}: {exc.message}")
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return RestoreReport(
        not issues,
        dry_run,
        manifest.backup_id,
        None if dry_run else str(target),
        {} if dry_run else restored,
        case_count,
        round(time.perf_counter() - started, 6),
        tuple(issues),
    )


# ---------------------------------------------------------------------------
# Audit-head anchors.
# ---------------------------------------------------------------------------


def _anchor_body(service: Any, memory: Any, created_at: str) -> dict[str, Any]:
    cases = []
    page = service.config.operations.page_max
    offset = 0
    while True:
        summaries = service.list_cases(limit=page, offset=offset)
        if not summaries:
            break
        offset += len(summaries)
        for summary in summaries:
            view = service.audit(summary.case_id)
            cases.append(
                {
                    "case_id": summary.case_id,
                    "count": view.count,
                    "head_sha256": view.head_sha256,
                }
            )
    body: dict[str, Any] = {
        "anchor_format": ANCHOR_FORMAT,
        "created_at": created_at,
        "notice": ANCHOR_NOTICE,
        "review": {
            "case_count": len(cases),
            "catalogue_sha256": digest(cases),
            "cases": cases,
        },
        "memory": None,
    }
    if memory is not None:
        sequence, head = memory.head()
        body["memory"] = {"event_count": sequence, "head_sha256": head}
    return body


def export_anchor(
    service: Any,
    destination_file: Path | str,
    *,
    memory: Any = None,
    clock: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Write an anchor file (exclusive create) for every case and the memory head.

    ``service`` is a (read-only) ``ReviewService``; ``memory`` an optional
    ``MemoryStore``. Store the file on separate media; see ``ANCHOR_NOTICE``.
    """
    body = _anchor_body(service, memory, (clock or _utc_now)())
    anchor = {**body, "anchor_sha256": digest(body)}
    path = Path(destination_file)
    if path.is_symlink():
        raise BackupError("anchor destination must not be a symlink")
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(anchor, indent=2, sort_keys=True))
    except FileExistsError:
        raise BackupError(
            "anchor file already exists; anchors are never overwritten"
        ) from None
    _restrict(path)
    return anchor


@dataclass(frozen=True)
class AnchorVerification(_Contract):
    """Result of comparing current stores with an earlier anchor (not authentication)."""

    ok: bool
    cases_checked: int
    memory_checked: bool
    issues: tuple[str, ...]


def _check_anchor_shape(anchor: object) -> None:
    """Raise ``backup_failed`` unless the anchor has exactly the expected structure."""

    def fail(where: str) -> None:
        raise BackupError(f"malformed anchor file: {where}")

    def is_count(value: object) -> bool:
        return type(value) is int and value >= 0

    def keys(value: object, expected: set[str], where: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            fail(f"{where} must be an object")
        if set(value) != expected:
            fail(f"{where} must have exactly the keys {sorted(expected)}")
        return value

    anchor = keys(
        anchor,
        {"anchor_format", "created_at", "notice", "review", "memory", "anchor_sha256"},
        "anchor",
    )
    if anchor["anchor_format"] != ANCHOR_FORMAT:
        raise BackupError("unsupported anchor format")
    for name in ("created_at", "notice", "anchor_sha256"):
        if type(anchor[name]) is not str:
            fail(f"{name} must be a string")
    review = keys(
        anchor["review"], {"case_count", "catalogue_sha256", "cases"}, "review"
    )
    if (
        not is_count(review["case_count"])
        or type(review["catalogue_sha256"]) is not str
    ):
        fail("review.case_count or review.catalogue_sha256 has the wrong type")
    if not isinstance(review["cases"], list):
        fail("review.cases must be a list")
    for index, entry in enumerate(review["cases"]):
        entry = keys(
            entry, {"case_id", "count", "head_sha256"}, f"review.cases[{index}]"
        )
        if (
            type(entry["case_id"]) is not str
            or not is_count(entry["count"])
            or type(entry["head_sha256"]) is not str
        ):
            fail(f"review.cases[{index}] has a field of the wrong type")
    if anchor["memory"] is not None:
        memory = keys(anchor["memory"], {"event_count", "head_sha256"}, "memory")
        if (
            not is_count(memory["event_count"])
            or type(memory["head_sha256"]) is not str
        ):
            fail("memory has a field of the wrong type")


def verify_anchor(
    anchor_file: Path | str, service: Any, *, memory: Any = None
) -> AnchorVerification:
    """Detect removed cases, truncated/replaced chain tails and memory rewinds."""
    try:
        anchor = loads_strict(Path(anchor_file).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ReviewError) as exc:
        raise BackupError(f"cannot read anchor: {exc}") from None
    _check_anchor_shape(anchor)
    issues = []
    body = {k: v for k, v in anchor.items() if k != "anchor_sha256"}
    if digest(body) != anchor.get("anchor_sha256"):
        issues.append("anchor: self digest mismatch (anchor file altered)")
    cases = anchor["review"]["cases"]
    if digest(cases) != anchor["review"]["catalogue_sha256"]:
        issues.append("anchor: catalogue digest mismatch")
    if anchor["review"]["case_count"] != len(cases):
        issues.append("anchor: case_count differs from the listed cases")
    for entry in cases:
        try:
            result = service.verify_audit(
                entry["case_id"],
                AuditAnchor(entry["case_id"], entry["count"], entry["head_sha256"]),
            )
        except ReviewError as exc:
            issues.append(f"case {entry['case_id']}: {exc.code}")
            continue
        issues.extend(f"case {entry['case_id']}: {i}" for i in result.issues)
    memory_checked = False
    if anchor.get("memory") is not None:
        if memory is None:
            issues.append("anchor covers a memory store that was not supplied")
        else:
            memory_checked = True
            expected = anchor["memory"]
            with memory.session(write=False) as mdb:
                from watchlist.case_memory.store import verify_events

                events = mdb.events()
                _, _, chain = verify_events(events)
            issues.extend(f"memory: {i}" for i in chain)
            count = expected["event_count"]
            if len(events) < count:
                issues.append(
                    "memory: event chain is shorter than the anchor (truncated)"
                )
            elif count and events[count - 1]["event_sha256"] != expected["head_sha256"]:
                issues.append("memory: event hash at the anchored position differs")
    return AnchorVerification(not issues, len(cases), memory_checked, tuple(issues))


def _main(argv: list[str] | None = None) -> int:
    """``python -m watchlist.review.backup {create,verify,restore,anchor,verify-anchor}``."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m watchlist.review.backup")
    parser.add_argument(
        "command", choices=("create", "verify", "restore", "anchor", "verify-anchor")
    )
    parser.add_argument("--workspace")
    parser.add_argument("--memory")
    parser.add_argument("--memory-config")
    parser.add_argument("--review-config")
    parser.add_argument("--backup")
    parser.add_argument("--destination")
    parser.add_argument("--anchor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            result: Any = create_backup(
                args.destination, workspace=args.workspace, memory_store=args.memory
            )
        elif args.command == "verify":
            result = verify_backup(args.backup)
        elif args.command == "restore":
            result = restore_backup(
                args.backup,
                args.destination,
                dry_run=args.dry_run,
                review_config=args.review_config,
            )
        else:
            from watchlist.review.service import ReviewService

            service = ReviewService.open(
                args.workspace, args.review_config, read_only=True
            )
            memory = None
            if args.memory:
                from watchlist.case_memory.store import MemoryStore

                memory = MemoryStore.open(
                    args.memory, args.memory_config, read_only=True
                )
            if args.command == "anchor":
                result = {
                    "anchor_sha256": export_anchor(service, args.anchor, memory=memory)[
                        "anchor_sha256"
                    ]
                }
            else:
                result = verify_anchor(args.anchor, service, memory=memory)
        payload = result if isinstance(result, dict) else result.to_dict()
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if payload.get("ok", True) else 1
    except ReviewError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False))
        return 2
    finally:
        sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(_main())
