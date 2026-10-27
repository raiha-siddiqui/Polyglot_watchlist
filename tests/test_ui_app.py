"""End-to-end tests of the Streamlit reviewer interface (``AppTest``).

The real ``app.py`` runs against the real API and a real v2 ``ReviewService``
in a temporary workspace (``test_ui_support.live_api``). Walks: screen ->
review -> audit, stale/duplicate handling, escaping, right-to-left text,
memory context and error states. All names are fictional fixtures.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from test_ui_support import (
    REVIEWER_KEY,
    VIEWER_KEY,
    app_test,
    button,
    form_ids,
    html_text,
    live_api,
    page_text,
    screen,
)

from review_v2_support import FakeRetriever, StaticContext
from streamlit.testing.v1 import AppTest
from watchlist.review.nodes import RapidFuzzRetriever
from watchlist.ui.client import ApiClientError, ReviewForm, new_uuid
from watchlist.ui.components import RESEARCH_NOTICE, md_plain

EVIL_NAME = "<script>alert('pwned')</script>"
EVIL_ALIAS = '<img src=x onerror="alert(1)">Luma'
ARABIC_ALIAS = "سيرا لومين"


def raw_markup(text: str) -> bool:
    return bool(re.search(r"<(script|img|iframe|svg)\b", text, re.IGNORECASE))


def assert_notice(at: AppTest) -> None:
    assert md_plain(RESEARCH_NOTICE) in [w.value for w in at.warning]


def review(
    at: AppTest,
    outcome: str,
    reason: str,
    candidate: str | None = None,
) -> AppTest:
    sid = form_ids(at)
    at.radio(key=f"outcome_{sid}").set_value(outcome)
    at.run()
    if candidate is not None:
        at.selectbox(key=f"candidate_{sid}").select(candidate)
    at.text_area(key=f"reason_{sid}").input(reason)
    button(at, "Submit review").click()
    return at.run()


class LosingClient:
    """Wraps a client; the first call of ``method`` succeeds server-side but the
    response is 'lost' (raises ``api_timeout``), like a dropped connection."""

    def __init__(self, inner: Any, method: str):
        self._inner, self._method, self.lost = inner, method, 0
        self.config = inner.config
        self.calls: list[tuple[Any, ...]] = []

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if name != self._method:
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((args, kwargs))
            result = target(*args, **kwargs)
            if not self.lost:
                self.lost += 1
                raise ApiClientError("api_timeout", "lost response", retryable=True)
            return result

        return call


# -- notice and configuration ------------------------------------------------------------


def test_notice_on_every_page_and_key_never_shown(tmp_path):
    with live_api(tmp_path) as (client, _):
        at = app_test(client)
        assert not at.exception
        assert_notice(at)
        for page in ("Case list", "Current case", "Screen a name"):
            at.radio(key="nav").set_value(page)
            at.run()
            assert not at.exception
            assert_notice(at)
            assert REVIEWER_KEY not in page_text(at) + html_text(at)
        assert "API key: configured" in page_text(at)


def test_bad_configuration_shows_error_and_notice(monkeypatch):
    monkeypatch.setenv("WATCHLIST_API_URL", "http://example.org:8000")
    monkeypatch.setenv("WATCHLIST_API_KEY", REVIEWER_KEY)
    at = AppTest.from_file(
        str(__import__("test_ui_support").APP_PATH), default_timeout=60
    )
    at.run()
    at.text_input(key="session_api_key").input(REVIEWER_KEY)
    button(at, "Sign in").click().run()
    assert not at.exception
    assert_notice(at)
    errors = " ".join(e.value for e in at.error)
    assert "client\\_config\\_invalid" in errors
    assert REVIEWER_KEY not in errors


# -- main walkthrough ---------------------------------------------------------------------------


def test_screen_review_audit_walkthrough(tmp_path):
    with live_api(tmp_path) as (client, service):
        at = screen(app_test(client), "Luma Vesper")
        assert not at.exception
        assert_notice(at)
        assert at.session_state["nav"] == "Current case"
        html = html_text(at)
        assert "syn-001" in html and "syn-012" in html
        assert "Uncalibrated similarity score" in html
        assert "awaiting_review" in html
        # No outcome preselected; no candidate selector until follow-up is chosen.
        sid = form_ids(at)
        assert at.radio(key=f"outcome_{sid}").value is None
        assert not [s for s in at.selectbox if (s.key or "").startswith("candidate_")]
        at = review(
            at,
            "candidate_for_follow_up",
            "Fictional walkthrough: the alias text is identical.",
            candidate="syn-012",
        )
        assert not at.exception
        assert "Your review was recorded" in page_text(at)
        html = html_text(at)
        assert "completed" in html and "syn-012" in html
        assert "Fictional walkthrough" in html
        assert not [t for t in at.text_area if t.label.startswith("Your unsent notes")]
        case_id = at.session_state["case_id"]
        stored = service.get_case(case_id)
        assert stored.status == "completed"
        assert stored.review.submission.candidate_entity_id == "syn-012"
        assert stored.review.submission.submission_id == sid
        assert stored.review.submission.actor == "fictional-reviewer"
        # Audit view with local verification.
        button(at, "Load audit and verify").click()
        at.run()
        assert not at.exception
        success = " ".join(s.value for s in at.success)
        assert "Audit chain verified locally" in success
        table = at.dataframe[0].value
        assert list(table["sequence"]) == list(range(1, len(table) + 1))
        # Case list shows the case and reopens it.
        at.radio(key="nav").set_value("Case list")
        at.run()
        assert case_id in list(at.dataframe[0].value["case_id"])
        at.selectbox(key="open_case_id").select(case_id)
        button(at, "Open case").click()
        at.run()
        assert at.session_state["nav"] == "Current case"
        assert not [r for r in at.radio if (r.key or "").startswith("outcome_")]


def test_none_selected_needs_reason_and_has_no_candidate(tmp_path):
    with live_api(tmp_path) as (client, service):
        at = screen(app_test(client), "Luma Vesper")
        sid = form_ids(at)
        at.radio(key=f"outcome_{sid}").set_value("none_selected")
        at.run()
        assert not [s for s in at.selectbox if (s.key or "").startswith("candidate_")]
        assert any(
            "NOT a clearance" in o for o in at.radio(key=f"outcome_{sid}").options
        )
        button(at, "Submit review").click()
        at.run()
        assert "A reason is required" in page_text(at)
        case_id = at.session_state["case_id"]
        assert service.get_case(case_id).status == "awaiting_review"
        at = review(at, "none_selected", "Fictional: no listed record fits.")
        submitted = service.get_case(case_id).review.submission
        assert submitted.outcome == "none_selected"
        assert submitted.candidate_entity_id is None


def test_follow_up_without_candidate_is_refused_client_side(tmp_path):
    with live_api(tmp_path) as (client, service):
        at = screen(app_test(client), "Luma Vesper")
        sid = form_ids(at)
        at.radio(key=f"outcome_{sid}").set_value("candidate_for_follow_up")
        at.run()
        at.text_area(key=f"reason_{sid}").input("reason")
        button(at, "Submit review").click()
        at.run()
        assert "Choose which listed record" in page_text(at)
        assert service.get_case(at.session_state["case_id"]).review is None


def test_form_identity_is_stable_across_reruns(tmp_path):
    with live_api(tmp_path) as (client, _):
        at = screen(app_test(client), "Luma Vesper")
        first = form_ids(at)
        at.run()
        button(at, "Reload case").click()
        at.run()
        assert form_ids(at) == first


# -- duplicates and lost responses --------------------------------------------------------------


def test_lost_screen_response_retry_reuses_request_id(tmp_path):
    with live_api(tmp_path) as (inner, service):
        client = LosingClient(inner, "start_case")
        at = screen(app_test(client), "Luma Vesper")
        assert "api\\_timeout" in " ".join(e.value for e in at.error)
        assert at.session_state["nav"] == "Screen a name"
        button(at, "Screen name").click()
        at.run()
        assert at.session_state["nav"] == "Current case"
        ids = [kwargs["request_id"] for _, kwargs in client.calls]
        assert len(ids) == 2 and ids[0] == ids[1]
        assert len(service.list_cases(50, 0)) == 1


def test_lost_review_response_resend_records_one_review(tmp_path):
    with live_api(tmp_path) as (inner, service):
        client = LosingClient(inner, "submit_review")
        at = screen(app_test(client), "Luma Vesper")
        at = review(at, "unresolved", "Fictional: cannot decide.")
        assert "same submission ID" in " ".join(w.value for w in at.warning)
        case_id = at.session_state["case_id"]
        assert service.get_case(case_id).status == "completed"
        # The screen still shows the old response; resending the same form is a replay.
        button(at, "Submit review").click()
        at.run()
        assert not at.exception
        assert "Your review was recorded" in page_text(at)
        forms = [args[1] for args, _ in client.calls]
        assert forms[0] == forms[1]
        assert (
            service.get_case(case_id).review.submission.reason
            == "Fictional: cannot decide."
        )


def test_stale_form_keeps_notes_and_disables_submit(tmp_path):
    with live_api(tmp_path) as (client, service):
        at = screen(app_test(client), "Luma Vesper")
        case_id = at.session_state["case_id"]
        # Another reviewer (same API) completes the case first.
        current = client.get_case(case_id)
        client.submit_review(
            case_id,
            ReviewForm(
                new_uuid(),
                current["revision"],
                current["case"]["packet_sha256"],
                "unresolved",
                None,
                "Other fictional reviewer.",
            ),
        )
        at = review(at, "none_selected", "My unsent fictional notes.")
        assert not at.exception
        errors = " ".join(e.value for e in at.error)
        assert "stale\\_submission" in errors
        submit = button(at, "Submit review")
        assert submit.disabled
        sid = form_ids(at)
        assert at.text_area(key=f"reason_{sid}").value == "My unsent fictional notes."
        button(at, "Reload case").click()
        at.run()
        html = html_text(at)
        assert "Other fictional reviewer." in html
        notes = [t for t in at.text_area if t.label.startswith("Your unsent notes")]
        assert notes and notes[0].value == "My unsent fictional notes."
        assert service.get_case(case_id).review.submission.reason == (
            "Other fictional reviewer."
        )


def test_stale_reload_while_still_awaiting_gives_fresh_form_with_notes(tmp_path):
    class StaleOnce:
        def __init__(self, inner):
            self._inner, self.config, self.done = inner, inner.config, False

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def submit_review(self, case_id, form):
            if not self.done:
                self.done = True
                raise ApiClientError(
                    "stale_submission", "changed", status=409, retryable=False
                )
            return self._inner.submit_review(case_id, form)

    with live_api(tmp_path) as (inner, service):
        at = screen(app_test(StaleOnce(inner)), "Luma Vesper")
        old = form_ids(at)
        at = review(at, "unresolved", "Carry these fictional notes.")
        assert button(at, "Submit review").disabled
        button(at, "Reload case").click()
        at.run()
        new = form_ids(at)
        assert new != old
        assert at.radio(key=f"outcome_{new}").value is None
        assert at.text_area(key=f"reason_{new}").value == "Carry these fictional notes."
        at = review(at, "unresolved", "Carry these fictional notes.")
        stored = service.get_case(at.session_state["case_id"]).review.submission
        assert stored.submission_id == new


# -- escaping and right-to-left ------------------------------------------------------------------


def test_malicious_text_is_escaped_everywhere(tmp_path):
    with live_api(tmp_path, FakeRetriever(alias=EVIL_ALIAS)) as (client, _):
        at = screen(app_test(client), EVIL_NAME)
        assert not at.exception
        at = review(
            at,
            "candidate_for_follow_up",
            "<iframe src=javascript:alert(2)></iframe>[link](javascript:x)",
            candidate="syn-001",
        )
        button(at, "Load audit and verify").click()
        at.run()
        html = html_text(at)
        assert "&lt;script&gt;alert(&#x27;pwned&#x27;)&lt;/script&gt;" in html
        assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;Luma" in html
        assert "&lt;iframe" in html
        assert not raw_markup(html)
        # Nothing with record text goes through Markdown with HTML enabled.
        for element in at.get("markdown") + list(at.warning) + list(at.error):
            assert not raw_markup(element.value)
        for element in at.get("markdown"):
            assert not element.proto.allow_html


def test_arabic_text_renders_right_to_left(tmp_path):
    with live_api(tmp_path, RapidFuzzRetriever()) as (client, _):
        at = screen(app_test(client), ARABIC_ALIAS)
        assert not at.exception
        html = html_text(at)
        assert f'<bdi dir="auto">{ARABIC_ALIAS}</bdi>' in html  # screened name
        assert f'<td dir="auto">{ARABIC_ALIAS}</td>' in html  # matched alias
        rows = re.findall(r"<tr><td>1</td><td><code>([^<]+)</code>", html)
        assert rows == ["syn-007"]


# -- memory context ---------------------------------------------------------------------------------


def test_prior_annotations_are_shown_as_dated_opinions(tmp_path):
    data = {
        "status": "ok",
        "notice": "Earlier dated annotations; not identity facts.",
        "lookup_at": "2026-10-26T00:00:00+00:00",
        "warnings": [],
        "keys": [
            {
                "namespace_id": "fictional",
                "source_entity_id": "syn-001",
                "eligible_count": 1,
                "shown_count": 1,
                "omitted": {},
                "differing_outcomes": False,
                "cards": [
                    {
                        "reviewed_at": "2026-09-30T08:00:00+00:00",
                        "case_id": "fictional-earlier-case",
                        "origin": "test",
                        "outcome": "candidate_for_follow_up",
                        "relation": "selected_for_follow_up",
                        "interpretation": "Selected earlier; not verified identity.",
                        "reviewer_opinion": "<b>bold?</b> fictional opinion",
                        "actor_label": "fictional-reviewer",
                        "status": "active",
                    }
                ],
            }
        ],
    }
    provider = StaticContext("case_memory", data)
    with live_api(tmp_path, context_providers=[provider]) as (client, _):
        at = screen(app_test(client), "Luma Vesper")
        assert not at.exception
        html = html_text(at)
        assert "2026-09-30T08:00:00+00:00" in html
        assert "Earlier reviewer opinion" in html
        assert "&lt;b&gt;bold?&lt;/b&gt; fictional opinion" in html
        assert "Prior annotations (reviewer memory)" in page_text(at)


def test_memory_unavailable_is_not_empty_history(tmp_path):
    provider = StaticContext(
        "case_memory",
        {
            "status": "unregistered_snapshot",
            "notice": "n",
            "message": "This is not an empty history.",
            "keys": [],
        },
    )
    with live_api(tmp_path, context_providers=[provider]) as (client, _):
        at = screen(app_test(client), "Luma Vesper")
        assert "unregistered_snapshot" in html_text(at)
        assert "not an empty history" in html_text(at)


# -- errors and other states ------------------------------------------------------------------------


def test_empty_result_is_not_a_clearance(tmp_path):
    with live_api(tmp_path, FakeRetriever(empty=True)) as (client, _):
        at = screen(app_test(client), "Nobody Fictional")
        assert "not a clearance" in html_text(at)


def test_viewer_key_cannot_start_a_case(tmp_path):
    with live_api(tmp_path, key=VIEWER_KEY) as (client, service):
        at = screen(app_test(client), "Luma Vesper")
        assert not at.exception
        errors = " ".join(e.value for e in at.error)
        assert "forbidden" in errors and VIEWER_KEY not in errors
        assert len(service.list_cases(50, 0)) == 0


def test_unreachable_api_is_reported(tmp_path):
    httpx = pytest.importorskip("httpx")
    from watchlist.ui.client import ClientConfig, WatchlistClient

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    http = httpx.Client(
        base_url="http://127.0.0.1:9", transport=httpx.MockTransport(refuse)
    )
    client = WatchlistClient(
        ClientConfig("http://127.0.0.1:9", REVIEWER_KEY), http=http
    )
    at = screen(app_test(client), "Luma Vesper")
    assert not at.exception
    assert "api\\_unreachable" in " ".join(e.value for e in at.error)
    at.radio(key="nav").set_value("Case list")
    at.run()
    assert not at.exception
    assert "api\\_unreachable" in " ".join(e.value for e in at.error)


def test_retrieval_failure_then_recover(tmp_path):
    retriever = FakeRetriever(fail=True)
    with live_api(tmp_path, retriever) as (client, service):
        at = screen(app_test(client), "Luma Vesper")
        assert not at.exception
        errors = " ".join(e.value for e in at.error)
        assert "retrieval\\_failed" in errors
        (summary,) = service.list_cases(50, 0)
        retriever.fail = False
        at.radio(key="nav").set_value("Case list")
        at.run()
        at.selectbox(key="open_case_id").select(summary.case_id)
        button(at, "Open case").click()
        at.run()
        button(at, "Recover case").click()
        at.run()
        assert not at.exception
        assert "Recovery finished" in page_text(at)
        assert service.get_case(summary.case_id).status == "awaiting_review"
        assert form_ids(at)


def test_cancel_is_not_an_outcome(tmp_path):
    with live_api(tmp_path) as (client, service):
        at = screen(app_test(client), "Luma Vesper")
        case_id = at.session_state["case_id"]
        button(at, "Cancel case").click()
        at.run()
        assert "cancellation reason is required" in page_text(at)
        at.text_input(key=f"cancel_reason_{case_id}").input("Fictional duplicate.")
        button(at, "Cancel case").click()
        at.run()
        assert not at.exception
        assert service.get_case(case_id).status == "cancelled"
        assert service.get_case(case_id).review is None
        assert "Cancellation is not a review outcome" in html_text(at)


def test_lost_review_response_then_reload_shows_recorded_review(tmp_path):
    with live_api(tmp_path) as (inner, service):
        client = LosingClient(inner, "submit_review")
        at = screen(app_test(client), "Luma Vesper")
        at = review(at, "unresolved", "Fictional: recorded despite lost response.")
        button(at, "Reload case").click()
        at.run()
        assert not at.exception
        assert "Fictional: recorded despite lost response." in html_text(at)
        assert not [t for t in at.text_area if t.label.startswith("Your unsent notes")]
        assert not [b for b in at.button if b.label == "Submit review"]
        assert len(client.calls) == 1
        assert service.get_case(at.session_state["case_id"]).status == "completed"


# -- review findings (completion-25 adversarial review) ---------------------------------


def test_audit_view_is_dropped_when_the_case_changes(tmp_path):
    with live_api(tmp_path) as (client, _):
        at = screen(app_test(client), "Luma Vesper")
        case_id = at.session_state["case_id"]
        button(at, "Load audit and verify").click()
        at.run()
        before = client.audit(case_id)["audit"]["events"]
        assert md_plain(f"verified locally: {len(before)} events") in page_text(at)
        assert "for case revision" in page_text(at)
        at = review(at, "unresolved", "Fictional: audit refresh regression.")
        assert not at.exception
        # The old "verified" result must not survive the review.
        assert case_id not in at.session_state["audit"]
        assert "Audit chain verified locally" not in page_text(at)
        assert not at.dataframe
        button(at, "Load audit and verify").click()
        at.run()
        after = client.audit(case_id)["audit"]["events"]
        assert len(after) > len(before)
        assert md_plain(f"verified locally: {len(after)} events") in page_text(at)
        assert len(at.dataframe[0].value) == len(after)
        assert "earlier case revision" not in page_text(at)


def test_audit_from_an_older_revision_is_flagged(tmp_path):
    with live_api(tmp_path) as (client, _):
        at = screen(app_test(client), "Luma Vesper")
        case_id = at.session_state["case_id"]
        button(at, "Load audit and verify").click()
        at.run()
        loaded = dict(at.session_state["audit"][case_id])
        loaded["revision"] = "0" * 64
        at.session_state["audit"] = {case_id: loaded}
        at.run()
        assert not at.exception
        assert "loaded for an earlier case revision" in page_text(at)


def test_origin_filter_change_resets_page_offset(tmp_path):
    with live_api(tmp_path) as (client, _):
        at = screen(app_test(client), "Luma Vesper", origin="test")
        at.radio(key="nav").set_value("Case list")
        at.session_state["cases_offset"] = 50
        at.run()
        assert "No cases on this page." in page_text(at)
        at.selectbox(key="cases_origin").select("test")
        at.run()
        assert not at.exception
        assert at.session_state["cases_offset"] == 0
        assert "No cases on this page." not in page_text(at)
        assert at.session_state["case_id"] in list(at.dataframe[0].value["case_id"])


def test_environment_key_does_not_unlock_any_browser_session(monkeypatch):
    from test_ui_support import APP_PATH

    monkeypatch.setenv("WATCHLIST_API_KEY", REVIEWER_KEY)
    for _ in range(2):
        at = AppTest.from_file(str(APP_PATH), default_timeout=60).run()
        assert not at.exception
        assert at.text_input(key="session_api_key").value == ""
        assert not [b for b in at.button if b.label == "Screen name"]
        assert "_watchlist_ui_client" not in at.session_state
        assert REVIEWER_KEY not in page_text(at) + html_text(at)


def test_session_login_uses_supplied_key_and_logout_drops_evidence(
    tmp_path, monkeypatch
):
    from test_ui_support import APP_PATH
    from watchlist.ui import client as client_module

    original = client_module.WatchlistClient
    monkeypatch.setenv("WATCHLIST_API_KEY", VIEWER_KEY)
    with live_api(tmp_path) as (client, service):
        monkeypatch.setattr(
            client_module,
            "WatchlistClient",
            lambda config: original(config, http=client._http),
        )
        at = AppTest.from_file(str(APP_PATH), default_timeout=60).run()
        at.text_input(key="session_api_key").input(REVIEWER_KEY)
        button(at, "Sign in").click().run()
        assert not at.exception
        assert "session_api_key" not in at.session_state
        at = screen(at, "Luma Vesper")
        assert not at.exception
        (case,) = service.list_cases()
        assert service.get_case(case.case_id).header.actor == "fictional-reviewer"
        button(at, "Sign out").click().run()
        assert not at.exception
        for key in ("_watchlist_ui_client", "case_payload", "forms", "audit"):
            assert key not in at.session_state
        assert at.text_input(key="session_api_key").value == ""


def test_documented_ui_launches_reject_rebound_hosts(monkeypatch):
    import shlex
    import yaml
    from click.testing import CliRunner
    from streamlit.web import cli
    from streamlit.web.server.starlette import starlette_websocket as ws
    from test_ui_support import APP_PATH

    root = APP_PATH.parents[3]
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    target = (
        makefile.split("serve-ui:\n", 1)[1].split("\n\n", 1)[0].replace("\\\n", " ")
    )
    # Preserve the actual launch flags, replacing only Make's port variable.
    flags = shlex.split(
        target.split("src/watchlist/ui/app.py", 1)[1].replace("$(UI_PORT)", "8501")
    )
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    command = compose["services"]["ui"]["command"]
    container_flags = command[command.index("--server.address=0.0.0.0") :]
    assert "WATCHLIST_API_KEY" not in compose["services"]["ui"]["environment"]
    assert compose["services"]["api"]["ports"] == ["127.0.0.1:8501:8501"]
    captured = []
    monkeypatch.setattr(
        cli,
        "_main_run",
        lambda target, args, flag_options: captured.append((args, flag_options)),
    )
    for launch in (flags, container_flags):
        result = CliRunner().invoke(cli.main, ["run", str(APP_PATH), *launch])
        assert result.exit_code == 0, result.output
        args, options = captured.pop()
        assert not args  # catches misspelled/repeated CLI option syntax
        assert options["server_allowedHosts"] == ("localhost", "127.0.0.1")
        assert options["browser_gatherUsageStats"] is False
        with monkeypatch.context() as patch:
            original = ws.config.get_option
            patch.setattr(
                ws.config,
                "get_option",
                lambda key: (
                    list(options["server_allowedHosts"])
                    if key == "server.allowedHosts"
                    else original(key)
                ),
            )
            assert ws._is_host_allowed("localhost:8501")
            assert ws._is_host_allowed("127.0.0.1:8501")
            assert not ws._is_host_allowed("attacker.example:8501")
    assert "--server.address 127.0.0.1" in target
