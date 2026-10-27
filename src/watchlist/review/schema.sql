-- Review service v2: case ledger, hash-chained audit events and the pinned
-- LangGraph SQLite checkpoint tables in ONE file, so a backup or rollback
-- always captures a coherent ledger/checkpoint pair. Rollback-journal mode
-- (the SQLite default; WAL is never enabled) lets read-only inspection open
-- the file without creating -wal/-shm sidecars.
CREATE TABLE workspace_meta (
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL
);
CREATE TABLE cases (
 case_id TEXT PRIMARY KEY,
 seq INTEGER NOT NULL UNIQUE CHECK(seq >= 1),
 thread_id TEXT NOT NULL UNIQUE,
 request_id TEXT UNIQUE,
 created_at TEXT NOT NULL,
 origin TEXT NOT NULL,
 header_json TEXT NOT NULL,
 header_sha256 TEXT NOT NULL
);
CREATE TABLE evidence (
 case_id TEXT PRIMARY KEY REFERENCES cases(case_id),
 payload_json TEXT NOT NULL,
 sha256 TEXT NOT NULL
);
CREATE TABLE packets (
 case_id TEXT PRIMARY KEY REFERENCES evidence(case_id),
 payload_json TEXT NOT NULL,
 sha256 TEXT NOT NULL
);
CREATE TABLE submissions (
 submission_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL UNIQUE REFERENCES packets(case_id),
 payload_json TEXT NOT NULL,
 sha256 TEXT NOT NULL
);
CREATE TABLE cancellations (
 case_id TEXT PRIMARY KEY REFERENCES cases(case_id),
 payload_json TEXT NOT NULL,
 sha256 TEXT NOT NULL
);
CREATE TABLE audit_events (
 case_id TEXT NOT NULL REFERENCES cases(case_id),
 sequence INTEGER NOT NULL CHECK(sequence >= 1),
 event_type TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 previous_sha256 TEXT NOT NULL,
 event_sha256 TEXT NOT NULL,
 PRIMARY KEY(case_id, sequence),
 UNIQUE(case_id, event_type)
);
-- A submission and a cancellation are mutually exclusive for one case.
CREATE TRIGGER submissions_not_cancelled BEFORE INSERT ON submissions
 WHEN EXISTS (SELECT 1 FROM cancellations WHERE case_id = NEW.case_id)
 BEGIN SELECT RAISE(ABORT, 'case is cancelled'); END;
CREATE TRIGGER cancellations_not_submitted BEFORE INSERT ON cancellations
 WHEN EXISTS (SELECT 1 FROM submissions WHERE case_id = NEW.case_id)
 BEGIN SELECT RAISE(ABORT, 'case already has a submission'); END;
-- Append-only guards (no UPDATE, no DELETE) against accidental edits. They are
-- not tamper-proof: the owner of the file can drop them, which the schema
-- check then reports, and the orphan/sequence check reports removed cases.
CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events
 BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events
 BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER cases_no_update BEFORE UPDATE ON cases
 BEGIN SELECT RAISE(ABORT, 'cases are append-only'); END;
CREATE TRIGGER cases_no_delete BEFORE DELETE ON cases
 BEGIN SELECT RAISE(ABORT, 'cases are append-only'); END;
CREATE TRIGGER evidence_no_update BEFORE UPDATE ON evidence
 BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER evidence_no_delete BEFORE DELETE ON evidence
 BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER packets_no_update BEFORE UPDATE ON packets
 BEGIN SELECT RAISE(ABORT, 'packets are append-only'); END;
CREATE TRIGGER packets_no_delete BEFORE DELETE ON packets
 BEGIN SELECT RAISE(ABORT, 'packets are append-only'); END;
CREATE TRIGGER submissions_no_update BEFORE UPDATE ON submissions
 BEGIN SELECT RAISE(ABORT, 'submissions are append-only'); END;
CREATE TRIGGER submissions_no_delete BEFORE DELETE ON submissions
 BEGIN SELECT RAISE(ABORT, 'submissions are append-only'); END;
CREATE TRIGGER cancellations_no_update BEFORE UPDATE ON cancellations
 BEGIN SELECT RAISE(ABORT, 'cancellations are append-only'); END;
CREATE TRIGGER cancellations_no_delete BEFORE DELETE ON cancellations
 BEGIN SELECT RAISE(ABORT, 'cancellations are append-only'); END;
-- Exact table layout of langgraph-checkpoint-sqlite 3.1.x. The service never
-- calls the saver's setup(), which would switch the file to WAL mode.
CREATE TABLE checkpoints (
 thread_id TEXT NOT NULL,
 checkpoint_ns TEXT NOT NULL DEFAULT '',
 checkpoint_id TEXT NOT NULL,
 parent_checkpoint_id TEXT,
 type TEXT,
 checkpoint BLOB,
 metadata BLOB,
 PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE writes (
 thread_id TEXT NOT NULL,
 checkpoint_ns TEXT NOT NULL DEFAULT '',
 checkpoint_id TEXT NOT NULL,
 task_id TEXT NOT NULL,
 task_path TEXT NOT NULL DEFAULT '',
 idx INTEGER NOT NULL,
 channel TEXT NOT NULL,
 type TEXT,
 value BLOB,
 PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
PRAGMA user_version = 2;
-- 0x50575232, ASCII "PWR2": Polyglot Watchlist Review ledger v2.
PRAGMA application_id = 1347899954;
