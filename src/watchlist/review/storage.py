"""v2 workspace files, owned SQLite schema, append-only ledger and audit chain.

A v2 workspace is a directory with exactly two owned files:

* ``writer.lock`` — the cooperative cross-process lock (see ``locking``);
* ``review.sqlite`` — ledger tables, audit events and LangGraph checkpoints.

``create_workspace`` is the only function that creates files. ``connect``
opens an existing database with ``mode=rw`` or ``mode=ro`` and never creates
one; read-only connections create no sidecar files because WAL is never used.
All ledger writes happen in short ``BEGIN IMMEDIATE`` transactions under the
workspace lock; replays of an existing record are accepted only when byte-equal.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from watchlist.review.contracts import (
    AUDIT_SCHEMA,
    CASE_SCHEMA,
    PACKET_SCHEMA,
    SUBMISSION_SCHEMA,
    WORKSPACE_FORMAT,
    AcceptedReview,
    AuditEvent,
    AuditReceipt,
    Cancellation,
    CaseHeader,
    NotFoundError,
    RetrievalResult,
    ReviewPacket,
    ReviewError,
    StaleSubmissionError,
    StorageIntegrityError,
    SubmissionConflictError,
    InvalidStateError,
    VersionMismatchError,
    WorkspaceBusyError,
    WorkspaceError,
    canonical,
    digest,
)
from watchlist.review.locking import LOCK_NAME, create_lock_file, workspace_lock

DB_NAME = "review.sqlite"
APPLICATION_ID = 0x50575232  # ASCII "PWR2"
USER_VERSION = 2
CHECKPOINT_FORMAT = "langgraph-checkpoint-sqlite-3"
_SIDECARS = ("-journal", "-wal", "-shm")
_BUSY_MS = 5000


def expected_meta() -> dict[str, str]:
    """Contract versions every compatible v2 workspace must declare."""
    return {
        "workspace_format": WORKSPACE_FORMAT,
        "case_schema": CASE_SCHEMA,
        "packet_schema": PACKET_SCHEMA,
        "submission_schema": SUBMISSION_SCHEMA,
        "audit_schema": AUDIT_SCHEMA,
        "checkpoint_format": CHECKPOINT_FORMAT,
    }


def schema_sql() -> str:
    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def _schema_signature(db: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
    """Describe all schema objects with whitespace-normalized SQL."""
    return tuple(
        (kind, name, table, " ".join(sql.split()) if sql else None)
        for kind, name, table, sql in db.execute(
            "SELECT type,name,tbl_name,sql FROM main.sqlite_master ORDER BY type,name"
        )
    )


@lru_cache(maxsize=1)
def _expected_signature() -> tuple[tuple[Any, ...], ...]:
    with closing(sqlite3.connect(":memory:")) as reference:
        reference.executescript(schema_sql())
        return _schema_signature(reference)


def require_checkpoint_runtime() -> None:
    """The checkpoint tables are pinned to the 3.x SQLite saver layout."""
    try:
        installed = version("langgraph-checkpoint-sqlite")
    except PackageNotFoundError:
        raise WorkspaceError(
            "langgraph-checkpoint-sqlite is not installed (agent extra)"
        ) from None
    if installed.split(".")[0] != "3":
        raise VersionMismatchError(
            "unsupported checkpoint saver version", details={"installed": installed}
        )


# ---------------------------------------------------------------------------
# Workspace files.
# ---------------------------------------------------------------------------


def _absolute(root: Path | str) -> Path:
    # abspath normalizes '..' lexically, so '../data/review-v2' is accepted;
    # the check below only guards against a platform that leaves one behind.
    path = Path(os.path.abspath(Path(root)))
    if ".." in path.parts:
        raise WorkspaceError("workspace path must not contain parent traversal")
    return path


def check_workspace(root: Path | str) -> Path:
    """Require an existing, complete workspace without creating or changing files."""
    path = _absolute(root)
    if path.is_symlink() or not path.is_dir():
        raise WorkspaceError(
            "existing review workspace directory is missing",
            details={"workspace": str(path)},
        )
    for name in (LOCK_NAME, DB_NAME):
        member = path / name
        if member.is_symlink() or not member.is_file():
            raise WorkspaceError(
                f"workspace is incomplete: {name} is missing or not a regular file"
            )
    return path


def create_workspace(root: Path | str, *, created_at: str) -> Path:
    """Create a new v2 workspace in a missing or empty directory.

    The lock file is created exclusively first, so two concurrent initializers
    cannot both claim the directory. A crash during initialization leaves an
    incomplete workspace that ``open`` rejects and ``initialize`` refuses to
    reuse; it contains no cases and may be removed by the operator.
    """
    require_checkpoint_runtime()
    path = _absolute(root)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise WorkspaceError("workspace path exists and is not a directory")
    if path.exists() and any(path.iterdir()):
        raise WorkspaceError(
            "new review workspace must be missing or empty",
            details={"workspace": str(path)},
        )
    path.mkdir(parents=True, exist_ok=True)
    create_lock_file(path)
    with workspace_lock(path, timeout=0):
        database = path / DB_NAME
        if database.exists():
            raise WorkspaceError("workspace database already exists")
        with closing(sqlite3.connect(database, isolation_level=None)) as db:
            db.executescript(schema_sql())
            db.execute("BEGIN IMMEDIATE")
            meta = {**expected_meta(), "created_at": created_at}
            db.executemany(
                "INSERT INTO workspace_meta VALUES (?,?)", sorted(meta.items())
            )
            db.execute("COMMIT")
    return path


def connect(root: Path, *, read_only: bool) -> sqlite3.Connection:
    """Open the existing database; ``mode=ro``/``mode=rw`` never create a file."""
    uri = (root / DB_NAME).as_uri() + ("?mode=ro" if read_only else "?mode=rw")
    try:
        db = sqlite3.connect(
            uri, uri=True, isolation_level=None, check_same_thread=False
        )
        db.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
        db.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:
        raise WorkspaceError(f"cannot open review database: {exc}") from None
    db.row_factory = sqlite3.Row
    return db


def settle_journal(db: sqlite3.Connection) -> None:
    """Let SQLite dispose of a non-hot leftover journal under the workspace lock.

    Call only after identity verification. A value-preserving header write
    reuses/deletes the journal without changing any ledger or checkpoint row.
    """
    version = db.execute("PRAGMA user_version").fetchone()[0]
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(f"PRAGMA user_version={version}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def connect_checkpoints(root: Path, *, read_only: bool) -> sqlite3.Connection:
    """A separate connection for the LangGraph saver, which manages its own commits."""
    uri = (root / DB_NAME).as_uri() + ("?mode=ro" if read_only else "?mode=rw")
    db = sqlite3.connect(uri, uri=True, check_same_thread=False)
    db.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
    return db


def sqlite_failure(exc: sqlite3.Error, message: str) -> ReviewError:
    """Map a SQLite error: lock contention is retryable, anything else is integrity.

    The workspace lock serializes cooperating writers, so ``database is locked``
    means a process outside the service holds the file; it is not corruption.
    """
    text = str(exc)
    if isinstance(exc, sqlite3.OperationalError) and (
        "locked" in text.lower() or "busy" in text.lower()
    ):
        return WorkspaceBusyError(
            f"review database is locked by another process: {text}"
        )
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code in (
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_CANTOPEN,
    ):
        return WorkspaceError("review storage is unavailable")
    return StorageIntegrityError(f"{message}: {text}")


# Rows whose case is missing, and a gap in the creation sequence, both mean a
# case was removed from the catalogue (the delete triggers were bypassed).
_ORPHAN_CHECKS = (
    *(
        (
            f"{table} rows without a case",
            f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS (SELECT 1 FROM cases c WHERE c.case_id = t.case_id)",
        )
        for table in (
            "evidence",
            "packets",
            "submissions",
            "cancellations",
            "audit_events",
        )
    ),
    *(
        (
            f"{table} rows without a case thread",
            f"SELECT COUNT(*) FROM {table} t WHERE NOT EXISTS (SELECT 1 FROM cases c WHERE c.thread_id = t.thread_id)",
        )
        for table in ("checkpoints", "writes")
    ),
    ("case sequence has gaps", "SELECT COALESCE(MAX(seq), 0) - COUNT(*) FROM cases"),
)


def catalogue_issues(db: sqlite3.Connection) -> list[str]:
    """Workspace-wide checks that no case was removed; reads only."""
    return [label for label, sql in _ORPHAN_CHECKS if db.execute(sql).fetchone()[0]]


def verify_identity(db: sqlite3.Connection) -> None:
    """Reject foreign, damaged, differently versioned or truncated stores before any work."""
    try:
        if db.execute("PRAGMA main.application_id").fetchone()[0] != APPLICATION_ID:
            raise WorkspaceError("database is not a v2 review ledger")
        found = db.execute("PRAGMA main.user_version").fetchone()[0]
        if found != USER_VERSION:
            raise VersionMismatchError(
                "unsupported review ledger version", details={"found": found}
            )
        if db.execute("PRAGMA main.journal_mode").fetchone()[0].lower() == "wal":
            raise WorkspaceError("review ledger must not use WAL mode")
        if _schema_signature(db) != _expected_signature():
            raise WorkspaceError(
                "review ledger schema differs from the owned v2 schema"
            )
        meta = {
            row[0]: row[1] for row in db.execute("SELECT key,value FROM workspace_meta")
        }
        issues = catalogue_issues(db)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise sqlite_failure(exc, "review ledger") from None
        raise WorkspaceError(f"unreadable review ledger: {exc}") from None
    except sqlite3.DatabaseError as exc:
        raise WorkspaceError(f"unreadable review ledger: {exc}") from None
    for key, value in expected_meta().items():
        if meta.get(key) != value:
            raise VersionMismatchError(
                f"workspace {key} differs from this service",
                details={"expected": value, "found": meta.get(key)},
            )
    if issues:
        raise StorageIntegrityError(
            "a case was removed from the catalogue; preserve the workspace and "
            "investigate",
            details={"issues": issues},
        )
    require_checkpoint_runtime()


def sidecars(root: Path) -> list[str]:
    """Names of SQLite sidecar files currently present (diagnostics/tests)."""
    return [
        DB_NAME + suffix for suffix in _SIDECARS if (root / (DB_NAME + suffix)).exists()
    ]


# ---------------------------------------------------------------------------
# Audit hash chain. Each event hashes its envelope, which includes the
# previous event's hash. This is limited local tamper evidence: whoever can
# rewrite the whole file can recompute the chain; an AuditAnchor kept
# elsewhere detects truncation or replacement of the tail.
# ---------------------------------------------------------------------------


def event_envelope(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "audit_schema": AUDIT_SCHEMA,
        **{
            k: row[k]
            for k in (
                "case_id",
                "sequence",
                "event_type",
                "payload_json",
                "created_at",
                "previous_sha256",
            )
        },
    }


def verify_chain(
    rows: list[dict[str, Any]], case_id: str
) -> tuple[int, str, list[str]]:
    """Recompute every event hash and link; return (count, head, issues)."""
    previous, issues = "", []
    for expected_sequence, row in enumerate(rows, 1):
        if row["case_id"] != case_id or row["sequence"] != expected_sequence:
            issues.append(f"audit event {expected_sequence}: sequence or case mismatch")
        if row["previous_sha256"] != previous:
            issues.append(f"audit event {expected_sequence}: broken previous-hash link")
        if digest(event_envelope(row)) != row["event_sha256"]:
            issues.append(f"audit event {expected_sequence}: event hash mismatch")
        try:
            if canonical(json.loads(row["payload_json"])) != row["payload_json"]:
                issues.append(f"audit event {expected_sequence}: noncanonical payload")
        except ValueError:
            issues.append(f"audit event {expected_sequence}: payload is not JSON")
        previous = row["event_sha256"]
    return len(rows), previous, issues


def to_audit_event(row: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        row["case_id"],
        row["sequence"],
        row["event_type"],
        json.loads(row["payload_json"]),
        row["created_at"],
        row["previous_sha256"],
        row["event_sha256"],
    )


@dataclass(frozen=True)
class LedgerSnapshot:
    """Raw saved rows of one case, read without interpretation or repair."""

    case_row: dict[str, Any]
    evidence_row: dict[str, Any] | None
    packet_row: dict[str, Any] | None
    submission_row: dict[str, Any] | None
    cancellation_row: dict[str, Any] | None
    events: tuple[dict[str, Any], ...]


class Ledger:
    """Append-only case records bound to one connection inside one locked session."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _row(self, sql: str, args: tuple[Any, ...]) -> dict[str, Any] | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    # -- reads ---------------------------------------------------------------

    def case_row(self, case_id: str) -> dict[str, Any]:
        row = self._row("SELECT * FROM cases WHERE case_id=?", (case_id,))
        if row is None:
            raise NotFoundError("unknown case ID", details={"case_id": case_id})
        return row

    def snapshot(self, case_id: str) -> LedgerSnapshot:
        case_row = self.case_row(case_id)
        events = tuple(
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM audit_events WHERE case_id=? ORDER BY sequence",
                (case_id,),
            )
        )
        return LedgerSnapshot(
            case_row,
            self._row("SELECT * FROM evidence WHERE case_id=?", (case_id,)),
            self._row("SELECT * FROM packets WHERE case_id=?", (case_id,)),
            self._row("SELECT * FROM submissions WHERE case_id=?", (case_id,)),
            self._row("SELECT * FROM cancellations WHERE case_id=?", (case_id,)),
            events,
        )

    def case_ids(self, limit: int, offset: int, origin: str | None = None) -> list[str]:
        """Stable catalogue order: creation sequence, assigned under the lock."""
        if origin is None:
            sql, args = (
                "SELECT case_id FROM cases ORDER BY seq LIMIT ? OFFSET ?",
                (limit, offset),
            )
        else:
            sql, args = (
                "SELECT case_id FROM cases WHERE origin=? ORDER BY seq LIMIT ? OFFSET ?",
                (origin, limit, offset),
            )
        return [r[0] for r in self.db.execute(sql, args)]

    def case_for_request(self, request_id: str) -> str | None:
        row = self.db.execute(
            "SELECT case_id FROM cases WHERE request_id=?", (request_id,)
        ).fetchone()
        return None if row is None else row[0]

    def _checked(self, row: dict[str, Any] | None, label: str) -> Any:
        if row is None:
            return None
        value = json.loads(row["payload_json"])
        if digest(value) != row["sha256"]:
            raise StorageIntegrityError(
                f"saved {label} checksum mismatch", details={"case_id": row["case_id"]}
            )
        return value

    def header(self, case_id: str) -> CaseHeader:
        row = self.case_row(case_id)
        value = json.loads(row["header_json"])
        if digest(value) != row["header_sha256"]:
            raise StorageIntegrityError(
                "saved case header checksum mismatch", details={"case_id": case_id}
            )
        return CaseHeader.from_dict(value)

    def evidence(self, case_id: str) -> RetrievalResult | None:
        value = self._checked(
            self._row("SELECT * FROM evidence WHERE case_id=?", (case_id,)), "evidence"
        )
        return None if value is None else RetrievalResult.from_dict(value)

    def packet(self, case_id: str) -> tuple[ReviewPacket, str] | None:
        row = self._row("SELECT * FROM packets WHERE case_id=?", (case_id,))
        value = self._checked(row, "packet")
        return None if value is None else (ReviewPacket.from_dict(value), row["sha256"])

    def submission(self, case_id: str) -> AcceptedReview | None:
        value = self._checked(
            self._row("SELECT * FROM submissions WHERE case_id=?", (case_id,)),
            "submission",
        )
        return None if value is None else AcceptedReview.from_dict(value)

    def submission_case(self, submission_id: str) -> str | None:
        row = self.db.execute(
            "SELECT case_id FROM submissions WHERE submission_id=?", (submission_id,)
        ).fetchone()
        return None if row is None else row[0]

    def events(self, case_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM audit_events WHERE case_id=? ORDER BY sequence",
                (case_id,),
            )
        ]

    # -- writes (each its own short transaction) -----------------------------

    def _append(
        self, case_id: str, event_type: str, payload: dict[str, Any], created_at: str
    ) -> dict[str, Any]:
        """Append one event or return the identical existing one (idempotent replay)."""
        rows = self.events(case_id)
        encoded = canonical(payload)
        for row in rows:
            if row["event_type"] == event_type:
                if row["payload_json"] != encoded:
                    raise StorageIntegrityError(
                        f"conflicting replay of audit event {event_type}",
                        details={"case_id": case_id},
                    )
                return row
        count, head, issues = verify_chain(rows, case_id)
        if issues:
            raise StorageIntegrityError(
                "audit chain integrity failure",
                details={"case_id": case_id, "issues": issues},
            )
        row = {
            "case_id": case_id,
            "sequence": count + 1,
            "event_type": event_type,
            "payload_json": encoded,
            "created_at": created_at,
            "previous_sha256": head,
        }
        row["event_sha256"] = digest(event_envelope(row))
        self.db.execute(
            "INSERT INTO audit_events VALUES (?,?,?,?,?,?,?)",
            tuple(
                row[k]
                for k in (
                    "case_id",
                    "sequence",
                    "event_type",
                    "payload_json",
                    "created_at",
                    "previous_sha256",
                    "event_sha256",
                )
            ),
        )
        return row

    def create_case(self, header: CaseHeader, *, max_cases: int) -> None:
        value = header.to_dict()
        with self.transaction():
            count, top = self.db.execute(
                "SELECT COUNT(*), COALESCE(MAX(seq),0) FROM cases"
            ).fetchone()
            if count >= max_cases:
                raise InvalidStateError(
                    "workspace case limit reached", details={"max_cases": max_cases}
                )
            self.db.execute(
                "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?)",
                (
                    header.case_id,
                    top + 1,
                    header.thread_id,
                    header.request_id,
                    header.created_at,
                    header.origin,
                    canonical(value),
                    digest(value),
                ),
            )
            self._append(header.case_id, "case_created", value, header.created_at)

    def _put_once(
        self,
        table: str,
        case_id: str,
        value: dict[str, Any],
        label: str,
        extra: tuple[Any, ...] = (),
    ) -> str:
        """Insert a content row once; an identical replay is a no-op."""
        sha = digest(value)
        old = self._row(
            f"SELECT payload_json, sha256 FROM {table} WHERE case_id=?", (case_id,)
        )
        if old is not None:
            if (old["payload_json"], old["sha256"]) != (canonical(value), sha):
                raise StorageIntegrityError(
                    f"conflicting replay of saved {label}", details={"case_id": case_id}
                )
            return sha
        placeholders = ",".join("?" * (3 + len(extra)))
        self.db.execute(
            f"INSERT INTO {table} VALUES ({placeholders})",
            (*extra, case_id, canonical(value), sha),
        )
        return sha

    def record_evidence(
        self, case_id: str, result: RetrievalResult, created_at: str
    ) -> str:
        with self.transaction():
            sha = self._put_once("evidence", case_id, result.to_dict(), "evidence")
            self._append(
                case_id, "evidence_recorded", {"evidence_sha256": sha}, created_at
            )
        return sha

    def record_packet(self, case_id: str, packet: ReviewPacket, created_at: str) -> str:
        with self.transaction():
            sha = self._put_once("packets", case_id, packet.to_dict(), "packet")
            self._append(case_id, "review_ready", {"packet_sha256": sha}, created_at)
        return sha

    def record_submission(self, review: AcceptedReview) -> None:
        """Accept exactly one review per case; UNIQUE constraints back the lock."""
        value = review.to_dict()
        try:
            with self.transaction():
                self.db.execute(
                    "INSERT INTO submissions VALUES (?,?,?,?)",
                    (
                        review.submission.submission_id,
                        review.case_id,
                        canonical(value),
                        digest(value),
                    ),
                )
                self._append(
                    review.case_id,
                    "review_submitted",
                    {"review": value},
                    review.submitted_at,
                )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "cancelled" in message:
                raise InvalidStateError(
                    "case is cancelled", details={"case_id": review.case_id}
                ) from None
            if "submissions.case_id" in message:
                raise StaleSubmissionError(
                    "case already has an accepted review",
                    details={"case_id": review.case_id},
                ) from None
            raise SubmissionConflictError(
                "submission_id is already used",
                details={"submission_id": review.submission.submission_id},
            ) from None

    def record_cancellation(self, cancellation: Cancellation) -> None:
        value = cancellation.to_dict()
        try:
            with self.transaction():
                self._put_once(
                    "cancellations", cancellation.case_id, value, "cancellation"
                )
                self._append(
                    cancellation.case_id,
                    "case_cancelled",
                    {"cancellation": value},
                    cancellation.cancelled_at,
                )
        except sqlite3.IntegrityError:
            raise InvalidStateError(
                "case already has an accepted review",
                details={"case_id": cancellation.case_id},
            ) from None

    def finalize(
        self, case_id: str, payload: dict[str, Any], created_at: str
    ) -> AuditReceipt:
        """Commit the single logical final event; a replay returns the same receipt."""
        with self.transaction():
            row = self._append(case_id, "case_finalized", payload, created_at)
        return AuditReceipt(case_id, row["sequence"], row["event_sha256"])
