"""Crash-window recovery for the v2 workflow, with real process termination.

Each crash test terminates a separate Python process with ``os._exit(77)``
right after the named durable action, so no context manager or ``finally``
block runs. The parent then reopens the workspace and recovers. Once saved
evidence exists the parent uses a retriever that raises if called.
"""

import json
import subprocess
import sys

import pytest

from review_v2_support import (
    CRASH_CODE,
    FakeRetriever,
    make_config,
    submission,
    tree,
    worker_command,
    worker_env,
)
from watchlist.review import contracts as c
from watchlist.review.recovery import CheckpointFacts, assess_case, plan_recovery
from watchlist.review.service import ReviewService
from watchlist.review.storage import Ledger, connect

FULL_EVENTS = [
    "case_created",
    "evidence_recorded",
    "review_ready",
    "review_submitted",
    "case_finalized",
]


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "crash workspace"
    ReviewService.initialize(root, make_config(), retriever=FakeRetriever())
    return root


def run_worker(*args, expect=CRASH_CODE):
    result = subprocess.run(
        worker_command(*args),
        capture_output=True,
        text=True,
        timeout=180,
        env=worker_env(),
    )
    assert result.returncode == expect, result.stderr[-2000:]
    return result


def events(service, case_id):
    return [event.event_type for event in service.audit(case_id).events]


def only_case(service):
    (summary,) = service.list_cases()
    return summary.case_id


@pytest.mark.parametrize(
    "stage, events_after_crash, retrievals",
    [
        ("after_case", ["case_created"], 1),
        ("after_evidence", ["case_created", "evidence_recorded"], 0),
        ("after_packet", ["case_created", "evidence_recorded", "review_ready"], 0),
    ],
)
def test_start_crash_windows_recover_to_the_review_interrupt(
    workspace, stage, events_after_crash, retrievals
):
    run_worker("start", str(workspace), stage)
    reader = ReviewService.open(workspace, make_config(), read_only=True)
    case_id = only_case(reader)
    crashed = reader.get_case(case_id)
    assert (crashed.status, crashed.next_action, crashed.recovery_required) == (
        "preparing",
        "recover",
        True,
    )
    assert events(reader, case_id) == events_after_crash
    retriever = FakeRetriever(fail=retrievals == 0)
    service = ReviewService.open(workspace, make_config(), retriever=retriever)
    with pytest.raises(c.RecoveryRequiredError):
        service.submit_review(case_id, {**submission(crashed, packet_sha256="0" * 64)})
    recovered = service.recover(case_id)
    assert recovered.status == "awaiting_review"
    assert retriever.calls == retrievals
    assert events(service, case_id) == [
        "case_created",
        "evidence_recorded",
        "review_ready",
    ]
    # Recovery is idempotent and a pending review stays pending.
    assert (
        ReviewService.open(
            workspace, make_config(), retriever=FakeRetriever(fail=True)
        ).recover(case_id)
        == recovered
    )
    done = service.submit_review(case_id, submission(recovered))
    assert done.status == "completed" and events(service, case_id) == FULL_EVENTS
    assert service.verify_audit(case_id).ok


@pytest.mark.parametrize(
    "stage, saved_events",
    [
        ("after_submission", FULL_EVENTS[:4]),
        ("during_explanation", FULL_EVENTS[:4]),
        ("after_final_audit", FULL_EVENTS),
    ],
)
def test_review_crash_windows_finish_from_the_accepted_review(
    workspace, tmp_path, stage, saved_events
):
    service = ReviewService.open(workspace, make_config(), retriever=FakeRetriever())
    view = service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    form = submission(view)
    form_path = tmp_path / "form.json"
    form_path.write_text(json.dumps(form), encoding="utf-8", newline="\n")
    run_worker("review", str(workspace), stage, view.case_id, str(form_path))

    reader = ReviewService.open(workspace, make_config(), read_only=True)
    crashed = reader.get_case(view.case_id)
    assert (crashed.status, crashed.next_action, crashed.recovery_required) == (
        "finalizing",
        "recover",
        True,
    )
    assert crashed.review.submission.to_dict() == form
    assert events(reader, view.case_id) == saved_events

    safe = ReviewService.open(
        workspace, make_config(), retriever=FakeRetriever(fail=True)
    )
    # A lost response may be retried with the same form: no second effect.
    assert safe.submit_review(view.case_id, form).status == "finalizing"
    with pytest.raises(c.StaleSubmissionError):
        safe.submit_review(view.case_id, submission(view))
    done = safe.recover(view.case_id)
    assert done.status == "completed"
    assert done.review == crashed.review
    assert events(safe, view.case_id) == FULL_EVENTS
    assert safe.verify_audit(view.case_id).ok
    if stage == "after_final_audit":
        assert done.final_receipt == crashed.final_receipt
    before = tree(workspace)
    assert safe.recover(view.case_id) == done
    assert tree(workspace) == before


def test_recovery_before_evidence_requires_the_frozen_retriever(workspace):
    run_worker("start", str(workspace), "after_case")
    case_id = only_case(ReviewService.open(workspace, make_config(), read_only=True))
    other = ReviewService.open(
        workspace, make_config(), retriever=FakeRetriever(tag="replacement-model")
    )
    before = tree(workspace)
    with pytest.raises(c.RetrieverMismatchError):
        other.recover(case_id)
    assert other.get_case(case_id).status == "preparing"
    assert len(tree(workspace)) == len(before)
    retriever = FakeRetriever()
    assert (
        ReviewService.open(workspace, make_config(), retriever=retriever)
        .recover(case_id)
        .status
        == "awaiting_review"
    )
    assert retriever.calls == 1


def test_in_process_failure_after_evidence_never_retrieves_again(workspace):
    def fail(stage):
        if stage == "after_evidence":
            raise RuntimeError("simulated storage outage")

    retriever = FakeRetriever()
    service = ReviewService.open(
        workspace, make_config(), retriever=retriever, failpoint=fail
    )
    with pytest.raises(c.RecoveryRequiredError) as caught:
        service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    case_id = caught.value.details["case_id"]
    assert service.get_case(case_id).status == "preparing"
    # The replayed retrieve node finds ledger evidence and skips the retriever
    # (and therefore the failpoint that follows a fresh retrieval).
    assert service.recover(case_id).status == "awaiting_review"
    assert service.recover(case_id).status == "awaiting_review"
    assert retriever.calls == 1
    assert events(service, case_id) == [
        "case_created",
        "evidence_recorded",
        "review_ready",
    ]


def _facts_for(workspace, case_id, **facts):
    db = connect(workspace, read_only=True)
    try:
        snapshot = Ledger(db).snapshot(case_id)
    finally:
        db.close()
    return snapshot, CheckpointFacts(**facts)


def test_plan_recovery_rules(workspace):
    service = ReviewService.open(workspace, make_config(), retriever=FakeRetriever())
    view = service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    identity = service.workflow_identity
    snapshot, _ = _facts_for(workspace, view.case_id, exists=False)
    # Durable evidence without any checkpoint is a contradiction, not a fresh start.
    missing = assess_case(snapshot, CheckpointFacts(exists=False), identity)
    assert missing.status == "inconsistent"
    with pytest.raises(c.StorageIntegrityError):
        plan_recovery(missing, CheckpointFacts(exists=False))
    pending = CheckpointFacts(
        exists=True,
        next=("human_review",),
        interrupted=True,
        values={
            "case_id": view.case_id,
            "packet_sha256": view.packet_sha256,
            "review_status": "pending",
        },
    )
    assessment = assess_case(snapshot, pending, identity)
    assert assessment.status == "awaiting_review"
    assert plan_recovery(assessment, pending) == "none"
    saved_result = CheckpointFacts(
        exists=True,
        next=("human_review",),
        interrupted=True,
        has_task_results=True,
        values=pending.values,
    )
    assert (
        plan_recovery(assess_case(snapshot, saved_result, identity), saved_result)
        == "continue"
    )
    foreign = CheckpointFacts(
        exists=True,
        next=("explain",),
        values={"case_id": view.case_id, "packet_sha256": "0" * 64},
    )
    assert assess_case(snapshot, foreign, identity).status == "inconsistent"
    other_identity = c.WorkflowIdentity("review-workflow-v2.1", identity.config_sha256)
    incompatible = assess_case(snapshot, pending, other_identity)
    assert incompatible.status == "incompatible"
    with pytest.raises(c.VersionMismatchError):
        plan_recovery(incompatible, pending)


def test_checkpoint_rollback_against_ledger_progress_is_inconsistent(workspace):
    """Deleting the checkpoint history of a reviewed case must not restart it."""
    service = ReviewService.open(workspace, make_config(), retriever=FakeRetriever())
    view = service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    import sqlite3

    db = sqlite3.connect(workspace / "review.sqlite")
    db.execute("DELETE FROM checkpoints")
    db.execute("DELETE FROM writes")
    db.commit()
    db.close()
    retriever = FakeRetriever(fail=True)
    rolled_back = ReviewService.open(workspace, make_config(), retriever=retriever)
    assert rolled_back.get_case(view.case_id).status == "inconsistent"
    with pytest.raises(c.StorageIntegrityError):
        rolled_back.recover(view.case_id)
    assert retriever.calls == 0


HOT_JOURNAL_WRITER = r"""
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute("PRAGMA cache_size=10")
db.execute("BEGIN IMMEDIATE")
for i in range(200):
    db.execute("INSERT INTO workspace_meta VALUES (?, ?)", ("fictional-junk-%d" % i, "x" * 20000))
os._exit(9)
"""


def test_crash_inside_a_transaction_is_rolled_back_only_by_a_writable_open(workspace):
    service = ReviewService.open(workspace, make_config(), retriever=FakeRetriever())
    view = service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            HOT_JOURNAL_WRITER,
            str(workspace / "review.sqlite"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 9
    assert (workspace / "review.sqlite-journal").exists()
    before = tree(workspace)
    with pytest.raises(c.RecoveryRequiredError):
        ReviewService(workspace, make_config(), read_only=True).get_case(view.case_id)
    with pytest.raises(c.RecoveryRequiredError):
        ReviewService.open(workspace, make_config(), read_only=True)
    assert tree(workspace) == before
    reopened = ReviewService.open(workspace, make_config())
    assert not (workspace / "review.sqlite-journal").exists()
    assert reopened.get_case(view.case_id).status == "awaiting_review"
    db = connect(workspace, read_only=True)
    try:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM workspace_meta WHERE key LIKE 'fictional-junk-%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()


def test_non_hot_journal_is_settled_by_writable_open(workspace):
    writer = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('BEGIN IMMEDIATE')
connection.execute("INSERT INTO workspace_meta VALUES ('fictional-small-crash', 'uncommitted')")
os._exit(9)
"""
    process = subprocess.run(
        [sys.executable, "-B", "-c", writer, str(workspace / "review.sqlite")],
        timeout=60,
    )
    assert process.returncode == 9
    journal = workspace / "review.sqlite-journal"
    assert journal.exists()
    with pytest.raises(c.RecoveryRequiredError):
        ReviewService.open(workspace, make_config(), read_only=True)
    ReviewService.open(workspace, make_config())
    assert not journal.exists()
    reader = ReviewService.open(workspace, make_config(), read_only=True)
    assert reader.list_cases() == []
    db = connect(workspace, read_only=True)
    try:
        assert (
            db.execute(
                "SELECT value FROM workspace_meta WHERE key='fictional-small-crash'"
            ).fetchone()
            is None
        )
    finally:
        db.close()
