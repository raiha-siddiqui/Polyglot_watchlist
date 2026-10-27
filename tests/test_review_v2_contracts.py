"""Contract versions, JSON round trips and strict rejection of bad shapes (v2)."""

import copy
import json
import math
from uuid import uuid4

import pytest

from review_v2_support import CONFIG_PATH, config_dict, make_config
from watchlist.review import contracts as c
from watchlist.review.contracts import (
    AcceptedReview,
    AuditAnchor,
    ReviewConfig,
    ReviewSubmission,
    ValidationError,
    VersionMismatchError,
)
from watchlist.review.validation import (
    coerce_submission,
    validate_actor,
    validate_name,
    validate_origin,
    validate_page,
    validate_reason,
)

SHA = "a" * 64


def form(**changes):
    value = {
        "submission_id": str(uuid4()),
        "packet_sha256": SHA,
        "outcome": "candidate_for_follow_up",
        "candidate_entity_id": "syn-001",
        "reason": "Fictional reason.",
        "actor": "fictional-reviewer",
        "origin": "test",
    }
    value.update(changes)
    return value


def test_declared_versions_are_explicit_strings():
    assert c.CASE_SCHEMA == "review-case-v2"
    assert c.PACKET_SCHEMA == "review-packet-v2"
    assert c.WORKFLOW_VERSION == "review-workflow-v2.0"
    assert c.WORKSPACE_FORMAT == "review-workspace-v2"
    assert set(c.ORIGINS) == {"human", "simulated_demo", "test", "unknown"}
    assert c.OUTCOMES == ("candidate_for_follow_up", "none_selected", "unresolved")


def test_project_config_loads_and_identity_is_version_plus_policy_digest():
    config = ReviewConfig.load(CONFIG_PATH)
    identity = config.workflow_identity()
    assert identity.version == c.WORKFLOW_VERSION
    assert identity.config_sha256 == c.digest(config.workflow.to_dict())
    assert config.retriever.kind == "rapidfuzz"


def test_identity_ignores_formatting_key_order_and_operational_settings(tmp_path):
    data = config_dict()
    reordered = {key: data[key] for key in reversed(list(data))}
    reordered["operations"] = {
        "lock_timeout_seconds": 42.0,
        "max_cases": 3,
        "page_max": 7,
    }
    reordered["workflow"] = {
        **data["workflow"],
        "allowed_origins": list(reversed(data["workflow"]["allowed_origins"])),
    }
    path = tmp_path / "reformatted.json"
    path.write_text(json.dumps(reordered, indent=7), encoding="utf-8", newline="\n")
    assert (
        ReviewConfig.load(path).workflow_identity()
        == ReviewConfig.load(CONFIG_PATH).workflow_identity()
    )


def test_capacity_paging_and_origin_order_do_not_change_identity():
    # Regression: page_max/max_cases used to be hashed, locking out in-flight cases.
    base = make_config().workflow_identity()
    assert make_config(page_max=201, max_cases=20000).workflow_identity() == base
    assert (
        make_config(
            allowed_origins=["unknown", "test", "simulated_demo", "human"]
        ).workflow_identity()
        == base
    )
    assert make_config(allowed_origins=["human"]).workflow_identity() != base


def test_identity_changes_when_workflow_policy_changes():
    assert make_config(top_k=4).workflow_identity() != make_config().workflow_identity()


@pytest.mark.parametrize(
    "mutate, error",
    [
        (lambda d: d.update(config_version="review-config-v9"), VersionMismatchError),
        (lambda d: d.pop("operations"), ValidationError),
        (lambda d: d.update(extra=1), ValidationError),
        (lambda d: d["workflow"].update(top_k=0), ValidationError),
        (lambda d: d["workflow"].update(top_k=True), ValidationError),
        (lambda d: d["workflow"].update(allowed_origins=["robot"]), ValidationError),
        (lambda d: d["workflow"].update(allowed_origins=[]), ValidationError),
        (
            lambda d: d["workflow"].update(explanation_template="free-prose"),
            ValidationError,
        ),
        (lambda d: d["retriever"].update(kind="e5"), ValidationError),
        (lambda d: d["retriever"].update(embedding_config="x.json"), ValidationError),
        (lambda d: d["operations"].update(lock_timeout_seconds=-1), ValidationError),
        (lambda d: d["operations"].update(page_max=0), ValidationError),
        (lambda d: d["operations"].pop("max_cases"), ValidationError),
        (lambda d: d["workflow"].update(page_max=200), ValidationError),
    ],
)
def test_invalid_configs_are_rejected(mutate, error):
    data = copy.deepcopy(config_dict())
    mutate(data)
    with pytest.raises(error):
        ReviewConfig.from_dict(data)


def test_config_text_rejects_duplicate_keys_and_nonfinite_numbers(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        '{"config_version": "review-config-v2", "config_version": "x"}',
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="duplicate"):
        ReviewConfig.load(path)
    path.write_text('{"a": NaN}', encoding="utf-8")
    with pytest.raises(ValidationError, match="nonfinite"):
        ReviewConfig.load(path)
    with pytest.raises(ValidationError):
        ReviewConfig.load(tmp_path / "missing.json")


def test_submission_round_trip_is_json_safe():
    submission = ReviewSubmission.from_dict(form())
    data = submission.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert ReviewSubmission.from_dict(data) == submission
    accepted = AcceptedReview(
        c.SUBMISSION_SCHEMA, str(uuid4()), "2026-01-01T00:00:00+00:00", submission
    )
    assert (
        AcceptedReview.from_dict(json.loads(json.dumps(accepted.to_dict()))) == accepted
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"submission_id": "not-a-uuid"},
        {"submission_id": str(uuid4()).upper()},
        {"packet_sha256": "A" * 64},
        {"packet_sha256": "a" * 63},
        {"outcome": "match"},
        {"outcome": "cleared"},
        {"outcome": "none_selected"},
        {"outcome": "unresolved", "candidate_entity_id": "syn-001"},
        {"candidate_entity_id": None},
        {"candidate_entity_id": ""},
        {"reason": ""},
        {"reason": "   "},
        {"reason": 7},
        {"reason": "x" * 4001},
        {"actor": ""},
        {"actor": None},
        {"origin": "robot"},
        {"reason": "\ud800"},
    ],
)
def test_bad_submission_values_are_rejected(changes):
    with pytest.raises(ValidationError):
        ReviewSubmission.from_dict(form(**changes))


@pytest.mark.parametrize("value", [None, [], "text", 3, {}])
def test_non_object_or_empty_submission_is_rejected(value):
    with pytest.raises(ValidationError):
        ReviewSubmission.from_dict(value)


def test_missing_and_extra_submission_fields_are_reported():
    missing = form()
    del missing["reason"]
    with pytest.raises(ValidationError) as caught:
        ReviewSubmission.from_dict(missing)
    assert caught.value.details["missing"] == ["reason"]
    with pytest.raises(ValidationError) as caught:
        ReviewSubmission.from_dict({**form(), "reviewer_decision": "override"})
    assert caught.value.details["unexpected"] == ["reviewer_decision"]


def test_policy_bounds_apply_on_top_of_structural_checks():
    policy = make_config(reason_max_chars=10, allowed_origins=["human"]).workflow
    with pytest.raises(ValidationError, match="reason"):
        coerce_submission(form(origin="human"), policy)
    with pytest.raises(ValidationError, match="origin"):
        coerce_submission(form(reason="short"), policy)
    assert (
        coerce_submission(form(reason="short", origin="human"), policy).reason
        == "short"
    )


@pytest.mark.parametrize("name", ["", "   ", "x" * 201, 7, None, "\ud800"])
def test_invalid_names_are_rejected(name):
    with pytest.raises(ValidationError):
        validate_name(name, make_config().workflow)


def test_name_is_normalized_but_original_is_kept():
    assert validate_name("  LUMA   VESPER ", make_config().workflow) == (
        "  LUMA   VESPER ",
        "luma vesper",
    )


def test_origin_and_paging_validation():
    policy = make_config().workflow
    assert validate_origin("simulated_demo", policy) == "simulated_demo"
    for bad in (None, "Human", "automatic"):
        with pytest.raises(ValidationError):
            validate_origin(bad, policy)
    operations = make_config().operations
    for limit, offset in ((0, 0), (201, 0), (True, 0), (10, -1), (10, 1.5)):
        with pytest.raises(ValidationError):
            validate_page(limit, offset, operations)
    with pytest.raises(ValidationError):
        validate_page(10, 0, operations, "automatic")
    assert validate_page(None, 0, operations) == (50, 0, None)
    # Regression: the default page must not exceed a smaller page_max.
    assert validate_page(None, 0, make_config(page_max=20).operations, "test") == (
        20,
        0,
        "test",
    )


@pytest.mark.parametrize(
    "field, value",
    [
        ("name", "​"),
        ("name", "Luma\x00Vesper"),
        ("name", "Luma ‮Vesper"),
        ("name", " \t "),
        ("actor", "x\x00"),
        ("actor", "​"),
        ("actor", "analyst‍"),
        ("reason", "\x07beep"),
        ("reason", "⁦"),
    ],
)
def test_control_and_invisible_text_is_rejected(field, value):
    policy = make_config().workflow
    check = {"name": validate_name, "actor": validate_actor, "reason": validate_reason}[
        field
    ]
    with pytest.raises(ValidationError):
        check(value, policy)


def test_visible_text_keeps_script_joiners_and_reason_line_breaks():
    policy = make_config().workflow
    # ZWNJ is part of correct Persian spelling; it must survive validation.
    assert validate_name("می‌خواهم", policy)[0].count("‌") == 1
    assert validate_reason("Line one.\nLine two.\tTabbed.", policy).count("\n") == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"case_id": "not-a-uuid", "count": 1, "head_sha256": "a" * 64},
        {
            "case_id": "00000000-0000-4000-8000-000000000000",
            "count": 0,
            "head_sha256": "a" * 64,
        },
        {
            "case_id": "00000000-0000-4000-8000-000000000000",
            "count": True,
            "head_sha256": "a" * 64,
        },
        {
            "case_id": "00000000-0000-4000-8000-000000000000",
            "count": 1,
            "head_sha256": "XYZ",
        },
    ],
)
def test_audit_anchor_is_validated_on_direct_construction(kwargs):
    with pytest.raises(ValidationError):
        c.AuditAnchor(**kwargs)


def test_json_value_rejects_nonfinite_and_non_json():
    with pytest.raises(ValidationError):
        c.json_value({"x": math.inf}, "x")
    with pytest.raises(ValidationError):
        c.json_value({"x": {1, 2}}, "x")
    with pytest.raises(ValidationError):
        c.json_value({1: "x"}, "x")
    assert c.json_value(("a", {"b": 1.5}), "x") == ["a", {"b": 1.5}]


def test_anchor_contract_requires_a_positive_count():
    with pytest.raises(ValidationError):
        AuditAnchor.from_dict({"case_id": str(uuid4()), "count": 0, "head_sha256": SHA})


def test_errors_have_stable_codes_and_json_form():
    error = c.StaleSubmissionError("old packet", details={"case_id": "x"})
    assert error.to_dict() == {
        "error": "stale_submission",
        "message": "old packet",
        "retryable": False,
        "details": {"case_id": "x"},
    }
    assert c.WorkspaceBusyError("busy").retryable is True
    codes = {
        cls.code
        for cls in vars(c).values()
        if isinstance(cls, type) and issubclass(cls, c.ReviewError)
    }
    assert {
        "invalid_input",
        "case_not_found",
        "workspace_invalid",
        "workspace_busy",
        "read_only",
        "incompatible_version",
        "invalid_state",
        "stale_submission",
        "submission_conflict",
        "recovery_required",
        "storage_inconsistent",
        "retriever_mismatch",
        "retrieval_failed",
        "context_provider_failed",
    } <= codes
