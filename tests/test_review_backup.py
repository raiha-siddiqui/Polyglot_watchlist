"""Backup/restore drills into new directories and audit-head anchoring."""

import json

import pytest

from test_case_memory_support import SNAP_MAIN, Env, memory_config
from review_v2_support import make_config, submission, tree
from watchlist.case_memory.store import MemoryStore
from watchlist.review.backup import (
    MANIFEST_NAME,
    BackupError,
    create_backup,
    export_anchor,
    restore_backup,
    verify_anchor,
    verify_backup,
)
from watchlist.review.service import ReviewService


@pytest.fixture
def env(tmp_path):
    env = Env(tmp_path / "live")
    env.review(["syn-001", "syn-012"], origin="human", reason="Fictional backup note.")
    env.review(["syn-002"], outcome="unresolved", origin="test")
    env.start(["syn-003"])  # one case still awaiting review
    env.publish()
    return env


def test_backup_and_restore_drill_into_new_directories(env, tmp_path):
    live_before = tree(env.workspace), tree(env.memory_dir)
    manifest = create_backup(
        tmp_path / "backups" / "b1",
        workspace=env.workspace,
        memory_store=env.memory_dir,
    )
    assert set(manifest.members) == {"review", "memory"}
    assert (
        tree(env.workspace),
        tree(env.memory_dir),
    ) == live_before  # sources untouched
    check = verify_backup(tmp_path / "backups" / "b1")
    assert check.ok, check.issues
    report = restore_backup(
        tmp_path / "backups" / "b1", tmp_path / "restored", review_config=make_config()
    )
    assert report.ok, report.issues
    assert report.case_count == 3 and report.elapsed_seconds > 0
    restored = ReviewService.open(
        tmp_path / "restored" / "review", make_config(), read_only=True
    )
    original = ReviewService.open(env.workspace, make_config(), read_only=True)
    assert [c.to_dict() for c in restored.list_cases()] == [
        c.to_dict() for c in original.list_cases()
    ]
    for summary in original.list_cases():
        assert restored.get_case(summary.case_id) == original.get_case(summary.case_id)
    memory = MemoryStore.open(
        tmp_path / "restored" / "memory", memory_config(), read_only=True
    )
    verification = memory.verify()
    assert verification.ok and verification.annotation_count == 2
    assert memory.head() == env.store.head()
    # The restored workspace is usable: the pending case accepts its review there.
    writable = ReviewService.open(
        tmp_path / "restored" / "review",
        make_config(),
        retriever=env.retriever(SNAP_MAIN),
    )
    pending = [c for c in writable.list_cases() if c.status == "awaiting_review"][0]
    view = writable.get_case(pending.case_id)
    done = writable.submit_review(
        view.case_id, submission(view, candidate_entity_id="syn-003")
    )
    assert done.status == "completed"
    # ...and the live workspace is unaffected by activity in the restored copy.
    assert original.get_case(pending.case_id).status == "awaiting_review"


def test_restore_dry_run_leaves_nothing_behind(env, tmp_path):
    create_backup(tmp_path / "b", workspace=env.workspace, memory_store=env.memory_dir)
    report = restore_backup(tmp_path / "b", dry_run=True, review_config=make_config())
    assert report.ok and report.dry_run and report.destination is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b", "live"]


def test_destinations_must_be_new_and_outside_sources(env, tmp_path):
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(BackupError, match="new"):
        create_backup(tmp_path / "used", workspace=env.workspace)
    with pytest.raises(BackupError, match="overlap"):
        create_backup(env.workspace / "inside", workspace=env.workspace)
    create_backup(tmp_path / "b", memory_store=env.memory_dir)
    with pytest.raises(BackupError, match="new"):
        restore_backup(tmp_path / "b", tmp_path / "used")
    with pytest.raises(BackupError):
        create_backup(tmp_path / "nothing")


def test_corrupt_or_incomplete_backups_are_detected_before_restore(env, tmp_path):
    create_backup(tmp_path / "b", workspace=env.workspace, memory_store=env.memory_dir)
    path = tmp_path / "b" / "memory" / "memory.sqlite"
    data = bytearray(path.read_bytes())
    data[-200] ^= 0xFF
    path.write_bytes(bytes(data))
    check = verify_backup(tmp_path / "b")
    assert not check.ok and any("SHA-256" in i for i in check.issues)
    with pytest.raises(BackupError, match="verification"):
        restore_backup(tmp_path / "b", tmp_path / "r")
    assert not (tmp_path / "r").exists()
    (tmp_path / "b" / MANIFEST_NAME).unlink()
    assert verify_backup(tmp_path / "b").issues == (
        "backup set has no manifest (incomplete or not a backup)",
    )


def test_backup_waits_for_the_writer_lock(env, tmp_path):
    from watchlist.review.locking import workspace_lock

    with workspace_lock(env.workspace, timeout=1):
        import threading

        errors = []

        def attempt():
            try:
                create_backup(tmp_path / "b", workspace=env.workspace, lock_timeout=0)
            except BackupError as exc:
                errors.append(exc)

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join()
    assert errors and errors[0].details["cause"] == "workspace_busy"


def test_anchor_detects_truncation_removed_cases_and_memory_rewind(env, tmp_path):
    service = ReviewService.open(env.workspace, make_config(), read_only=True)
    create_backup(
        tmp_path / "early", workspace=env.workspace, memory_store=env.memory_dir
    )
    env.review(["syn-005"])
    env.publish()  # memory grows after the early backup
    anchor = export_anchor(service, tmp_path / "anchor.json", memory=env.store)
    assert "not tamper-proof" in anchor["notice"]
    assert anchor["review"]["case_count"] == 4
    result = verify_anchor(tmp_path / "anchor.json", service, memory=env.store)
    assert result.ok and result.cases_checked == 4 and result.memory_checked
    with pytest.raises(BackupError, match="never overwritten"):
        export_anchor(service, tmp_path / "anchor.json")
    # New activity after the anchor is fine (chains only grow).
    env.review(["syn-004"])
    env.publish()
    assert verify_anchor(tmp_path / "anchor.json", service, memory=env.store).ok
    # Rewind: a memory backup taken BEFORE the anchor, restored, fails the anchor.
    restored = restore_backup(tmp_path / "early", tmp_path / "rewound")
    assert restored.ok
    old_memory = MemoryStore.open(
        tmp_path / "rewound" / "memory", memory_config(), read_only=True
    )
    rewound = verify_anchor(tmp_path / "anchor.json", service, memory=old_memory)
    assert not rewound.ok
    assert any("shorter than the anchor" in i for i in rewound.issues)
    # The rewound review workspace lacks the case created after the early backup.
    old_review = ReviewService.open(
        tmp_path / "rewound" / "review", make_config(), read_only=True
    )
    missing = verify_anchor(tmp_path / "anchor.json", old_review, memory=old_memory)
    assert any(i.endswith("case_not_found") for i in missing.issues)
    # An edited anchor file is reported too.
    data = json.loads((tmp_path / "anchor.json").read_text(encoding="utf-8"))
    data["review"]["cases"][0]["count"] = 1
    (tmp_path / "edited.json").write_text(json.dumps(data), encoding="utf-8")
    edited = verify_anchor(tmp_path / "edited.json", service, memory=env.store)
    assert any("self digest" in i for i in edited.issues)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: {"anchor_format": "review-anchor-v1"},
        lambda a: [1, 2],
        lambda a: {**a, "review": {**a["review"], "cases": [{"x": 1}]}},
        lambda a: {**a, "review": {**a["review"], "cases": [{"case_id": "c"}]}},
        lambda a: {**a, "review": {**a["review"], "cases": "not-a-list"}},
        lambda a: {**a, "review": [1]},
        lambda a: {**a, "memory": {"event_count": -1, "head_sha256": "0"}},
        lambda a: {**a, "memory": {"event_count": True, "head_sha256": "0"}},
        lambda a: {**a, "extra": 1},
    ],
)
def test_malformed_anchor_files_raise_backup_failed(env, tmp_path, mutate, capsys):
    """Regression: malformed anchors are a structured error, never a traceback."""
    from review_v2_support import config_dict
    from watchlist.review.backup import _main

    service = ReviewService.open(env.workspace, make_config(), read_only=True)
    anchor = export_anchor(service, tmp_path / "anchor.json", memory=env.store)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(mutate(anchor)), encoding="utf-8")
    with pytest.raises(BackupError) as raised:
        verify_anchor(bad, service, memory=env.store)
    assert raised.value.code == "backup_failed"
    config = tmp_path / "review.json"
    config.write_text(json.dumps(config_dict()), encoding="utf-8")
    rc = _main(
        [
            "verify-anchor",
            "--workspace",
            str(env.workspace),
            "--review-config",
            str(config),
            "--anchor",
            str(bad),
        ]
    )
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["error"] == "backup_failed"
