"""Outbox publication: projection, idempotency, conflicts and real crash windows."""

import json
import sqlite3
import subprocess
from uuid import uuid4

import pytest

from test_case_memory_support import (
    CRASH_CODE,
    SNAP_ALT,
    SNAP_MAIN,
    SNAP_UNREGISTERED,
    Env,
    memory_config,
    worker_command,
    worker_env,
)
from watchlist.case_memory.publisher import Publisher, annotation_from_case
from watchlist.case_memory.state import MemoryInputError, UnregisteredIdentifierError
from watchlist.case_memory.store import DB_NAME, MemoryStore, sidecars


def counts(env):
    db = sqlite3.connect(env.memory_dir / DB_NAME)
    try:
        return {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "publication_intents",
                "annotations",
                "annotation_records",
                "memory_events",
                "quarantine",
            )
        }
    finally:
        db.close()


def test_projection_keeps_exact_ids_relations_and_no_query_text(tmp_path):
    env = Env(tmp_path)
    view = env.review(["syn-012", "syn-001"], selected="syn-012", origin="human")
    annotation = annotation_from_case(view, "ws-test", env.store.registry)
    assert annotation.origin == "human"
    assert annotation.snapshot_sha256 == SNAP_MAIN
    assert [(r.source_entity_id, r.relation, r.rank) for r in annotation.records] == [
        ("syn-012", "selected_for_follow_up", 1),
        ("syn-001", "considered_without_selection", 2),
    ]
    payload = json.dumps(annotation.to_dict())
    assert view.header.name not in payload  # the query text is not projected
    assert "Luma Vesper" not in payload  # neither are alias or display names


def test_only_completed_cases_are_projected(tmp_path):
    env = Env(tmp_path)
    pending = env.start(["syn-001"])
    with pytest.raises(MemoryInputError):
        annotation_from_case(pending, "ws-test", env.store.registry)
    report = env.publish()
    assert (report.scanned, report.not_completed, report.published) == (1, 1, 0)


def test_publish_is_idempotent_and_returns_the_same_receipt(tmp_path):
    env = Env(tmp_path)
    view = env.review(["syn-001", "syn-002"])
    first = env.publish()
    assert (first.enqueued, first.published, first.pending) == (1, 1, 0)
    publisher = env.publisher()
    receipt = publisher.receipt(view.case_id)
    before = counts(env)
    again = env.publish()
    assert (again.enqueued, again.already_enqueued, again.published) == (0, 1, 0)
    assert publisher.receipt(view.case_id) == receipt
    assert counts(env) == before
    assert env.store.verify().ok


def test_unregistered_snapshot_and_foreign_source_are_quarantined(tmp_path):
    env = Env(tmp_path, sources={SNAP_MAIN: ("fictional-alt-v1", "alt")})
    env.review(["syn-001"])  # main snapshot, but evidence labelled with the alt source
    env.review(["syn-001"], snapshot=SNAP_UNREGISTERED)
    report = env.publish()
    assert report.published == 0
    assert sorted(q["reason_code"] for q in report.quarantined) == [
        "memory_unregistered_identifier",
        "memory_unregistered_identifier",
    ]
    again = env.publish()  # quarantine is recorded once
    assert counts(env)["quarantine"] == 2
    assert len(again.quarantined) == 2
    with pytest.raises(UnregisteredIdentifierError):
        view = env.reader().get_case(env.reader().list_cases()[0].case_id)
        annotation_from_case(view, "ws-test", env.store.registry)


def test_same_case_under_a_second_workspace_label_is_quarantined(tmp_path):
    """A copied/restored workspace registered under another label cannot duplicate."""
    env = Env(tmp_path)
    env.review(["syn-001"])
    env.publish()
    other = Publisher(env.store, env.reader(), "ws-copy").publish()
    assert other.published == 0
    assert [q["reason_code"] for q in other.quarantined] == [
        "case_published_from_other_workspace"
    ]
    assert counts(env)["annotations"] == 1


def test_alt_namespace_projection_uses_its_own_namespace(tmp_path):
    env = Env(tmp_path)
    view = env.review(["syn-001"], snapshot=SNAP_ALT)
    annotation = annotation_from_case(view, "ws-test", env.store.registry)
    assert annotation.namespace_id == "fictional-alt-v1"
    assert (
        annotation.records[0].record_key
        != annotation_from_case(env.review(["syn-001"]), "ws-test", env.store.registry)
        .records[0]
        .record_key
    )


def run_worker(env, stage):
    return subprocess.run(
        worker_command("publish", str(env.workspace), str(env.memory_dir), stage),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=worker_env(),
        timeout=180,
    )


@pytest.mark.parametrize(
    "stage, pending_after_crash",
    [("after_enqueue", 1), ("before_publish_commit", 2), ("after_publish_commit", 1)],
)
def test_real_crash_windows_lose_nothing_and_duplicate_nothing(
    tmp_path, stage, pending_after_crash
):
    env = Env(tmp_path)
    first = env.review(["syn-001", "syn-012"])
    second = env.review(["syn-012"], outcome="unresolved")
    crashed = run_worker(env, stage)
    assert crashed.returncode == CRASH_CODE, crashed.stderr
    if stage == "before_publish_commit":
        # Died inside the transaction: a hot journal remains until a writable open.
        assert sidecars(env.memory_dir)
    store = MemoryStore.open(
        env.memory_dir, memory_config()
    )  # rolls back any hot journal
    assert not sidecars(env.memory_dir)
    verification = store.verify()
    assert verification.ok, verification.issues
    published = counts(env)["annotations"]
    intents = counts(env)["publication_intents"]
    assert intents - published == pending_after_crash
    # Retry in a fresh process: everything published exactly once.
    retried = run_worker(env, "none")
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout)["ok"]
    final = counts(env)
    assert final["publication_intents"] == final["annotations"] == 2
    assert final["annotation_records"] == 3
    assert env.store.verify().ok
    receipts = {
        env.publisher().receipt(v.case_id)["annotation_id"] for v in (first, second)
    }
    assert len(receipts) == 2
    # Key revisions count each publication once (syn-012 appears in both cases).
    db = sqlite3.connect(env.memory_dir / DB_NAME)
    revisions = sorted(r[0] for r in db.execute("SELECT revision FROM key_versions"))
    db.close()
    assert revisions == [1, 2]


def _review_with_submission(env, candidates, submission_id):
    view = env.start(candidates, origin="test")
    return env.service().submit_review(
        view.case_id,
        {
            "submission_id": submission_id,
            "packet_sha256": view.packet_sha256,
            "outcome": "unresolved",
            "candidate_entity_id": None,
            "reason": "Fictional note.",
            "actor": "fictional-reviewer",
            "origin": "test",
        },
    )


def test_submission_id_reused_across_workspaces_is_quarantined_not_blocking(tmp_path):
    """Regression: v2 enforces submission IDs per workspace only."""
    a, b = Env(tmp_path / "a"), Env(tmp_path / "b")
    shared = str(uuid4())
    _review_with_submission(a, ["syn-003"], shared)
    duplicate = _review_with_submission(b, ["syn-003"], shared)
    assert Publisher(a.store, a.reader(), "ws-a").publish().published == 1
    other = Publisher(a.store, b.reader(), "ws-b").publish()
    assert other.published == 0 and other.blocked == 0
    assert [q["reason_code"] for q in other.quarantined] == ["duplicate_submission_id"]
    later = a.review(["syn-004"], origin="test")
    report = Publisher(a.store, a.reader(), "ws-a").publish()
    assert report.published == 1 and report.pending == 0
    assert Publisher(a.store, a.reader(), "ws-a").receipt(later.case_id) is not None
    assert Publisher(a.store, b.reader(), "ws-b").receipt(duplicate.case_id) is None
    verification = a.store.verify()
    assert verification.ok
    assert verification.pending_intents == 0 and verification.blocked_intents == 0


def test_unpublishable_intent_is_quarantined_and_drain_continues(tmp_path, monkeypatch):
    """Regression: one bad outbox entry must not stop later publications."""
    from watchlist.case_memory import publisher as publisher_module

    a, b = Env(tmp_path / "a"), Env(tmp_path / "b")
    shared = str(uuid4())
    _review_with_submission(a, ["syn-003"], shared)
    blocked_view = _review_with_submission(b, ["syn-003"], shared)
    Publisher(a.store, a.reader(), "ws-a").publish()
    # Simulate an outbox row written before the enqueue-time check existed.
    monkeypatch.setattr(publisher_module, "_submission_in_use", lambda *a: False)
    first = Publisher(a.store, b.reader(), "ws-b").publish()
    monkeypatch.undo()
    assert first.published == 0 and first.pending == 0 and first.blocked == 1
    assert [q["reason_code"] for q in first.quarantined] == ["publication_blocked"]
    later = a.review(["syn-004"], origin="test")
    report = Publisher(a.store, a.reader(), "ws-a").publish()
    assert report.published == 1 and report.pending == 0 and report.blocked == 1
    assert Publisher(a.store, a.reader(), "ws-a").receipt(later.case_id) is not None
    assert Publisher(a.store, b.reader(), "ws-b").receipt(blocked_view.case_id) is None
    events_before = counts(a)["memory_events"]
    again = Publisher(a.store, a.reader(), "ws-a").drain()
    assert again["published"] == 0 and again["newly_blocked"] == []
    assert counts(a)["memory_events"] == events_before  # no retry storm, no new events
    verification = a.store.verify()
    assert verification.ok
    assert verification.pending_intents == 0 and verification.blocked_intents == 1
    context = a.start(["syn-003"], origin="test", memory=True).packet.context
    data = context["case_memory"]["data"]
    assert data["pending_publications"] == 0 and data["blocked_publications"] == 1
    assert any("could not be published" in w for w in data["warnings"])
