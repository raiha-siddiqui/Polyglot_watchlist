"""Memory store files, owned schema, transactions and the memory event chain.

A memory store is a directory with exactly two owned files:

* ``writer.lock`` — the same portable cooperative lock as a v2 review workspace;
* ``memory.sqlite`` — projection tables, lifecycle rows and hash-chained events.

``MemoryStore.initialize`` is the only call that creates files; ``open`` never
creates, migrates or repairs. Every operation takes the store lock with a
bounded wait and opens a fresh connection. Lock order is always *review
workspace, then memory store* (the context provider runs inside the review
lock; the publisher reads the ledger first and releases it before locking
memory), so the two locks cannot deadlock.

The event chain and append-only triggers are limited local tamper evidence:
whoever can rewrite the whole file can recompute every hash. A separately
kept anchor (see ``watchlist.review.backup``) detects truncation.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

from watchlist.case_memory.state import (
    MEMORY_SCHEMA,
    Annotation,
    CaseMemoryError,
    MemoryConfig,
    MemoryInputError,
    MemoryIntegrityError,
    MemoryUnavailableError,
    MemoryVersionError,
    annotation_id_for,
    utc_now,
)
from watchlist.review.contracts import (
    ReviewError,
    WorkspaceBusyError,
    _Contract,
    canonical,
    digest,
)
from watchlist.review.locking import (
    LOCK_NAME,
    create_lock_file,
    validated_timeout,
    workspace_lock,
)

DB_NAME = "memory.sqlite"
# Quarantine reason for an outbox intent that ``drain`` could not publish. Such
# an intent is "blocked": excluded from pending work, reported separately and
# never retried automatically (an operator must resolve it).
BLOCKED_REASON = "publication_blocked"
# SQL condition (alias ``i`` = publication_intents) for intents still to publish.
PENDING_INTENTS_WHERE = (
    "NOT EXISTS (SELECT 1 FROM annotations a WHERE a.annotation_id = i.annotation_id) "
    "AND NOT EXISTS (SELECT 1 FROM quarantine q WHERE q.case_id = i.case_id "
    f"AND q.reason_code = '{BLOCKED_REASON}')"
)
APPLICATION_ID = 0x50574D31  # ASCII "PWM1"
USER_VERSION = 1
_SIDECARS = ("-journal", "-wal", "-shm")
_BUSY_MS = 5000
EVENT_TYPES = (
    "store_initialized",
    "source_registered",
    "annotation_published",
    "publication_quarantined",
    "annotation_withdrawn",
    "annotation_superseded",
    "annotation_deleted",
    "retention_applied",
)


def _no_failpoint(stage: str) -> None:
    return None


def schema_sql() -> str:
    return Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def _signature(db: sqlite3.Connection) -> tuple[tuple[Any, ...], ...]:
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
        return _signature(reference)


def restrict_permissions(path: Path) -> None:
    """Owner-only permissions where the OS supports POSIX modes.

    On Windows ``os.chmod`` only toggles the read-only flag, so this is a no-op
    there; directory ACLs remain the operator's responsibility (see
    docs/threat-model.md).
    """
    if os.name == "nt":
        return
    os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _absolute(root: Path | str) -> Path:
    path = Path(os.path.abspath(Path(root)))
    if ".." in path.parts:
        raise MemoryInputError("memory store path must not contain parent traversal")
    return path


def check_store(root: Path | str) -> Path:
    """Require an existing complete store; never creates or changes files."""
    path = _absolute(root)
    if path.is_symlink() or not path.is_dir():
        raise MemoryUnavailableError(
            "memory store directory is missing", details={"store": str(path)}
        )
    for name in (LOCK_NAME, DB_NAME):
        member = path / name
        if member.is_symlink() or not member.is_file():
            raise MemoryUnavailableError(
                f"memory store is incomplete: {name} is missing or not a regular file"
            )
    return path


def sidecars(root: Path) -> list[str]:
    return [DB_NAME + s for s in _SIDECARS if (root / (DB_NAME + s)).exists()]


def connect(root: Path, *, read_only: bool) -> sqlite3.Connection:
    """Open the existing store (``mode=ro``/``mode=rw`` never create a file)."""
    uri = (root / DB_NAME).as_uri() + ("?mode=ro" if read_only else "?mode=rw")
    try:
        db = sqlite3.connect(
            uri, uri=True, isolation_level=None, check_same_thread=False
        )
        db.execute(f"PRAGMA busy_timeout={_BUSY_MS}")
        db.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error as exc:
        raise MemoryUnavailableError(f"cannot open memory store: {exc}") from None
    db.row_factory = sqlite3.Row
    return db


def _settle_journal(db: sqlite3.Connection) -> None:
    """Clear a leftover rollback journal under the store lock.

    A *hot* journal was already rolled back by SQLite when ``verify_identity``
    first read the file. A process that died before its journal header was
    synced leaves a journal SQLite treats as not hot (nothing to roll back) but
    does not delete; read-only sessions would then refuse to open forever. A
    value-preserving header write in its own transaction makes SQLite reuse and
    delete that file. No row changes.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(f"PRAGMA user_version = {USER_VERSION}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def verify_identity(db: sqlite3.Connection) -> dict[str, str]:
    """Reject foreign, differently versioned or schema-altered stores."""
    try:
        if db.execute("PRAGMA main.application_id").fetchone()[0] != APPLICATION_ID:
            raise MemoryIntegrityError("database is not a case memory store")
        found = db.execute("PRAGMA main.user_version").fetchone()[0]
        if found != USER_VERSION:
            raise MemoryVersionError(
                "unsupported memory store version", details={"found": found}
            )
        if db.execute("PRAGMA main.journal_mode").fetchone()[0].lower() == "wal":
            raise MemoryIntegrityError("memory store must not use WAL mode")
        if _signature(db) != _expected_signature():
            raise MemoryIntegrityError(
                "memory store schema differs from the owned v1 schema"
            )
        meta = {r[0]: r[1] for r in db.execute("SELECT key,value FROM memory_meta")}
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            raise MemoryUnavailableError(f"memory store is busy: {exc}") from None
        raise MemoryIntegrityError(f"unreadable memory store: {exc}") from None
    except sqlite3.DatabaseError as exc:
        raise MemoryIntegrityError(f"unreadable memory store: {exc}") from None
    if meta.get("memory_schema") != MEMORY_SCHEMA:
        raise MemoryVersionError(
            "memory store schema version differs",
            details={"expected": MEMORY_SCHEMA, "found": meta.get("memory_schema")},
        )
    return meta


# ---------------------------------------------------------------------------
# Event chain.
# ---------------------------------------------------------------------------


def blocked_intent_count(mdb: MemoryDb) -> int:
    """Outbox intents that ``drain`` quarantined as unpublishable."""
    return mdb.row(
        "SELECT COUNT(*) AS n FROM publication_intents i WHERE NOT EXISTS "
        "(SELECT 1 FROM annotations a WHERE a.annotation_id = i.annotation_id) "
        "AND EXISTS (SELECT 1 FROM quarantine q WHERE q.case_id = i.case_id "
        f"AND q.reason_code = '{BLOCKED_REASON}')"
    )["n"]


def event_envelope(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "memory_schema": MEMORY_SCHEMA,
        **{
            k: row[k]
            for k in (
                "sequence",
                "event_type",
                "target_id",
                "payload_json",
                "created_at",
                "previous_sha256",
            )
        },
    }


def verify_events(rows: Sequence[dict[str, Any]]) -> tuple[int, str, list[str]]:
    """Recompute every memory event hash and link; return (count, head, issues)."""
    previous, issues = "", []
    for expected, row in enumerate(rows, 1):
        if row["sequence"] != expected:
            issues.append(f"memory event {expected}: sequence gap or reorder")
        if row["previous_sha256"] != previous:
            issues.append(f"memory event {expected}: broken previous-hash link")
        if digest(event_envelope(row)) != row["event_sha256"]:
            issues.append(f"memory event {expected}: event hash mismatch")
        if row["event_type"] not in EVENT_TYPES:
            issues.append(f"memory event {expected}: unknown event type")
        try:
            if canonical(json.loads(row["payload_json"])) != row["payload_json"]:
                issues.append(f"memory event {expected}: noncanonical payload")
        except ValueError:
            issues.append(f"memory event {expected}: payload is not JSON")
        previous = row["event_sha256"]
    return len(rows), previous, issues


class MemoryDb:
    """One locked connection plus the store's write helpers."""

    def __init__(self, db: sqlite3.Connection, clock: Callable[[], str]):
        self.db = db
        self.clock = clock

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def rows(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args)]

    def row(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        found = self.db.execute(sql, args).fetchone()
        return None if found is None else dict(found)

    def events(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM memory_events ORDER BY sequence")

    def head(self) -> tuple[int, str]:
        row = self.row(
            "SELECT sequence, event_sha256 FROM memory_events ORDER BY sequence DESC LIMIT 1"
        )
        return (0, "") if row is None else (row["sequence"], row["event_sha256"])

    def append_event(
        self,
        event_type: str,
        target_id: str,
        payload: dict[str, Any],
        *,
        keys: Sequence[tuple[str, str]] = (),
    ) -> dict[str, Any]:
        """Append one event (inside the caller's transaction) and bump key revisions.

        ``payload`` must hold identifiers and digests only. ``keys`` are
        ``(record_key, snapshot_sha256)`` pairs whose revision this event changes;
        they are stored in the payload so ``verify`` can recompute revisions.
        """
        if event_type not in EVENT_TYPES:
            raise MemoryIntegrityError(f"unknown memory event type {event_type}")
        pairs = sorted({(k, s) for k, s in keys})
        body = {**payload, "record_keys": [list(p) for p in pairs]}
        sequence, head = self.head()
        row = {
            "sequence": sequence + 1,
            "event_type": event_type,
            "target_id": target_id,
            "payload_json": canonical(body),
            "created_at": self.clock(),
            "previous_sha256": head,
        }
        row["event_sha256"] = digest(event_envelope(row))
        self.db.execute(
            "INSERT INTO memory_events VALUES (?,?,?,?,?,?,?)",
            tuple(
                row[k]
                for k in (
                    "sequence",
                    "event_type",
                    "target_id",
                    "payload_json",
                    "created_at",
                    "previous_sha256",
                    "event_sha256",
                )
            ),
        )
        for record_key, snapshot in pairs:
            self.db.execute(
                "INSERT INTO key_versions VALUES (?,?,1) ON CONFLICT(record_key, "
                "snapshot_sha256) DO UPDATE SET revision = revision + 1",
                (record_key, snapshot),
            )
        return row

    def key_revisions(
        self, pairs: Sequence[tuple[str, str]]
    ) -> dict[tuple[str, str], int]:
        result = {}
        for record_key, snapshot in pairs:
            row = self.row(
                "SELECT revision FROM key_versions WHERE record_key=? AND snapshot_sha256=?",
                (record_key, snapshot),
            )
            result[(record_key, snapshot)] = 0 if row is None else row["revision"]
        return result

    def annotation(self, annotation_id: str) -> Annotation | None:
        row = self.row(
            "SELECT payload_json, payload_sha256 FROM annotations WHERE annotation_id=?",
            (annotation_id,),
        )
        if row is None:
            return None
        value = json.loads(row["payload_json"])
        if digest(value) != row["payload_sha256"]:
            raise MemoryIntegrityError(
                "saved annotation checksum mismatch",
                details={"annotation_id": annotation_id},
            )
        return Annotation.from_dict(value)


@dataclass(frozen=True)
class MemoryVerification(_Contract):
    """Result of local integrity checks; ``ok`` is tamper evidence, not authentication.

    ``pending_intents`` will publish on the next ``drain``; ``blocked_intents``
    were quarantined as unpublishable and need an operator. Blocked intents do
    not make ``ok`` false (the store is consistent), but they are never
    reported as pending work.
    """

    ok: bool
    event_count: int
    head_sha256: str
    annotation_count: int
    pending_intents: int
    tombstone_count: int
    issues: tuple[str, ...]
    blocked_intents: int = 0


class MemoryStore:
    """Opened handle on one memory store directory (holds no open connection)."""

    def __init__(
        self,
        root: Path,
        config: MemoryConfig,
        *,
        read_only: bool,
        clock: Callable[[], str] | None = None,
        failpoint: Callable[[str], None] | None = None,
        lock_timeout: float | None = None,
    ):
        if not isinstance(config, MemoryConfig):
            raise TypeError(
                "config must be a MemoryConfig; use MemoryConfig.load(path)"
            )
        self._root = Path(root).absolute()
        self._config = config
        self._registry = config.registry()
        self._read_only = bool(read_only)
        self._clock = clock or utc_now
        self._failpoint = failpoint or _no_failpoint
        self._timeout = validated_timeout(
            config.lock_timeout_seconds if lock_timeout is None else lock_timeout
        )

    @staticmethod
    def _load(config: MemoryConfig | Path | str) -> MemoryConfig:
        return config if isinstance(config, MemoryConfig) else MemoryConfig.load(config)

    @classmethod
    def initialize(
        cls, root: Path | str, config: MemoryConfig | Path | str, **options: Any
    ) -> MemoryStore:
        """Create a new store in a missing or empty directory and open it writable."""
        config = cls._load(config)
        clock = options.get("clock") or utc_now
        path = _absolute(root)
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise MemoryInputError("memory store path exists and is not a directory")
        if path.exists() and any(path.iterdir()):
            raise MemoryInputError(
                "new memory store must be a missing or empty directory",
                details={"store": str(path)},
            )
        path.mkdir(parents=True, exist_ok=True)
        restrict_permissions(path)
        try:
            create_lock_file(path)
        except ReviewError as exc:
            raise MemoryInputError(exc.message) from None
        with workspace_lock(path, timeout=0):
            database = path / DB_NAME
            with closing(sqlite3.connect(database, isolation_level=None)) as db:
                db.executescript(schema_sql())
                db.execute("BEGIN IMMEDIATE")
                created = clock()
                meta = {
                    "memory_schema": MEMORY_SCHEMA,
                    "created_at": created,
                    "store_uuid": str(uuid4()),
                }
                db.executemany(
                    "INSERT INTO memory_meta VALUES (?,?)", sorted(meta.items())
                )
                db.row_factory = sqlite3.Row
                MemoryDb(db, lambda: created).append_event(
                    "store_initialized",
                    meta["store_uuid"],
                    {"memory_schema": MEMORY_SCHEMA},
                )
                db.execute("COMMIT")
            restrict_permissions(database)
        return cls.open(path, config, **options)

    @classmethod
    def open(
        cls,
        root: Path | str,
        config: MemoryConfig | Path | str,
        *,
        read_only: bool = False,
        **options: Any,
    ) -> MemoryStore:
        """Open an existing store; never creates, migrates or repairs files."""
        config = cls._load(config)
        store = cls(check_store(root), config, read_only=read_only, **options)
        with store.session(write=not read_only):
            pass
        return store

    # -- properties --------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self._root

    @property
    def config(self) -> MemoryConfig:
        return self._config

    @property
    def registry(self):
        return self._registry

    @property
    def read_only(self) -> bool:
        return self._read_only

    def now(self) -> str:
        return self._clock()

    def failpoint(self, stage: str) -> None:
        self._failpoint(stage)

    # -- sessions ----------------------------------------------------------------

    @contextmanager
    def session(self, *, write: bool) -> Iterator[MemoryDb]:
        """Lock the store, open a fresh connection and verify its identity."""
        if write and self._read_only:
            raise MemoryInputError("this memory store was opened read-only")
        check_store(self._root)
        try:
            lock = workspace_lock(self._root, timeout=self._timeout)
            lock.__enter__()
        except WorkspaceBusyError as exc:
            raise MemoryUnavailableError(
                "memory store is busy; lock wait timed out", details=exc.details
            ) from None
        except CaseMemoryError:
            raise
        except ReviewError as exc:
            if exc.code == "reentrant_call":
                raise
            raise MemoryUnavailableError(exc.message) from None
        try:
            if not write and sidecars(self._root):
                raise MemoryUnavailableError(
                    "an interrupted transaction left a SQLite journal; open the memory "
                    "store writable once so SQLite can roll it back",
                    details={"files": sidecars(self._root)},
                )
            db = connect(self._root, read_only=not write)
            try:
                verify_identity(db)
                if write and sidecars(self._root):
                    _settle_journal(db)
                yield MemoryDb(db, self._clock)
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    raise MemoryUnavailableError(
                        f"memory store is busy: {exc}"
                    ) from None
                raise MemoryIntegrityError(f"memory store error: {exc}") from None
            except sqlite3.Error as exc:
                raise MemoryIntegrityError(f"memory store error: {exc}") from None
            finally:
                db.close()
        finally:
            lock.__exit__(None, None, None)

    # -- operations ----------------------------------------------------------------

    def register_source_workspace(self, workspace_id: str) -> None:
        """Register an operator-chosen stable label for a v2 review workspace."""
        from watchlist.case_memory.identifiers import validate_identifier

        workspace_id = validate_identifier(workspace_id, "workspace_id")
        with self.session(write=True) as mdb:
            if mdb.row(
                "SELECT 1 FROM source_workspaces WHERE workspace_id=?", (workspace_id,)
            ):
                return
            with mdb.transaction():
                mdb.db.execute(
                    "INSERT INTO source_workspaces VALUES (?,?)",
                    (workspace_id, mdb.clock()),
                )
                mdb.append_event(
                    "source_registered", workspace_id, {"workspace_id": workspace_id}
                )

    def head(self) -> tuple[int, str]:
        with self.session(write=False) as mdb:
            return mdb.head()

    def verify(self) -> MemoryVerification:
        """Check SQLite integrity, the event chain, payload digests and projections.

        Reports issues instead of raising for content problems; an unreadable or
        foreign store still raises (it is unavailable, never "no history").
        """
        with self.session(write=False) as mdb:
            return _verify(mdb)


def _verify(mdb: MemoryDb) -> MemoryVerification:
    issues: list[str] = []
    integrity = [r[0] for r in mdb.db.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        issues.append("sqlite integrity_check failed")
    if mdb.db.execute("PRAGMA foreign_key_check").fetchall():
        issues.append("foreign key violations")
    events = mdb.events()
    count, head, chain = verify_events(events)
    issues.extend(chain)
    by_sequence = {e["sequence"]: e for e in events}
    revisions: Counter[tuple[str, str]] = Counter()
    for event in events:
        try:
            for pair in json.loads(event["payload_json"]).get("record_keys", []):
                revisions[(pair[0], pair[1])] += 1
        except (ValueError, TypeError, IndexError, AttributeError):
            issues.append(f"memory event {event['sequence']}: unreadable key list")
    stored = {
        (r["record_key"], r["snapshot_sha256"]): r["revision"]
        for r in mdb.rows("SELECT * FROM key_versions")
    }
    if stored != dict(revisions):
        issues.append("key revisions differ from the event chain")

    intents = {
        r["annotation_id"]: r for r in mdb.rows("SELECT * FROM publication_intents")
    }
    for annotation_id, intent in intents.items():
        try:
            value = json.loads(intent["payload_json"])
        except ValueError:
            issues.append(f"intent {annotation_id[:12]}: payload is not JSON")
            continue
        if digest(value) != intent["payload_sha256"]:
            issues.append(f"intent {annotation_id[:12]}: payload digest mismatch")
    annotations = mdb.rows("SELECT * FROM annotations")
    records = mdb.rows("SELECT * FROM annotation_records")
    by_annotation: dict[str, set[tuple[Any, ...]]] = {}
    for r in records:
        by_annotation.setdefault(r["annotation_id"], set()).add(
            (
                r["record_key"],
                r["namespace_id"],
                r["source_entity_id"],
                r["snapshot_sha256"],
                r["relation"],
                r["rank"],
            )
        )
    for row in annotations:
        label = f"annotation {row['annotation_id'][:12]}"
        try:
            value = json.loads(row["payload_json"])
            annotation = Annotation.from_dict(value)
        except (ValueError, ReviewError):
            issues.append(f"{label}: unreadable payload")
            continue
        if digest(value) != row["payload_sha256"]:
            issues.append(f"{label}: payload digest mismatch")
        if (
            annotation.annotation_id != row["annotation_id"]
            or annotation_id_for(annotation.source_workspace_id, annotation.case_id)
            != row["annotation_id"]
        ):
            issues.append(f"{label}: identifier mismatch")
        intent = intents.get(row["annotation_id"])
        if intent is None or intent["payload_sha256"] != row["payload_sha256"]:
            issues.append(f"{label}: does not match its publication intent")
        expected = {
            (
                rec.record_key,
                rec.namespace_id,
                rec.source_entity_id,
                rec.snapshot_sha256,
                rec.relation,
                rec.rank,
            )
            for rec in annotation.records
        }
        if by_annotation.get(row["annotation_id"], set()) != expected:
            issues.append(f"{label}: record rows differ from the payload")
        event = by_sequence.get(row["published_sequence"])
        if (
            event is None
            or event["event_type"] != "annotation_published"
            or event["target_id"] != row["annotation_id"]
            or json.loads(event["payload_json"]).get("payload_sha256")
            != row["payload_sha256"]
        ):
            issues.append(f"{label}: publication event missing or different")
    published_ids = {r["annotation_id"] for r in annotations}
    for event in events:
        if (
            event["event_type"] == "annotation_published"
            and event["target_id"] not in published_ids
        ):
            if not mdb.row(
                "SELECT 1 FROM tombstones WHERE annotation_id=?", (event["target_id"],)
            ):
                issues.append(
                    f"memory event {event['sequence']}: published annotation is missing "
                    "without a retention tombstone"
                )
    tombstones = mdb.rows("SELECT * FROM tombstones")
    for tomb in tombstones:
        label = f"tombstone {tomb['annotation_id'][:12]}"
        if tomb["annotation_id"] in published_ids or tomb["annotation_id"] in intents:
            issues.append(f"{label}: content still present after deletion")
        event = by_sequence.get(tomb["event_sequence"])
        if event is None or event["event_type"] != "annotation_deleted":
            issues.append(f"{label}: deletion event missing")
    for corr in mdb.rows("SELECT * FROM corrections"):
        event = by_sequence.get(corr["event_sequence"])
        expected_type = (
            "annotation_withdrawn"
            if corr["kind"] == "withdrawal"
            else "annotation_superseded"
        )
        if (
            event is None
            or event["event_type"] != expected_type
            or event["target_id"] != corr["original_annotation_id"]
        ):
            issues.append(f"correction {corr['correction_id'][:12]}: event mismatch")
    blocked = blocked_intent_count(mdb)
    pending = sum(1 for a in intents if a not in published_ids) - blocked
    return MemoryVerification(
        not issues,
        count,
        head,
        len(annotations),
        pending,
        len(tombstones),
        tuple(issues),
        blocked,
    )
