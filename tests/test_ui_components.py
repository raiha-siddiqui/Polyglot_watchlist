"""Unit tests for the reviewer interface's pure display helpers."""

from __future__ import annotations

import re

import pytest

pytest.importorskip("httpx")

from watchlist.ui.client import OUTCOMES, ApiClientError
from watchlist.ui.components import (
    OUTCOME_LABELS,
    RESEARCH_NOTICE,
    audit_rows,
    bidi,
    candidate_rows,
    candidates_html,
    case_header_html,
    error_text,
    esc,
    md_plain,
    memory_html,
    memory_panel,
    review_choice_errors,
    review_html,
)

EVIL = "<script>alert('x')</script><img src=x onerror=alert(1)>"
ARABIC = "سيرا لومين"


def case_with(alias: str = "Luma Vesper", *, context=None, candidates=True):
    evidence = {
        "evidence_id": "syn-001/syn-001-a1",
        "entity_id": "syn-001",
        "alias_id": "syn-001-a1",
        "original_text": alias,
        "source": "fictional-synthetic-v1",
        "synthetic": True,
        "attribution": "Authored fictional fixture",
        "provenance": {"note": EVIL},
    }
    candidate = {
        "rank": 1,
        "entity_id": "syn-001",
        "display_name": alias,
        "entity_type": "person",
        "matched_alias_id": "syn-001-a1",
        "matched_alias": alias,
        "score": 87.5,
        "score_kind": "normalized_levenshtein_similarity_x100",
        "evidence_ids": ["syn-001/syn-001-a1"],
    }
    return {
        "case_id": "c1",
        "status": "awaiting_review",
        "next_action": "submit_review",
        "issues": [EVIL],
        "compatibility_issues": [],
        "header": {
            "name": alias,
            "normalized_name": alias,
            "origin": "test",
            "created_at": "2026-10-26T00:00:00+00:00",
            "actor": EVIL,
        },
        "packet": {
            "retrieval": {
                "candidates": [candidate] if candidates else [],
                "evidence": [evidence] if candidates else [],
            },
            "context": context or {},
        },
    }


def no_raw_markup(text: str) -> bool:
    return not re.search(r"<(script|img|iframe|svg)\b", text, re.IGNORECASE)


def test_esc_and_bidi():
    assert esc(EVIL) == (
        "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;"
        "&lt;img src=x onerror=alert(1)&gt;"
    )
    assert esc(None) == ""
    assert bidi(ARABIC) == f'<span dir="auto">{ARABIC}</span>'
    assert (
        bidi('"', tag="td", cls='a"b') == '<td dir="auto" class="a&quot;b">&quot;</td>'
    )
    with pytest.raises(ValueError):
        bidi("x", tag="script")


def test_md_plain_neutralizes_markdown_html_latex_and_directives():
    text = md_plain("# Head\n* [x](javascript:alert(1)) <b>$x$</b> :red[y]")
    for raw in ("[x]", "](", "<b>", "$x$", ":red[", "\n"):
        assert raw not in text
    assert text.startswith("\\# Head")


def test_candidate_rows_join_evidence_and_keep_original_script():
    rows = candidate_rows(case_with(ARABIC))
    assert len(rows) == 1
    row = rows[0]
    assert row.matched_alias == ARABIC and row.entity_id == "syn-001"
    assert row.synthetic is True and "onerror" in row.provenance
    html = candidates_html(rows)
    assert f'<td dir="auto">{ARABIC}</td>' in html
    assert "87.50" in html and "Uncalibrated similarity score" in html
    assert no_raw_markup(html) and "&lt;script&gt;" in html


def test_matched_alias_evidence_is_chosen_by_alias_id():
    case = case_with(ARABIC)
    retrieval = case["packet"]["retrieval"]
    latin = dict(retrieval["evidence"][0], evidence_id="syn-001/syn-001-a0")
    latin.update(alias_id="syn-001-a0", original_text="Sira Lumen")
    retrieval["evidence"].insert(0, latin)
    retrieval["candidates"][0]["evidence_ids"].insert(0, "syn-001/syn-001-a0")
    assert candidate_rows(case)[0].matched_alias == ARABIC


def test_malicious_alias_is_escaped_everywhere():
    case = case_with(EVIL)
    for html in (
        candidates_html(candidate_rows(case)),
        case_header_html(case),
    ):
        assert no_raw_markup(html)
        assert "&lt;img src=x onerror=alert(1)&gt;" in html


def test_empty_candidates_are_not_a_clearance():
    html = candidates_html(candidate_rows(case_with(candidates=False)))
    assert "No candidate records" in html and "not a clearance" in html


def test_review_html_labels_and_escapes():
    case = case_with()
    case.update(
        status="completed",
        review={
            "submitted_at": "2026-10-26T01:00:00+00:00",
            "submission": {
                "outcome": "none_selected",
                "candidate_entity_id": None,
                "reason": EVIL,
                "actor": "fictional-reviewer",
                "origin": "test",
            },
        },
        explanation={"template_version": "evidence-v2", "text": EVIL},
    )
    html = review_html(case)
    assert "NOT a clearance" in html and no_raw_markup(html)
    assert "Selected record" not in html


def test_outcome_labels_cover_exactly_the_three_outcomes():
    assert (
        tuple(OUTCOME_LABELS)
        == OUTCOMES
        == (
            "candidate_for_follow_up",
            "none_selected",
            "unresolved",
        )
    )
    assert "not a clearance" in RESEARCH_NOTICE


@pytest.mark.parametrize(
    ("outcome", "candidate", "reason", "expected"),
    [
        (None, None, "r", ["Choose an outcome"]),
        ("cleared", None, "r", ["Choose an outcome"]),
        ("candidate_for_follow_up", None, "r", ["Choose which listed record"]),
        ("candidate_for_follow_up", "syn-999", "r", ["Choose which listed record"]),
        ("none_selected", "syn-001", "r", ["only for candidate_for_follow_up"]),
        ("unresolved", None, "   ", ["A reason is required"]),
        ("unresolved", None, "x" * 1001, ["at most 1000"]),
        ("candidate_for_follow_up", "syn-001", "ok", []),
        ("none_selected", None, "ok", []),
    ],
)
def test_review_choice_errors(outcome, candidate, reason, expected):
    errors = review_choice_errors(outcome, candidate, reason, ["syn-001"])
    assert len(errors) == len(expected)
    for got, want in zip(errors, expected):
        assert want in got


def memory_context(status="ok"):
    if status != "ok":
        return {
            "case_memory": {
                "version": "case-memory-context-v1",
                "data": {
                    "status": status,
                    "notice": "Prior annotations are not identity facts.",
                    "message": "This is not an empty history.",
                    "keys": [],
                },
            }
        }
    card = {
        "reviewed_at": "2026-09-01T10:00:00+00:00",
        "case_id": "earlier-case",
        "origin": "test",
        "outcome": "candidate_for_follow_up",
        "relation": "selected_for_follow_up",
        "interpretation": "Selected earlier; does not verify identity.",
        "reviewer_opinion": EVIL,
        "actor_label": "fictional-reviewer",
        "status": "active",
    }
    return {
        "case_memory": {
            "version": "case-memory-context-v1",
            "data": {
                "status": "ok",
                "notice": "Prior annotations are not identity facts.",
                "lookup_at": "2026-10-26T00:00:00+00:00",
                "warnings": ["1 completed review(s) are not yet published to memory"],
                "keys": [
                    {
                        "namespace_id": "fictional",
                        "source_entity_id": "syn-001",
                        "eligible_count": 2,
                        "shown_count": 1,
                        "omitted": {"over_cap": 1, "expired": 0},
                        "differing_outcomes": True,
                        "cards": [card],
                    }
                ],
            },
        }
    }


def test_memory_panel_dated_cards_escaped():
    panel = memory_panel(case_with(context=memory_context()))
    assert panel is not None and panel.status == "ok"
    key = panel.keys[0]
    assert key.entity_id == "syn-001" and key.omitted == {"over_cap": 1}
    html = memory_html(panel)
    assert "2026-09-01T10:00:00+00:00" in html
    assert "Earlier reviewer opinion" in html and "earlier reviewers differed" in html
    assert "not yet published" in html
    assert no_raw_markup(html)


def test_memory_unavailable_and_absent():
    panel = memory_panel(case_with(context=memory_context("unregistered_snapshot")))
    html = memory_html(panel)
    assert "unregistered_snapshot" in html and "not an empty history" in html
    assert memory_panel(case_with()) is None
    assert "says nothing about earlier reviews" in memory_html(None)
    bad = case_with(context={"case_memory": {"version": "v", "data": "oops"}})
    assert memory_panel(bad).status == "malformed"


def test_audit_rows_are_plain():
    rows = audit_rows(
        {
            "events": [
                {
                    "sequence": 1,
                    "event_type": "case_created",
                    "created_at": "t",
                    "event_sha256": "a" * 64,
                    "previous_sha256": "0" * 64,
                    "payload": {"name": EVIL},
                }
            ]
        }
    )
    assert rows[0]["sequence"] == 1 and "payload" not in rows[0]
    assert rows[0]["event_sha256"].startswith("a" * 16)


def test_error_text_has_guidance_and_no_key():
    error = ApiClientError("stale_submission", "changed", status=409, retryable=False)
    text = error_text(error)
    assert "Reload the case" in text and "HTTP 409" in text
    text = error_text(ApiClientError("api_unreachable", "down", retryable=True))
    assert "no response" in text and "safe to retry" in text
