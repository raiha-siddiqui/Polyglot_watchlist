"""v2 review service lifecycle, workspace modes, idempotency, versions and audit."""

import json
import sqlite3

import pytest

from review_v2_support import (
    FakeRetriever,
    StaticContext,
    make_config,
    submission,
    tree,
)
from watchlist.review import contracts as c
from watchlist.review.contracts import AuditAnchor, CaseView, ReviewSubmission
from watchlist.review.service import ReviewService
from watchlist.review.storage import DB_NAME


@pytest.fixture
def workspace(tmp_path):
    return tmp_path / "review-v2"


def new_service(workspace, retriever=None, **options):
    return ReviewService.initialize(
        workspace, make_config(), retriever=retriever or FakeRetriever(), **options
    )


def awaiting(workspace, **options):
    service = new_service(workspace, **options)
    return service, service.start_case(
        "  LUMA   VESPER ", origin="test", actor="fictional-analyst"
    )


def event_types(service, case_id):
    return [event.event_type for event in service.audit(case_id).events]


def with_trigger_bypass(path, sql, triggers):
    """Simulate an owner who drops append-only triggers, edits, then restores them."""
    db = sqlite3.connect(path / DB_NAME)
    saved = [
        db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
        for name in triggers
    ]
    for name in triggers:
        db.execute(f"DROP TRIGGER {name}")
    db.execute(sql[0], sql[1])
    for statement in saved:
        db.execute(statement)
    db.commit()
    db.close()


# -- lifecycle ---------------------------------------------------------------


def test_full_lifecycle_with_default_rapidfuzz_retriever(workspace):
    service = ReviewService.initialize(workspace, make_config())
    view = service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    assert (view.status, view.next_action, view.recovery_required) == (
        "awaiting_review",
        "submit_review",
        False,
    )
    candidates = view.packet.retrieval.candidates
    # The packaged fictional data deliberately has two records sharing this name.
    assert [c.entity_id for c in candidates[:2]] == ["syn-001", "syn-012"]
    assert (
        candidates[0].score == 100.0
        and candidates[0].score_kind == "normalized_levenshtein_similarity_x100"
    )
    assert view.packet.retrieval.retriever.kind == "rapidfuzz"
    assert (
        view.header.origin == "test"
        and view.header.workflow == service.workflow_identity
    )
    done = service.submit_review(view.case_id, submission(view))
    assert done.status == "completed" and done.next_action == "none"
    assert done.review.submission.outcome == "candidate_for_follow_up"
    assert "syn-001/syn-001-a1" in done.explanation.evidence_ids
    assert done.explanation.template_version == "evidence-v2"
    assert event_types(service, view.case_id) == [
        "case_created",
        "evidence_recorded",
        "review_ready",
        "review_submitted",
        "case_finalized",
    ]
    assert service.verify_audit(view.case_id).ok
    assert done.final_receipt.sequence == 5
    assert CaseView.from_dict(json.loads(json.dumps(done.to_dict()))) == done


def test_explanation_never_claims_identity_or_clearance(workspace):
    service, view = awaiting(workspace)
    done = service.submit_review(
        view.case_id,
        submission(view, outcome="none_selected", candidate_entity_id=None),
    )
    text = done.explanation.text.lower()
    assert "none_selected" in text and "no selected record" in text
    for word in ("identical", "is the same person", "cleared", "sanctioned", "guilty"):
        assert word not in text
    assert "not a clearance" in done.explanation.limitation


def test_empty_retrieval_still_requires_review(workspace):
    service, view = awaiting(workspace, retriever=FakeRetriever(empty=True))
    assert view.status == "awaiting_review" and view.packet.retrieval.candidates == ()
    with pytest.raises(c.ValidationError, match="shortlist"):
        service.submit_review(view.case_id, submission(view))
    done = service.submit_review(
        view.case_id, submission(view, outcome="unresolved", candidate_entity_id=None)
    )
    assert done.status == "completed"


def test_get_and_list_cases_are_ordered_and_omit_text(workspace):
    service = new_service(workspace)
    ids = [
        service.start_case(
            f"Fictional Name {i}", origin="simulated_demo", actor="demo"
        ).case_id
        for i in range(3)
    ]
    summaries = service.list_cases(limit=2, offset=0) + service.list_cases(
        limit=2, offset=2
    )
    assert [s.case_id for s in summaries] == ids
    assert all(
        s.origin == "simulated_demo" and s.status == "awaiting_review"
        for s in summaries
    )
    assert "Fictional Name" not in json.dumps([s.to_dict() for s in summaries])
    assert service.get_case(ids[1]).header.name == "Fictional Name 1"
    with pytest.raises(c.NotFoundError):
        service.get_case("00000000-0000-4000-8000-000000000000")
    with pytest.raises(c.ValidationError):
        service.get_case("not-a-case")


def test_invalid_start_inputs_create_no_case(workspace):
    service = new_service(workspace)
    for name, origin, actor in (
        ("", "test", "a"),
        ("x" * 201, "test", "a"),
        ("Luma", "automatic", "a"),
        ("Luma", None, "a"),
        ("Luma", "test", ""),
        (None, "test", "a"),
    ):
        with pytest.raises(c.ValidationError):
            service.start_case(name, origin=origin, actor=actor)
    assert service.list_cases() == []


def test_origin_policy_is_enforced(workspace):
    service = ReviewService.initialize(
        workspace, make_config(allowed_origins=["human"]), retriever=FakeRetriever()
    )
    with pytest.raises(c.ValidationError, match="origin"):
        service.start_case("Luma Vesper", origin="simulated_demo", actor="demo")


# -- explicit init / open / read-only -----------------------------------------


def test_initialize_requires_missing_or_empty_directory(workspace):
    new_service(workspace)
    with pytest.raises(c.WorkspaceError):
        ReviewService.initialize(workspace, make_config())
    other = workspace.parent / "occupied"
    other.mkdir()
    (other / "note.txt").write_text("fictional", encoding="utf-8")
    with pytest.raises(c.WorkspaceError):
        ReviewService.initialize(other, make_config())
    assert [p.name for p in other.iterdir()] == ["note.txt"]


@pytest.mark.parametrize("read_only", [False, True])
def test_open_never_creates_a_missing_workspace(workspace, read_only):
    with pytest.raises(c.WorkspaceError):
        ReviewService.open(workspace, make_config(), read_only=read_only)
    assert not workspace.exists()


def test_open_rejects_incomplete_workspace_without_repairing(workspace):
    new_service(workspace)
    (workspace / DB_NAME).unlink()
    before = tree(workspace)
    with pytest.raises(c.WorkspaceError, match="incomplete"):
        ReviewService.open(workspace, make_config())
    assert tree(workspace) == before


def test_read_only_inspection_changes_no_file(workspace):
    service, view = awaiting(workspace)
    service.submit_review(view.case_id, submission(view))
    second = service.start_case("Mira Velune", origin="test", actor="fictional-analyst")
    before = tree(workspace)
    reader = ReviewService.open(workspace, make_config(), read_only=True)
    for _ in range(2):
        assert reader.get_case(view.case_id).status == "completed"
        assert reader.get_case(second.case_id).status == "awaiting_review"
        assert len(reader.list_cases()) == 2
        assert reader.audit(view.case_id).count == 5
        assert reader.verify_audit(view.case_id).ok
    writer = ReviewService.open(workspace, make_config())
    writer.get_case(second.case_id)
    assert tree(workspace) == before
    assert sorted(p.name for p in workspace.iterdir()) == [DB_NAME, "writer.lock"]


def test_read_only_service_rejects_every_mutation(workspace):
    _, view = awaiting(workspace)
    before = tree(workspace)
    reader = ReviewService.open(workspace, make_config(), read_only=True)
    calls = [
        lambda: reader.start_case("Luma Vesper", origin="test", actor="a"),
        lambda: reader.submit_review(view.case_id, submission(view)),
        lambda: reader.recover(view.case_id),
        lambda: reader.cancel_case(view.case_id, actor="a", reason="fictional"),
    ]
    for call in calls:
        with pytest.raises(c.ReadOnlyError):
            call()
    assert tree(workspace) == before


def test_status_reads_have_no_side_effects_on_preparing_cases(workspace):
    def crash(stage):
        if stage == "after_case":
            raise RuntimeError("simulated interruption")

    service = new_service(workspace, failpoint=crash)
    with pytest.raises(RuntimeError):
        service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    case_id = service.list_cases()[0].case_id
    before = tree(workspace)
    for _ in range(3):
        view = ReviewService.open(workspace, make_config(), read_only=True).get_case(
            case_id
        )
        assert (view.status, view.next_action, view.recovery_required) == (
            "preparing",
            "recover",
            True,
        )
    assert tree(workspace) == before


# -- idempotency, stale and duplicate submissions --------------------------------


def test_identical_resubmission_is_idempotent(workspace):
    service, view = awaiting(workspace)
    form = submission(view)
    first = service.submit_review(view.case_id, form)
    again = service.submit_review(view.case_id, ReviewSubmission.from_dict(form))
    assert again == first
    assert event_types(service, view.case_id).count("review_submitted") == 1


def test_same_submission_id_with_different_content_conflicts(workspace):
    service, view = awaiting(workspace)
    form = submission(view)
    service.submit_review(view.case_id, form)
    with pytest.raises(c.SubmissionConflictError):
        service.submit_review(
            view.case_id, {**form, "reason": "A different fictional reason."}
        )


def test_second_review_with_new_id_is_stale(workspace):
    service, view = awaiting(workspace)
    service.submit_review(view.case_id, submission(view))
    with pytest.raises(c.StaleSubmissionError):
        service.submit_review(
            view.case_id,
            submission(view, outcome="unresolved", candidate_entity_id=None),
        )


def test_submission_for_other_packet_is_stale_and_writes_nothing(workspace):
    service, view = awaiting(workspace)
    before = tree(workspace)
    with pytest.raises(c.StaleSubmissionError) as caught:
        service.submit_review(view.case_id, submission(view, packet_sha256="0" * 64))
    assert caught.value.details["expected_packet_sha256"] == view.packet_sha256
    assert tree(workspace) == before
    assert service.get_case(view.case_id).status == "awaiting_review"


def test_submission_id_cannot_be_reused_across_cases(workspace):
    service, first = awaiting(workspace)
    second = service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    form = submission(first)
    service.submit_review(first.case_id, form)
    with pytest.raises(c.SubmissionConflictError):
        service.submit_review(
            second.case_id, {**form, "packet_sha256": second.packet_sha256}
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"outcome": "automatic_match"},
        {"candidate_entity_id": "syn-999"},
        {"origin": "robot"},
        {"submission_id": "abc"},
    ],
)
def test_bad_submission_shapes_are_rejected_without_writes(workspace, bad):
    service, view = awaiting(workspace)
    before = tree(workspace)
    with pytest.raises(c.ValidationError):
        service.submit_review(view.case_id, submission(view, **bad))
    with pytest.raises(c.ValidationError):
        service.submit_review(
            view.case_id, {k: v for k, v in submission(view).items() if k != "actor"}
        )
    assert tree(workspace) == before


# -- cancellation -------------------------------------------------------------------


def test_cancel_blocks_review_and_is_idempotent(workspace):
    service, view = awaiting(workspace)
    cancelled = service.cancel_case(
        view.case_id, actor="fictional-lead", reason="Fictional duplicate request."
    )
    assert cancelled.status == "cancelled" and cancelled.next_action == "none"
    assert (
        service.cancel_case(
            view.case_id, actor="fictional-lead", reason="Fictional duplicate request."
        )
        == cancelled
    )
    with pytest.raises(c.InvalidStateError):
        service.cancel_case(view.case_id, actor="other", reason="Another reason.")
    with pytest.raises(c.InvalidStateError):
        service.submit_review(view.case_id, submission(view))
    assert service.recover(view.case_id).status == "cancelled"
    assert service.verify_audit(view.case_id).ok


def test_cancel_after_review_is_rejected(workspace):
    service, view = awaiting(workspace)
    service.submit_review(view.case_id, submission(view))
    with pytest.raises(c.InvalidStateError):
        service.cancel_case(view.case_id, actor="fictional-lead", reason="Too late.")


# -- versions and adapters ------------------------------------------------------------


def test_policy_change_makes_pending_cases_incompatible_but_readable(workspace):
    _, view = awaiting(workspace)
    changed = ReviewService.open(
        workspace, make_config(top_k=3), retriever=FakeRetriever()
    )
    seen = changed.get_case(view.case_id)
    assert (seen.status, seen.compatible, seen.next_action) == (
        "incompatible",
        False,
        "none",
    )
    assert seen.packet == view.packet
    with pytest.raises(c.VersionMismatchError):
        changed.submit_review(view.case_id, submission(view))
    with pytest.raises(c.VersionMismatchError):
        changed.recover(view.case_id)
    assert (
        changed.cancel_case(
            view.case_id, actor="fictional-lead", reason="Closed after policy change."
        ).status
        == "cancelled"
    )


def test_completed_cases_stay_completed_under_a_new_policy(workspace):
    service, view = awaiting(workspace)
    service.submit_review(view.case_id, submission(view))
    seen = ReviewService.open(workspace, make_config(top_k=3), read_only=True).get_case(
        view.case_id
    )
    assert (seen.status, seen.compatible) == ("completed", False)


def test_retriever_change_does_not_alter_pending_evidence(workspace):
    _, view = awaiting(workspace)
    reopened = ReviewService.open(
        workspace, make_config(), retriever=FakeRetriever(tag="new-model", fail=True)
    )
    done = reopened.submit_review(view.case_id, submission(view))
    assert done.status == "completed" and done.packet == view.packet


@pytest.mark.parametrize(
    "sql, args, error",
    [
        (
            "UPDATE workspace_meta SET value='review-workspace-v9' WHERE key='workspace_format'",
            (),
            c.VersionMismatchError,
        ),
        ("PRAGMA user_version = 3", (), c.VersionMismatchError),
        ("PRAGMA application_id = 7", (), c.WorkspaceError),
        ("CREATE TABLE intruder (x)", (), c.WorkspaceError),
    ],
)
def test_foreign_or_mismatched_workspace_is_rejected_at_open(
    workspace, sql, args, error
):
    new_service(workspace)
    db = sqlite3.connect(workspace / DB_NAME)
    db.execute(sql, args)
    db.commit()
    db.close()
    with pytest.raises(error):
        ReviewService.open(workspace, make_config(), read_only=True)


# -- context provider hook --------------------------------------------------------------


def test_context_providers_are_frozen_into_the_packet(workspace):
    provider = StaticContext()
    service, view = awaiting(workspace, context_providers=[provider])
    assert view.header.context_providers == ("fictional_notes",)
    assert view.packet.context == {
        "fictional_notes": {"version": "v1", "data": provider.data}
    }
    request = provider.requests[0]
    assert (
        request.candidate_entity_ids == ("syn-001", "syn-012")
        and request.case_id == view.case_id
    )
    assert [c.entity_id for c in view.packet.retrieval.candidates] == [
        "syn-001",
        "syn-012",
    ]
    assert service.submit_review(view.case_id, submission(view)).status == "completed"


def test_failing_context_provider_leaves_case_recoverable_without_new_retrieval(
    workspace,
):
    retriever = FakeRetriever()
    service = new_service(
        workspace, retriever=retriever, context_providers=[StaticContext(fail=True)]
    )
    with pytest.raises(c.ContextProviderError) as caught:
        service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    case_id = caught.value.details["case_id"]
    assert service.get_case(case_id).status == "preparing"
    fixed = ReviewService.open(
        workspace,
        make_config(),
        retriever=FakeRetriever(fail=True),
        context_providers=[StaticContext()],
    )
    assert fixed.recover(case_id).status == "awaiting_review"
    assert retriever.calls == 1
    renamed = ReviewService.open(
        workspace,
        make_config(),
        retriever=FakeRetriever(),
        context_providers=[StaticContext(name="other")],
    )
    other = renamed.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    assert other.header.context_providers == ("other",)


@pytest.mark.parametrize(
    "data", [{"bad": {1, 2}}, ["not", "object"], {"big": "x" * 70000}]
)
def test_invalid_context_data_is_rejected(workspace, data):
    service = new_service(workspace, context_providers=[StaticContext(data=data)])
    with pytest.raises(c.ContextProviderError):
        service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")


def test_context_provider_registration_is_validated(workspace):
    with pytest.raises(c.ValidationError):
        new_service(workspace, context_providers=[StaticContext(name="Bad Name")])
    with pytest.raises(c.ValidationError):
        ReviewService(
            workspace,
            make_config(),
            read_only=False,
            context_providers=[StaticContext(), StaticContext()],
        )
    with pytest.raises(c.ValidationError):
        ReviewService(
            workspace, make_config(), read_only=False, context_providers=[object()]
        )


def test_retriever_failures_are_errors_not_empty_results(workspace):
    service = new_service(workspace, retriever=FakeRetriever(fail=True))
    with pytest.raises(c.RetrievalError) as caught:
        service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    view = service.get_case(caught.value.details["case_id"])
    assert view.status == "preparing" and view.packet is None


# -- audit and tamper evidence ----------------------------------------------------------


def test_audit_view_round_trips_and_anchor_detects_truncation(workspace):
    service, view = awaiting(workspace)
    service.submit_review(view.case_id, submission(view))
    audit = service.audit(view.case_id)
    assert c.AuditView.from_dict(json.loads(json.dumps(audit.to_dict()))) == audit
    anchor = AuditAnchor(view.case_id, audit.count, audit.head_sha256)
    assert service.verify_audit(view.case_id, anchor).ok
    with_trigger_bypass(
        workspace,
        ("DELETE FROM audit_events WHERE case_id=? AND sequence=5", (view.case_id,)),
        ["audit_events_no_delete"],
    )
    result = service.verify_audit(view.case_id, anchor.to_dict())
    assert not result.ok and result.anchor_checked
    assert any("truncated" in issue for issue in result.issues)


def test_edited_audit_payload_is_detected_everywhere(workspace):
    service, view = awaiting(workspace)
    with_trigger_bypass(
        workspace,
        (
            "UPDATE audit_events SET payload_json=? WHERE case_id=? AND sequence=1",
            ('{"forged":true}', view.case_id),
        ),
        ["audit_events_no_update"],
    )
    result = service.verify_audit(view.case_id)
    assert not result.ok and any("hash mismatch" in issue for issue in result.issues)
    seen = service.get_case(view.case_id)
    assert (seen.status, seen.next_action) == ("inconsistent", "none")
    before = tree(workspace)
    with pytest.raises(c.StorageIntegrityError):
        service.submit_review(view.case_id, submission(view))
    with pytest.raises(c.StorageIntegrityError):
        service.recover(view.case_id)
    assert tree(workspace) == before


def test_rehashed_forgery_of_a_record_is_still_detected(workspace):
    service, view = awaiting(workspace)
    service.submit_review(view.case_id, submission(view))
    # Rewrite the saved review and its own checksum consistently; the audit
    # event and final event still bind the original review.
    db = sqlite3.connect(workspace / DB_NAME)
    payload = json.loads(
        db.execute("SELECT payload_json FROM submissions").fetchone()[0]
    )
    payload["submission"]["reason"] = "Forged fictional reason."
    db.close()
    encoded = c.canonical(payload)
    with_trigger_bypass(
        workspace,
        (
            "UPDATE submissions SET payload_json=?, sha256=?",
            (encoded, c.digest(payload)),
        ),
        ["submissions_no_update"],
    )
    result = service.verify_audit(view.case_id)
    assert not result.ok and any("review_submitted" in issue for issue in result.issues)


def test_audit_and_verify_unknown_case(workspace):
    service = new_service(workspace)
    with pytest.raises(c.NotFoundError):
        service.audit("00000000-0000-4000-8000-000000000000")
    with pytest.raises(c.NotFoundError):
        service.verify_audit("00000000-0000-4000-8000-000000000000")


def test_append_only_triggers_block_accidental_edits(workspace):
    _, view = awaiting(workspace)
    db = sqlite3.connect(workspace / DB_NAME)
    for statement in (
        "UPDATE audit_events SET created_at=created_at",
        "DELETE FROM audit_events",
        "UPDATE cases SET origin=origin",
        "UPDATE packets SET sha256=sha256",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(statement)
    db.close()


# -- regressions from the adversarial review ------------------------------------------


def test_review_origin_must_equal_case_origin_and_list_filters_by_origin(workspace):
    service, view = awaiting(workspace)
    before = tree(workspace)
    with pytest.raises(c.ValidationError, match="origin"):
        service.submit_review(view.case_id, submission(view, origin="human"))
    assert tree(workspace) == before
    assert service.get_case(view.case_id).status == "awaiting_review"
    demo = service.start_case(
        "Luma Vesper", origin="simulated_demo", actor="fictional-demo"
    )
    assert [s.case_id for s in service.list_cases(origin="test")] == [view.case_id]
    assert [s.case_id for s in service.list_cases(origin="simulated_demo")] == [
        demo.case_id
    ]
    assert service.list_cases(origin="human") == []
    with pytest.raises(c.ValidationError):
        service.list_cases(origin="robot")
    done = service.submit_review(
        demo.case_id, submission(demo, origin="simulated_demo")
    )
    assert (
        done.status == "completed"
        and done.review.submission.origin == done.header.origin
    )


def test_operational_config_changes_do_not_lock_out_pending_cases(workspace):
    _, view = awaiting(workspace)
    reopened = ReviewService.open(
        workspace,
        make_config(
            page_max=201,
            max_cases=20000,
            allowed_origins=["unknown", "test", "simulated_demo", "human"],
        ),
        retriever=FakeRetriever(),
    )
    assert reopened.get_case(view.case_id).compatible
    assert reopened.submit_review(view.case_id, submission(view)).status == "completed"


def test_default_list_page_respects_a_small_page_max(workspace):
    new_service(workspace)
    small = ReviewService.open(workspace, make_config(page_max=20), read_only=True)
    assert small.list_cases() == []
    with pytest.raises(c.ValidationError):
        small.list_cases(21)


class CallbackContext:
    """A provider that wrongly calls back into the service, or raises a domain error."""

    name, version = "fictional_callback", "v1"

    def __init__(self, mode):
        self.mode, self.reader = mode, None

    def provide(self, request):
        if self.mode == "reenter":
            self.reader.list_cases()
        elif self.mode == "domain_error":
            raise c.StaleSubmissionError("provider-internal error code")
        return {"notes": []}


@pytest.mark.parametrize(
    "mode, cause, retryable",
    [("reenter", "reentrant_call", False), ("domain_error", "stale_submission", True)],
)
def test_context_provider_errors_are_wrapped_and_reentry_fails_fast(
    workspace, mode, cause, retryable
):
    from time import monotonic

    provider = CallbackContext(mode)
    service = new_service(workspace, context_providers=[provider], lock_timeout=30.0)
    provider.reader = ReviewService.open(
        workspace, make_config(lock_timeout=30.0), read_only=True
    )
    started = monotonic()
    with pytest.raises(c.ContextProviderError) as caught:
        service.start_case(
            "Luma Vesper",
            origin="test",
            actor="fictional-analyst",
            request_id="5f0c2b1e-8d4a-4b6f-9e3c-7a1d2c3b4e5f",
        )
    assert monotonic() - started < 25  # never waits for the 30 s lock timeout
    error = caught.value.to_dict()
    assert (error["error"], error["details"]["cause"], error["retryable"]) == (
        "context_provider_failed",
        cause,
        retryable,
    )
    case_id = caught.value.details["case_id"]
    assert service.get_case(case_id).status == "preparing"
    provider.mode = "ok"
    assert service.recover(case_id).status == "awaiting_review"


def test_reentrant_lock_use_fails_immediately(workspace):
    from watchlist.review.locking import workspace_lock

    new_service(workspace)
    with workspace_lock(workspace, timeout=1.0):
        with pytest.raises(c.ReentrantCallError):
            with workspace_lock(workspace, timeout=30.0):
                pass
    with workspace_lock(workspace, timeout=0):
        pass


def test_start_case_request_id_makes_retries_idempotent(workspace):
    retriever = FakeRetriever(fail=True)
    service = new_service(workspace, retriever=retriever)
    request_id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    case_ids = set()
    for _ in range(3):
        with pytest.raises(c.RetrievalError) as caught:
            service.start_case(
                "Luma Vesper",
                origin="test",
                actor="fictional-analyst",
                request_id=request_id,
            )
        assert caught.value.retryable
        case_ids.add(caught.value.details["case_id"])
    assert len(case_ids) == 1 and len(service.list_cases()) == 1
    retriever.fail = False
    view = service.start_case(
        "Luma Vesper", origin="test", actor="fictional-analyst", request_id=request_id
    )
    assert (
        view.case_id in case_ids
        and view.status == "awaiting_review"
        and view.header.request_id == request_id
    )
    again = service.start_case(
        "Luma Vesper", origin="test", actor="fictional-analyst", request_id=request_id
    )
    assert again == view and retriever.calls == 4
    with pytest.raises(c.RequestConflictError):
        service.start_case(
            "Mira Velune",
            origin="test",
            actor="fictional-analyst",
            request_id=request_id,
        )
    with pytest.raises(c.ValidationError):
        service.start_case(
            "Luma Vesper",
            origin="test",
            actor="fictional-analyst",
            request_id="not-a-uuid",
        )
    assert len(service.list_cases()) == 1


def test_failure_after_case_creation_without_request_id_is_recover_only(workspace):
    service = new_service(workspace, retriever=FakeRetriever(fail=True))
    with pytest.raises(c.RetrievalError) as caught:
        service.start_case("Luma Vesper", origin="test", actor="fictional-analyst")
    error = caught.value.to_dict()
    assert error["retryable"] is False and error["details"]["next_action"] == "recover"
    assert service.get_case(error["details"]["case_id"]).next_action == "recover"


@pytest.mark.parametrize(
    "table",
    ["cases", "evidence", "packets", "submissions", "cancellations", "audit_events"],
)
def test_delete_and_update_triggers_block_plain_edits(workspace, table):
    service, view = awaiting(workspace)
    other = service.start_case("Mira Velune", origin="test", actor="fictional-analyst")
    service.submit_review(view.case_id, submission(view))
    service.cancel_case(
        other.case_id, actor="fictional-analyst", reason="Fictional cancellation."
    )
    db = sqlite3.connect(workspace / DB_NAME)
    for statement in (f"DELETE FROM {table}", f"UPDATE {table} SET case_id=case_id"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute(statement)
    db.close()


def test_a_removed_case_is_reported_not_silently_hidden(workspace):
    service, view = awaiting(workspace)
    service.submit_review(view.case_id, submission(view))
    other = service.start_case("Mira Velune", origin="test", actor="fictional-analyst")
    with_trigger_bypass(
        workspace,
        ("DELETE FROM cases WHERE case_id=?", (view.case_id,)),
        ["cases_no_delete"],
    )
    for operation in (
        lambda: ReviewService.open(workspace, make_config(), read_only=True),
        lambda: service.get_case(other.case_id),
        lambda: service.list_cases(),
    ):
        with pytest.raises(c.StorageIntegrityError) as caught:
            operation()
        assert "audit_events rows without a case" in caught.value.details["issues"]


def test_external_sqlite_lock_is_busy_not_corruption(workspace):
    new_service(workspace)
    holder = sqlite3.connect(workspace / DB_NAME, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(c.WorkspaceBusyError) as caught:
            ReviewService.open(workspace, make_config(), read_only=True)
        assert caught.value.retryable
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_non_contention_lock_failure_is_not_reported_as_busy(workspace, monkeypatch):
    import portalocker

    new_service(workspace)

    def broken(handle, flags):
        raise portalocker.LockException("fictional ENOLCK")

    monkeypatch.setattr(portalocker, "lock", broken)
    with pytest.raises(c.WorkspaceError, match="cannot lock"):
        ReviewService.open(workspace, make_config(lock_timeout=30.0), read_only=True)
