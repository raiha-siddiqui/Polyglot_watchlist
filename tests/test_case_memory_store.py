"""Memory store creation, owned schema, read-only behaviour and integrity checks."""

import sqlite3

import pytest

from test_case_memory_support import Env, memory_config
from review_v2_support import tree
from watchlist.case_memory.state import (
    MemoryInputError,
    MemoryIntegrityError,
    MemoryUnavailableError,
)
from watchlist.case_memory.store import DB_NAME, MemoryStore


def test_initialize_requires_new_directory_and_open_never_creates(tmp_path):
    store = MemoryStore.initialize(tmp_path / "mem", memory_config())
    assert sorted(p.name for p in (tmp_path / "mem").iterdir()) == [
        DB_NAME,
        "writer.lock",
    ]
    assert store.verify().ok
    with pytest.raises(MemoryInputError):
        MemoryStore.initialize(tmp_path / "mem", memory_config())
    with pytest.raises(MemoryUnavailableError):
        MemoryStore.open(tmp_path / "missing", memory_config())
    assert not (tmp_path / "missing").exists()


def test_read_only_open_and_lookups_change_no_files(tmp_path):
    env = Env(tmp_path)
    env.review(["syn-001"])
    env.publish()
    before = tree(env.memory_dir)
    reader = MemoryStore.open(env.memory_dir, memory_config(), read_only=True)
    assert reader.verify().ok
    env.start(["syn-001"], memory=True)  # provider lookup uses a read session
    assert tree(env.memory_dir) == before
    with pytest.raises(MemoryInputError):
        reader.register_source_workspace("ws-x")


def test_append_only_triggers_block_edits_and_unauthorized_deletes(tmp_path):
    env = Env(tmp_path)
    env.review(["syn-001"])
    env.publish()
    db = sqlite3.connect(env.memory_dir / DB_NAME)
    for sql in (
        "UPDATE annotations SET outcome='none_selected'",
        "DELETE FROM annotations",
        "DELETE FROM annotation_records",
        "DELETE FROM memory_events",
        "UPDATE memory_events SET target_id='x'",
        "DELETE FROM publication_intents",
        "UPDATE key_versions SET revision = revision + 5",
    ):
        with pytest.raises(sqlite3.DatabaseError):
            db.execute(sql)
    db.close()
    assert env.store.verify().ok


def test_schema_change_is_rejected_as_inconsistent_not_empty(tmp_path):
    env = Env(tmp_path)
    db = sqlite3.connect(env.memory_dir / DB_NAME)
    db.execute("DROP TRIGGER annotations_no_update")
    db.commit()
    db.close()
    with pytest.raises(MemoryIntegrityError):
        MemoryStore.open(env.memory_dir, memory_config())
    # A new case with the memory provider fails visibly instead of showing no history.
    from watchlist.review.contracts import ContextProviderError

    with pytest.raises(ContextProviderError) as caught:
        env.start(["syn-001"], memory=True)
    assert caught.value.details["cause"] == "memory_inconsistent"


def test_foreign_or_corrupt_file_is_unavailable_or_inconsistent(tmp_path):
    env = Env(tmp_path)
    (env.memory_dir / DB_NAME).write_bytes(b"not a database at all" * 100)
    with pytest.raises((MemoryIntegrityError, MemoryUnavailableError)):
        MemoryStore.open(env.memory_dir, memory_config())


def test_verify_detects_payload_tamper_and_rehash_attempts(tmp_path):
    env = Env(tmp_path)
    env.review(["syn-001"], reason="Fictional original opinion.")
    env.publish()
    path = env.memory_dir / DB_NAME
    db = sqlite3.connect(path)
    db.execute("DROP TRIGGER annotations_no_update")
    db.execute(
        "UPDATE annotations SET payload_json = replace(payload_json, 'original', 'edited')"
    )
    db.execute(
        "CREATE TRIGGER annotations_no_update BEFORE UPDATE ON annotations\n BEGIN SELECT "
        "RAISE(ABORT, 'annotations are append-only'); END"
    )
    db.commit()
    db.close()
    result = env.store.verify()
    assert not result.ok
    assert any("digest mismatch" in issue for issue in result.issues)


def test_event_chain_break_is_reported(tmp_path):
    env = Env(tmp_path)
    env.review(["syn-001"])
    env.publish()
    db = sqlite3.connect(env.memory_dir / DB_NAME)
    db.execute("DROP TRIGGER memory_events_no_update")
    db.execute(
        "UPDATE memory_events SET created_at='2020-01-01T00:00:00+00:00' WHERE sequence=2"
    )
    db.execute(
        "CREATE TRIGGER memory_events_no_update BEFORE UPDATE ON memory_events\n BEGIN "
        "SELECT RAISE(ABORT, 'memory events are append-only'); END"
    )
    db.commit()
    db.close()
    issues = env.store.verify().issues
    assert "memory event 2: event hash mismatch" in issues


def test_busy_store_is_unavailable_and_retryable(tmp_path):
    from watchlist.review.locking import workspace_lock

    env = Env(tmp_path)
    store = MemoryStore.open(env.memory_dir, memory_config(), lock_timeout=0)
    import threading

    held, release = threading.Event(), threading.Event()

    def hold():
        with workspace_lock(env.memory_dir, timeout=1):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    held.wait(5)
    try:
        with pytest.raises(MemoryUnavailableError) as caught:
            store.verify()
        assert caught.value.retryable
    finally:
        release.set()
        thread.join()
