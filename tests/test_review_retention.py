"""Retention plan/apply drill across memory, backup sets and the v2 ledger."""

import json
import sqlite3

import pytest

from test_case_memory_support import Env
from review_v2_support import tree
from watchlist.review import retention
from watchlist.review.backup import BackupError, create_backup

NOW = "2027-03-15T00:00:00+00:00"  # > 365 days after the fictional base date
NOW_DAY = 406  # the fictional store clock day that reaches NOW


@pytest.fixture
def setup(tmp_path):
    env = Env(tmp_path / "live")
    env.review(["syn-001"], origin="human", day=0, reason="Fictional old note.")
    env.review(["syn-002"], origin="human", day=400, reason="Fictional recent note.")
    env.publish()
    env.clock.set_day(NOW_DAY)  # applying needs the store clock at the plan time
    backups = tmp_path / "backups"
    old = create_backup(
        backups / "old",
        memory_store=env.memory_dir,
        clock=lambda: "2026-01-01T00:00:00+00:00",
    )
    create_backup(backups / "recent", memory_store=env.memory_dir, clock=lambda: NOW)
    (backups / "stray").mkdir()  # not a backup set: never deleted automatically
    return env, backups, old


def test_plan_is_a_dry_run_and_inventories_every_copy(setup):
    env, backups, old = setup
    before = tree(env.memory_dir), tree(backups), tree(env.workspace)
    report = retention.plan(
        env.store.config.retention,
        memory=env.store,
        backups_root=backups,
        service=env.reader(),
        now=NOW,
    )
    assert (tree(env.memory_dir), tree(backups), tree(env.workspace)) == before
    assert len(report.memory_plan["candidates"]) == 1
    assert [b["backup_id"] for b in report.expired_backups] == [old.backup_id]
    assert report.unrecognized_backup_dirs == ("stray",)
    assert len(report.ledger_cases_past_retention) == 1
    assert report.ledger_status == "blocked_append_only"
    copies = " ".join(i["copy"] for i in report.inventory)
    for word in (
        "ledger",
        "checkpoints",
        "frozen memory contexts",
        "backup",
        "trained",
    ):
        assert word in copies


def test_apply_drill_deletes_planned_items_and_writes_a_verified_manifest(
    setup, tmp_path
):
    env, backups, old = setup
    policy = env.store.config.retention
    report = retention.plan(
        policy, memory=env.store, backups_root=backups, service=env.reader(), now=NOW
    )
    manifest_path = tmp_path / "deletion-manifest.json"
    result = retention.apply(
        report,
        policy,
        actor="fictional-maintainer",
        reason="Fictional retention drill.",
        manifest_path=manifest_path,
        memory=env.store,
        backups_root=backups,
        service=env.reader(),
    )
    assert result["verified"], result["issues"]
    assert not (backups / "old").exists()
    assert (backups / "recent").exists() and (backups / "stray").exists()
    assert result["memory"]["rows_deleted"]["annotations"] == 1
    assert env.store.verify().ok and env.store.verify().tombstone_count == 1
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["plan_id"] == report.plan_id
    assert "Fictional old note." not in manifest_path.read_text(encoding="utf-8")
    assert "not secure erasure" in saved["erasure_notice"]
    assert saved["ledger_status"] == "blocked_append_only"
    # The ledger case is untouched: still readable through the service.
    case_id = report.ledger_cases_past_retention[0]
    assert env.reader().get_case(case_id).status == "completed"
    # Re-applying the same plan is refused as stale; a manifest is never overwritten.
    with pytest.raises(BackupError):
        retention.apply(
            report,
            policy,
            actor="m",
            reason="again",
            manifest_path=tmp_path / "m2.json",
            memory=env.store,
            backups_root=backups,
            service=env.reader(),
        )


def _cli_config(env, tmp_path, retention_days: int):
    data = env.store.config.to_dict()
    data["retention"]["annotation_retention_days"] = retention_days
    path = tmp_path / "live" / "cfg.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_cli_plan_and_confirmed_apply(setup, tmp_path, capsys):
    """The CLI uses the real UTC clock; the plan time below is in the past."""
    env, backups, _ = setup
    past = "2026-06-01T00:00:00+00:00"  # day-0 note is > 100 days old here
    common = [
        "--memory-config",
        str(_cli_config(env, tmp_path, 100)),
        "--memory",
        str(env.memory_dir),
        "--backups",
        str(backups),
        "--now",
        past,
    ]
    assert retention._main(["plan", *common]) == 0
    planned = json.loads(capsys.readouterr().out)
    plan_id = planned["plan_id"]
    assert len(planned["memory_plan"]["candidates"]) == 1
    args = [
        "apply",
        *common,
        "--actor",
        "m",
        "--reason",
        "Fictional drill.",
        "--manifest",
        str(tmp_path / "cli-manifest.json"),
    ]
    assert retention._main([*args, "--confirm-plan-id", "0" * 64]) == 2
    assert "plan_id_mismatch" in capsys.readouterr().out
    assert (backups / "old").exists()
    assert retention._main([*args, "--confirm-plan-id", plan_id]) == 0
    assert not (backups / "old").exists()


def test_cli_apply_refuses_a_future_plan_and_deletes_nothing(setup, tmp_path, capsys):
    """Regression: ``--now`` in the future only forecasts; apply refuses it."""
    env, backups, _ = setup
    common = [
        "--memory-config",
        str(_cli_config(env, tmp_path, 365)),
        "--memory",
        str(env.memory_dir),
        "--backups",
        str(backups),
        "--now",
        "2199-01-01T00:00:00+00:00",
    ]
    assert retention._main(["plan", *common]) == 0
    plan_id = json.loads(capsys.readouterr().out)["plan_id"]
    manifest = tmp_path / "future-manifest.json"
    rc = retention._main(
        [
            "apply",
            *common,
            "--confirm-plan-id",
            plan_id,
            "--actor",
            "m",
            "--reason",
            "Fictional misuse.",
            "--manifest",
            str(manifest),
        ]
    )
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["error"] == "retention_invalid"
    assert not manifest.exists()
    assert (backups / "old").exists() and (backups / "recent").exists()
    verification = env.store.verify()
    assert verification.annotation_count == 2 and verification.tombstone_count == 0


def test_unusable_manifest_path_is_refused_before_any_deletion(setup, tmp_path):
    """Regression: a missing manifest directory must not delete first and crash later."""
    env, backups, _ = setup
    policy = env.store.config.retention
    report = retention.plan(policy, memory=env.store, backups_root=backups, now=NOW)
    with pytest.raises(BackupError, match="nothing was deleted") as raised:
        retention.apply(
            report,
            policy,
            actor="m",
            reason="Fictional drill.",
            manifest_path=tmp_path / "missing-dir" / "m.json",
            memory=env.store,
            backups_root=backups,
        )
    assert raised.value.code == "backup_failed"
    assert (backups / "old").exists()
    verification = env.store.verify()
    assert verification.annotation_count == 2 and verification.tombstone_count == 0


def test_failure_after_manifest_creation_is_recorded_in_the_manifest(
    setup, tmp_path, monkeypatch
):
    from watchlist.case_memory import lifecycle
    from watchlist.case_memory.state import RetentionError

    env, backups, _ = setup
    policy = env.store.config.retention
    report = retention.plan(policy, memory=env.store, backups_root=backups, now=NOW)

    def broken(*args, **kwargs):
        raise RetentionError("fictional injected failure")

    monkeypatch.setattr(lifecycle, "apply_retention", broken)
    path = tmp_path / "failed-manifest.json"
    with pytest.raises(RetentionError):
        retention.apply(
            report,
            policy,
            actor="m",
            reason="Fictional drill.",
            manifest_path=path,
            memory=env.store,
            backups_root=backups,
        )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["verified"] is False
    assert saved["failed"]["error"] == "retention_invalid"
    assert saved["plan_id"] == report.plan_id
    db = sqlite3.connect(env.memory_dir / "memory.sqlite")
    try:
        assert db.execute("SELECT COUNT(*) FROM tombstones").fetchone()[0] == 0
    finally:
        db.close()
