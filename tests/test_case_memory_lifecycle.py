"""Corrections/supersession (append-only) and memory retention deletion drills."""

import json
import sqlite3
import subprocess

import pytest

from test_case_memory_support import (
    CRASH_CODE,
    SNAP_ALT,
    SNAP_MAIN,
    Env,
    context_keys,
    memory_config,
    worker_command,
    worker_env,
)
from watchlist.case_memory.identifiers import RecordRef
from watchlist.case_memory.lifecycle import (
    apply_retention,
    plan_retention,
    supersede,
    withdraw,
)
from watchlist.case_memory.lookup import get_annotation, inspect_history
from watchlist.case_memory.state import (
    CorrectionError,
    MemoryInputError,
    NotFoundMemoryError,
    RetentionError,
)
from watchlist.case_memory.store import DB_NAME, MemoryStore


def annotation_id(env, view):
    return env.publisher().receipt(view.case_id)["annotation_id"]


def test_withdrawal_hides_future_display_but_keeps_history(tmp_path):
    env = Env(tmp_path)
    original = env.review(
        ["syn-001"], origin="human", day=0, reason="Fictional original."
    )
    env.publish()
    aid = annotation_id(env, original)
    row = withdraw(
        env.store, aid, actor="fictional-maintainer", reason="Fictional mistake."
    )
    assert row["kind"] == "withdrawal"
    # Identical replay is idempotent; a different correction conflicts.
    assert (
        withdraw(
            env.store, aid, actor="fictional-maintainer", reason="Fictional mistake."
        )
        == row
    )
    with pytest.raises(CorrectionError):
        withdraw(env.store, aid, actor="fictional-maintainer", reason="Other reason.")
    key = context_keys(env.start(["syn-001"], origin="human", day=1, memory=True))[
        "syn-001"
    ]
    assert key["cards"] == [] and key["omitted"]["withdrawn"] == 1
    detail = get_annotation(env.store, aid)
    assert detail["annotation"]["reason"] == "Fictional original."  # original preserved
    assert detail["corrections"][0]["kind"] == "withdrawal"
    history = inspect_history(
        env.store,
        RecordRef(
            "fictional-synthetic-v1",
            "syn-001",
            original.packet.retrieval.retriever.reference_sha256,
        ),
    )
    assert [e["status"] for e in history["entries"]] == ["withdrawn"]
    assert env.store.verify().ok


def test_supersession_links_original_to_replacement_and_rejects_merges(tmp_path):
    env = Env(tmp_path)
    original = env.review(
        ["syn-002", "syn-003"], outcome="none_selected", origin="human", day=0
    )
    replacement = env.review(["syn-002"], origin="human", day=2)
    unrelated = env.review(["syn-004"], origin="human", day=3)
    demo = env.review(["syn-002"], origin="simulated_demo", day=3)
    other_ns = env.review(["syn-002"], origin="human", day=3, snapshot=SNAP_ALT)
    env.publish()
    a, r = annotation_id(env, original), annotation_id(env, replacement)
    for bad in (unrelated, other_ns):
        with pytest.raises(CorrectionError, match="no exact record"):
            supersede(env.store, a, annotation_id(env, bad), actor="m", reason="x")
    with pytest.raises(CorrectionError, match="origin"):
        supersede(env.store, a, annotation_id(env, demo), actor="m", reason="x")
    with pytest.raises(CorrectionError, match="after the original"):
        supersede(env.store, r, a, actor="m", reason="x")
    with pytest.raises(CorrectionError, match="itself"):
        supersede(env.store, a, a, actor="m", reason="x")
    supersede(
        env.store,
        a,
        r,
        actor="fictional-maintainer",
        reason="Fictional linked correction.",
    )
    keys = context_keys(
        env.start(["syn-002", "syn-003"], origin="human", day=4, memory=True)
    )
    assert [c["case_id"] for c in keys["syn-002"]["cards"]] == [replacement.case_id]
    assert keys["syn-002"]["cards"][0]["supersedes_annotation_id"] == a
    assert (
        keys["syn-003"]["cards"] == [] and keys["syn-003"]["omitted"]["superseded"] == 1
    )
    assert env.store.verify().ok


def test_correction_inputs_are_validated(tmp_path):
    env = Env(tmp_path)
    view = env.review(["syn-001"])
    env.publish()
    aid = annotation_id(env, view)
    with pytest.raises(MemoryInputError):
        withdraw(env.store, aid, actor="", reason="x")
    with pytest.raises(MemoryInputError):
        withdraw(env.store, aid, actor="m", reason="‮flipped")
    with pytest.raises(NotFoundMemoryError):
        withdraw(env.store, "0" * 64, actor="m", reason="x")


def test_retention_drill_deletes_with_tombstones_and_blocks_republication(tmp_path):
    env = Env(tmp_path)
    old = env.review(
        ["syn-001", "syn-012"], origin="human", day=0, reason="Fictional old note."
    )
    newer = env.review(["syn-001"], origin="human", day=200)
    env.publish()
    old_id = annotation_id(env, old)
    withdraw(env.store, old_id, actor="m", reason="Fictional withdrawal before expiry.")
    now = "2027-02-10T10:00:00+00:00"  # > 365 days after day 0, < 365 after day 200
    plan = plan_retention(env.store, now=now)
    assert [c["annotation_id"] for c in plan.candidates] == [old_id]
    before = MemoryStore.open(env.memory_dir, memory_config(), read_only=True).verify()
    env.clock.set_day(373)  # the store clock reaches the plan time (2027-02-10)
    manifest = apply_retention(
        env.store,
        plan,
        actor="fictional-maintainer",
        reason="Fictional retention drill.",
    )
    assert manifest.verified, manifest.issues
    assert manifest.rows_deleted == {
        "corrections": 1,
        "annotation_records": 2,
        "annotations": 1,
        "publication_intents": 1,
    }
    assert any("append-only" in copy for copy in manifest.copies_not_deleted)
    db = sqlite3.connect(env.memory_dir / DB_NAME)
    raw = db.execute("SELECT group_concat(payload_json) FROM memory_events").fetchone()[
        0
    ]
    tomb = db.execute("SELECT * FROM tombstones").fetchall()
    db.close()
    assert "Fictional old note." not in raw  # events never held the note
    assert len(tomb) == 1 and "Fictional" not in json.dumps(tomb)
    after = env.store.verify()
    assert after.ok and after.tombstone_count == 1
    assert after.event_count > before.event_count
    with pytest.raises(NotFoundMemoryError, match="retention"):
        get_annotation(env.store, old_id)
    # The completed case still exists in the append-only ledger, but is never republished.
    report = env.publish()
    assert report.tombstoned == 1 and report.published == 0
    key = context_keys(env.start(["syn-001"], origin="human", day=201, memory=True))[
        "syn-001"
    ]
    assert [c["case_id"] for c in key["cards"]] == [newer.case_id]
    # A second application of the same plan is stale.
    with pytest.raises(RetentionError):
        apply_retention(env.store, plan, actor="m", reason="again")


def test_stale_retention_plan_is_refused(tmp_path):
    env = Env(tmp_path)
    env.review(["syn-001"], day=0)
    env.publish()
    plan = plan_retention(env.store, now="2027-03-01T00:00:00+00:00")
    env.review(["syn-002"], day=1)
    env.publish()  # a new expired annotation appears after planning
    env.clock.set_day(392)  # the store clock reaches the plan time (2027-03-01)
    with pytest.raises(RetentionError, match="stale"):
        apply_retention(env.store, plan, actor="m", reason="x")


def test_crash_during_retention_deletes_nothing(tmp_path):
    env = Env(tmp_path)
    env.review(["syn-001"], day=0)
    env.publish()
    now = "2027-03-01T00:00:00+00:00"
    result = subprocess.run(
        worker_command(
            "retention",
            str(env.workspace),
            str(env.memory_dir),
            "during_retention",
            now,
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=worker_env(),
        timeout=180,
    )
    assert result.returncode == CRASH_CODE, result.stderr
    store = MemoryStore.open(env.memory_dir, memory_config())
    verification = store.verify()
    assert verification.ok and verification.annotation_count == 1
    assert verification.tombstone_count == 0
    retry = subprocess.run(
        worker_command(
            "retention", str(env.workspace), str(env.memory_dir), "none", now
        ),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=worker_env(),
        timeout=180,
    )
    assert json.loads(retry.stdout)["result"]["verified"]
    assert store.verify().tombstone_count == 1


def test_future_retention_plan_is_a_forecast_and_deletes_nothing(tmp_path):
    """Regression: a plan dated after the store clock must never be applied."""
    env = Env(tmp_path)
    view = env.review(["syn-001"], origin="human", day=0)
    env.publish()
    forecast = plan_retention(env.store, now="2199-01-01T00:00:00+00:00")
    assert len(forecast.candidates) == 1  # forecasting is allowed (dry run)
    with pytest.raises(RetentionError, match="future") as raised:
        apply_retention(env.store, forecast, actor="m", reason="Fictional misuse.")
    assert raised.value.code == "retention_invalid"
    verification = env.store.verify()
    assert verification.annotation_count == 1 and verification.tombstone_count == 0
    assert env.publisher().receipt(view.case_id) is not None
    # Within the clock-skew allowance a plan is still applicable.
    env.clock.set_day(373)
    current = plan_retention(env.store, now="2027-02-10T10:04:00+00:00")
    assert apply_retention(env.store, current, actor="m", reason="Drill.").verified


def test_inspect_history_rejects_bad_pagination_with_a_structured_error(tmp_path):
    env = Env(tmp_path)
    ref = RecordRef("fictional-synthetic-v1", "syn-001", SNAP_MAIN)
    for bad in ({"limit": 0}, {"limit": 10_000}, {"offset": -1}, {"limit": "5"}):
        with pytest.raises(MemoryInputError) as raised:
            inspect_history(env.store, ref, **bad)
        assert raised.value.code == "memory_invalid_input"
