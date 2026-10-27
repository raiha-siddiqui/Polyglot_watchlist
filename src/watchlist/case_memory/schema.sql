-- Case memory store v1: a rebuildable projection of completed v2 reviews,
-- keyed only by exact namespaced reference-record IDs and snapshot
-- fingerprints. There is no raw query text, name column or name hash.
-- Rollback-journal mode (WAL is never enabled) so read-only opens create no
-- sidecar files.
CREATE TABLE memory_meta (
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL
);
-- Registered source review workspaces (an operator-chosen stable label).
CREATE TABLE source_workspaces (
 workspace_id TEXT PRIMARY KEY,
 registered_at TEXT NOT NULL
);
-- Outbox: one durable "publish this completed case" intent per source case.
-- case_id is globally unique (v2 case IDs are UUIDs), so a copied workspace
-- registered under another label cannot publish the same case twice.
CREATE TABLE publication_intents (
 case_id TEXT PRIMARY KEY,
 source_workspace_id TEXT NOT NULL REFERENCES source_workspaces(workspace_id),
 annotation_id TEXT NOT NULL UNIQUE,
 final_event_sha256 TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL,
 enqueued_at TEXT NOT NULL
);
-- Published annotations. A row exists only after the publish transaction
-- committed together with its event and key revisions.
CREATE TABLE annotations (
 annotation_id TEXT PRIMARY KEY REFERENCES publication_intents(annotation_id),
 source_workspace_id TEXT NOT NULL,
 case_id TEXT NOT NULL UNIQUE REFERENCES publication_intents(case_id),
 submission_id TEXT NOT NULL UNIQUE,
 origin TEXT NOT NULL CHECK(origin IN ('human','simulated_demo','test','unknown')),
 outcome TEXT NOT NULL CHECK(outcome IN ('candidate_for_follow_up','none_selected','unresolved')),
 reviewed_at TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL,
 published_sequence INTEGER NOT NULL UNIQUE,
 UNIQUE(source_workspace_id, case_id)
);
CREATE TABLE annotation_records (
 annotation_id TEXT NOT NULL REFERENCES annotations(annotation_id),
 record_key TEXT NOT NULL,
 namespace_id TEXT NOT NULL,
 source_entity_id TEXT NOT NULL,
 snapshot_sha256 TEXT NOT NULL,
 relation TEXT NOT NULL CHECK(relation IN ('selected_for_follow_up','considered_without_selection','unresolved_context')),
 rank INTEGER NOT NULL CHECK(rank >= 1),
 PRIMARY KEY(annotation_id, record_key, snapshot_sha256)
);
CREATE INDEX annotation_records_lookup ON annotation_records(record_key, snapshot_sha256, annotation_id);
-- Append-only corrections. A correction links to the original; the original
-- row is never rewritten. One correction per original.
CREATE TABLE corrections (
 correction_id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('withdrawal','supersession')),
 original_annotation_id TEXT NOT NULL UNIQUE REFERENCES annotations(annotation_id),
 replacement_annotation_id TEXT REFERENCES annotations(annotation_id),
 actor TEXT NOT NULL,
 reason TEXT NOT NULL,
 created_at TEXT NOT NULL,
 event_sequence INTEGER NOT NULL UNIQUE,
 CHECK((kind = 'supersession') = (replacement_annotation_id IS NOT NULL)),
 CHECK(replacement_annotation_id IS NULL OR replacement_annotation_id != original_annotation_id)
);
-- Minimal retention tombstones: IDs, hashes and dates only, no notes or names.
CREATE TABLE tombstones (
 annotation_id TEXT PRIMARY KEY,
 source_workspace_id TEXT NOT NULL,
 case_id TEXT NOT NULL UNIQUE,
 payload_sha256 TEXT NOT NULL,
 record_keys_json TEXT NOT NULL,
 reviewed_at TEXT NOT NULL,
 deleted_at TEXT NOT NULL,
 policy_json TEXT NOT NULL,
 event_sequence INTEGER NOT NULL
);
-- A source case that could not be published (conflict, unregistered data).
CREATE TABLE quarantine (
 case_id TEXT NOT NULL,
 source_workspace_id TEXT NOT NULL,
 reason_code TEXT NOT NULL,
 detail_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(case_id, source_workspace_id, reason_code, detail_sha256)
);
-- Rebuildable revision counter per exact key: incremented by every event that
-- touches the key, so a pending form can detect changed (even formerly empty)
-- history. verify() recomputes it from the event chain.
CREATE TABLE key_versions (
 record_key TEXT NOT NULL,
 snapshot_sha256 TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision >= 1),
 PRIMARY KEY(record_key, snapshot_sha256)
);
-- Hash-chained memory events: identifiers and digests only, never free text.
CREATE TABLE memory_events (
 sequence INTEGER PRIMARY KEY CHECK(sequence >= 1),
 event_type TEXT NOT NULL,
 target_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 previous_sha256 TEXT NOT NULL,
 event_sha256 TEXT NOT NULL UNIQUE
);
-- Append-only guards. Content rows may be deleted only after a retention
-- tombstone for that annotation exists (same transaction). These triggers
-- guard against accidents; they are not tamper-proof.
CREATE TRIGGER memory_events_no_update BEFORE UPDATE ON memory_events
 BEGIN SELECT RAISE(ABORT, 'memory events are append-only'); END;
CREATE TRIGGER memory_events_no_delete BEFORE DELETE ON memory_events
 BEGIN SELECT RAISE(ABORT, 'memory events are append-only'); END;
CREATE TRIGGER source_workspaces_no_update BEFORE UPDATE ON source_workspaces
 BEGIN SELECT RAISE(ABORT, 'source workspaces are append-only'); END;
CREATE TRIGGER source_workspaces_no_delete BEFORE DELETE ON source_workspaces
 BEGIN SELECT RAISE(ABORT, 'source workspaces are append-only'); END;
CREATE TRIGGER publication_intents_no_update BEFORE UPDATE ON publication_intents
 BEGIN SELECT RAISE(ABORT, 'publication intents are append-only'); END;
CREATE TRIGGER publication_intents_delete_tombstoned BEFORE DELETE ON publication_intents
 WHEN NOT EXISTS (SELECT 1 FROM tombstones WHERE annotation_id = OLD.annotation_id)
 BEGIN SELECT RAISE(ABORT, 'publication intents are deleted only by retention'); END;
CREATE TRIGGER annotations_no_update BEFORE UPDATE ON annotations
 BEGIN SELECT RAISE(ABORT, 'annotations are append-only'); END;
CREATE TRIGGER annotations_delete_tombstoned BEFORE DELETE ON annotations
 WHEN NOT EXISTS (SELECT 1 FROM tombstones WHERE annotation_id = OLD.annotation_id)
 BEGIN SELECT RAISE(ABORT, 'annotations are deleted only by retention'); END;
CREATE TRIGGER annotation_records_no_update BEFORE UPDATE ON annotation_records
 BEGIN SELECT RAISE(ABORT, 'annotation records are append-only'); END;
CREATE TRIGGER annotation_records_delete_tombstoned BEFORE DELETE ON annotation_records
 WHEN NOT EXISTS (SELECT 1 FROM tombstones WHERE annotation_id = OLD.annotation_id)
 BEGIN SELECT RAISE(ABORT, 'annotation records are deleted only by retention'); END;
CREATE TRIGGER corrections_no_update BEFORE UPDATE ON corrections
 BEGIN SELECT RAISE(ABORT, 'corrections are append-only'); END;
CREATE TRIGGER corrections_delete_tombstoned BEFORE DELETE ON corrections
 WHEN NOT EXISTS (SELECT 1 FROM tombstones WHERE annotation_id IN (OLD.original_annotation_id, OLD.replacement_annotation_id))
 BEGIN SELECT RAISE(ABORT, 'corrections are deleted only by retention'); END;
CREATE TRIGGER tombstones_no_update BEFORE UPDATE ON tombstones
 BEGIN SELECT RAISE(ABORT, 'tombstones are append-only'); END;
CREATE TRIGGER tombstones_no_delete BEFORE DELETE ON tombstones
 BEGIN SELECT RAISE(ABORT, 'tombstones are append-only'); END;
CREATE TRIGGER quarantine_no_update BEFORE UPDATE ON quarantine
 BEGIN SELECT RAISE(ABORT, 'quarantine is append-only'); END;
CREATE TRIGGER quarantine_no_delete BEFORE DELETE ON quarantine
 BEGIN SELECT RAISE(ABORT, 'quarantine is append-only'); END;
CREATE TRIGGER key_versions_monotonic BEFORE UPDATE ON key_versions
 WHEN NEW.revision != OLD.revision + 1 OR NEW.record_key != OLD.record_key OR NEW.snapshot_sha256 != OLD.snapshot_sha256
 BEGIN SELECT RAISE(ABORT, 'key revisions only increase by one'); END;
CREATE TRIGGER key_versions_no_delete BEFORE DELETE ON key_versions
 BEGIN SELECT RAISE(ABORT, 'key revisions are never deleted'); END;
PRAGMA user_version = 1;
-- 0x50574D31, ASCII "PWM1": Polyglot Watchlist Memory store v1.
PRAGMA application_id = 1347898673;
