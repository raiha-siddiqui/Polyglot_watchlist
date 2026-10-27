"""Streamlit reviewer interface over the v2 review API (research only).

Run after starting the API on loopback; enter your own key in the sign-in form::

    make serve-ui

Every operation is an HTTP call through ``watchlist.ui.client``; the API in
turn calls the one shared v2 ``ReviewService``. This module holds page layout
and per-session form state only. It never selects an outcome, never resends a
review with a new submission ID on its own, and never places record text in
HTML without escaping it (see ``components``).

Session-state keys (one browser session each):

``_watchlist_ui_client``
    Optional injected ``WatchlistClient`` (tests); otherwise built from this
    session's supplied key after an authenticated API read succeeds.
``nav``, ``case_id``, ``case_payload``, ``forms``, ``audit``, ``flash``,
``screen_request``, ``cases_offset``
    Navigation, the case response the reviewer is looking at, one review
    form per case (bound to that response's ``revision`` and
    ``packet_sha256`` with its own submission UUID), loaded audit views,
    one-shot messages and the pending screening request ID.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from typing import Any

import streamlit as st

from watchlist.ui.client import (
    OUTCOMES,
    DEFAULT_API_URL,
    ENV_API_URL,
    ApiClientError,
    ClientConfig,
    ReviewForm,
    WatchlistClient,
    new_uuid,
)
from watchlist.ui.components import (
    OUTCOME_LABELS,
    RESEARCH_NOTICE,
    SCORE_LABEL,
    audit_rows,
    candidate_rows,
    candidates_html,
    case_header_html,
    error_text,
    md_plain,
    memory_html,
    memory_panel,
    other_context,
    review_choice_errors,
    review_html,
)

CLIENT_KEY = "_watchlist_ui_client"
PAGES = ("Screen a name", "Current case", "Case list")
UI_ORIGINS = ("simulated_demo", "test", "human")
ORIGIN_HELP = {
    "simulated_demo": "simulated_demo — a fictional demonstration",
    "test": "test — software testing",
    "human": "human — a person reviewing fictional data",
}
STALE_CODES = frozenset({"stale_submission", "submission_conflict", "invalid_state"})
REASON_MAX = 1000
NAME_MAX = 200

_STYLE = """<style>
.wl-candidates { border-collapse: collapse; width: 100%; font-size: 0.92rem; }
.wl-candidates th, .wl-candidates td { border: 1px solid #8884; padding: 4px 6px;
  text-align: start; vertical-align: top; unicode-bidi: plaintext; }
.wl-memory, .wl-review, .wl-explanation, .wl-cancel, .wl-empty, .wl-case {
  border-inline-start: 4px solid #8888; padding-inline-start: 8px; margin: 6px 0; }
.wl-memory { border-color: #b08a2e; }
bdi, [dir=auto] { unicode-bidi: isolate; }
</style>"""


# -- state helpers -------------------------------------------------------------------


def _state() -> Any:
    return st.session_state


def _flash(kind: str, text: str) -> None:
    _state().setdefault("flash", []).append((kind, text))


def _client() -> WatchlistClient:
    """Return this session's authenticated client; never use a server-held key."""
    state = _state()
    client = state.get(CLIENT_KEY)
    if client is None:
        raise ApiClientError("unauthenticated", "Sign in with your own API key.")
    return client


def _on_sign_in() -> None:
    """Validate the supplied key through the API before opening the session."""
    key = _state().pop("session_api_key", "")
    client = None
    try:
        url = os.environ.get(ENV_API_URL, "").strip() or DEFAULT_API_URL
        client = WatchlistClient(ClientConfig(url.rstrip("/"), key or None))
        client.list_cases(limit=1)
    except ApiClientError as exc:
        if client is not None:
            client.close()
        _flash("error", error_text(exc))
        return
    _state()[CLIENT_KEY] = client


def _on_sign_out() -> None:
    """Drop the client, credentials, evidence and unsent forms for this session."""
    client = _state().get(CLIENT_KEY)
    if client is not None:
        client.close()
    _state().clear()


def _show_case(payload: dict[str, Any]) -> None:
    """Make ``payload`` the case on screen (does not touch other cases' forms).

    Any audit view loaded earlier for this case is dropped: the case may have
    changed (review, cancel, recover, reload), and an old "verified" result
    must never be shown next to newer case state. The reviewer reloads it.
    """
    state = _state()
    case_id = payload["case"]["case_id"]
    state.setdefault("audit", {}).pop(case_id, None)
    state["case_id"] = case_id
    state["case_payload"] = payload


def _form_for(payload: dict[str, Any]) -> dict[str, Any]:
    """The review form bound to this exact case response.

    A form keeps its submission UUID across reruns and double clicks. A new
    revision (after a reload) gets a new form with a new UUID; unsent notes
    from the old form are carried over, the outcome is not.
    """
    forms = _state().setdefault("forms", {})
    case_id = payload["case"]["case_id"]
    form = forms.get(case_id)
    if form is not None and form["revision"] == payload["revision"]:
        return form
    notes = ""
    if form is not None:
        notes = _state().get(f"reason_{form['submission_id']}", "") or form["notes"]
    form = {
        "submission_id": new_uuid(),
        "revision": payload["revision"],
        "packet_sha256": payload["case"]["packet_sha256"],
        "stale": False,
        "sent": False,
        "notes": notes,
    }
    forms[case_id] = form
    return form


# -- callbacks (run before the page body) ------------------------------------------------


def _on_screen() -> None:
    state = _state()
    name = state.get("screen_name", "") or ""
    origin = state.get("screen_origin")
    if not name.strip():
        _flash("error", "Enter a name to screen.")
        return
    pending = state.get("screen_request")
    if not pending or (pending["name"], pending["origin"]) != (name, origin):
        # One request ID per screening input: a double click or a retry after a
        # lost response returns the same case instead of creating another.
        pending = {"name": name, "origin": origin, "request_id": new_uuid()}
        state["screen_request"] = pending
    try:
        payload = _client().start_case(
            name, origin=origin, request_id=pending["request_id"]
        )
    except ApiClientError as exc:
        _flash("error", error_text(exc))
        return
    state.pop("screen_request", None)
    _show_case(payload)
    state["nav"] = "Current case"
    _flash("success", "Case started. Review the possible record matches below.")


def _on_reload() -> None:
    state = _state()
    case_id = state.get("case_id")
    if not case_id:
        return
    try:
        payload = _client().get_case(case_id)
    except ApiClientError as exc:
        _flash("error", error_text(exc))
        return
    form = state.get("forms", {}).get(case_id)
    if form is not None and form["stale"]:
        # A stale form is never revived, even if the revision looks unchanged:
        # the reviewer gets a fresh form (new submission ID, notes carried over).
        form["notes"] = state.get(f"reason_{form['submission_id']}") or form["notes"]
        form["revision"] = None
    _show_case(payload)


def _on_open(case_id: str | None = None) -> None:
    state = _state()
    case_id = case_id or state.get("open_case_id")
    if not case_id:
        _flash("error", "Choose a case to open.")
        return
    try:
        _show_case(_client().get_case(case_id))
    except ApiClientError as exc:
        _flash("error", error_text(exc))
        return
    state["nav"] = "Current case"


def _on_submit(case_id: str) -> None:
    state = _state()
    payload = state.get("case_payload")
    form = state.get("forms", {}).get(case_id)
    if not payload or form is None or payload["case"]["case_id"] != case_id:
        _flash("error", "This form no longer matches the case on screen; reload it.")
        return
    if form["stale"]:
        _flash("error", "This form is stale and cannot be submitted; reload the case.")
        return
    sid = form["submission_id"]
    outcome = state.get(f"outcome_{sid}")
    candidate = (
        state.get(f"candidate_{sid}") if outcome == "candidate_for_follow_up" else None
    )
    reason = state.get(f"reason_{sid}", "") or ""
    ids = [row.entity_id for row in candidate_rows(payload["case"])]
    problems = review_choice_errors(
        outcome, candidate, reason, ids, reason_max=REASON_MAX
    )
    if problems:
        for problem in problems:
            _flash("error", problem)
        return
    review = ReviewForm(
        submission_id=sid,
        revision=form["revision"],
        packet_sha256=form["packet_sha256"],
        outcome=outcome,
        candidate_entity_id=candidate,
        reason=reason,
    )
    try:
        result = _client().submit_review(case_id, review)
    except ApiClientError as exc:
        if exc.code in STALE_CODES:
            form["stale"] = True
            form["notes"] = reason
            if exc.details.get("next_action") == "start_case":
                _flash(
                    "error",
                    "Earlier annotations changed or expired. Start a new case to review fresh context; reloading this frozen case cannot refresh it. Your notes are kept below.",
                )
                return
            _flash(
                "error",
                error_text(exc)
                + " Your notes are kept below; submission is disabled until you "
                "reload the case and re-check the evidence.",
            )
        elif exc.retryable or exc.code in ("api_timeout", "operation_pending"):
            _flash(
                "warning",
                error_text(exc)
                + " Reload the case to see whether the review was recorded. If it "
                "was not, pressing Submit again resends this same form (same "
                "submission ID), which cannot record a second review.",
            )
        else:
            _flash("error", error_text(exc))
        return
    form["sent"] = True
    _show_case(result)
    _flash("success", "Your review was recorded. It is a human opinion, not a finding.")


def _on_recover(case_id: str) -> None:
    try:
        _show_case(_client().recover(case_id))
    except ApiClientError as exc:
        _flash("error", error_text(exc))
        return
    _flash("success", "Recovery finished; no review was invented.")


def _on_cancel(case_id: str) -> None:
    reason = _state().get(f"cancel_reason_{case_id}", "") or ""
    if not reason.strip():
        _flash("error", "A cancellation reason is required.")
        return
    try:
        _show_case(_client().cancel(case_id, reason))
    except ApiClientError as exc:
        _flash("error", error_text(exc))
        return
    _flash("info", "Case cancelled. Cancellation is not a review outcome.")


def _on_audit(case_id: str) -> None:
    client = _client()
    try:
        audit = client.audit(case_id)
        verification = client.verify_audit(case_id)
    except ApiClientError as exc:
        _flash("error", error_text(exc))
        return
    payload = _state().get("case_payload") or {}
    on_screen = (payload.get("case") or {}).get("case_id") == case_id
    _state().setdefault("audit", {})[case_id] = {
        "audit": audit["audit"],
        "verification": verification["verification"],
        "revision": payload.get("revision") if on_screen else None,
        "loaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _on_list_page(offset: int) -> None:
    _state()["cases_offset"] = max(0, offset)


# -- pages ----------------------------------------------------------------------------


def _render_flash() -> None:
    for kind, text in _state().pop("flash", []):
        getattr(st, kind)(md_plain(text))


def _page_screen() -> None:
    st.header("Screen a name")
    st.caption(
        "Enter a fictional name. The service retrieves possible record matches from "
        "the packaged fictional reference snapshot and stops for your review."
    )
    with st.form("screen_form"):
        st.text_input(
            "Name to screen (fictional data only)",
            max_chars=NAME_MAX,
            key="screen_name",
        )
        st.selectbox(
            "Origin label for this case",
            UI_ORIGINS,
            format_func=ORIGIN_HELP.get,
            key="screen_origin",
        )
        st.form_submit_button("Screen name", on_click=_on_screen)


def _render_candidates(case: dict[str, Any]) -> None:
    rows = candidate_rows(case)
    retrieval = (case.get("packet") or {}).get("retrieval") or {}
    retriever = retrieval.get("retriever") or {}
    st.subheader("Possible record matches")
    st.caption(
        f"{SCORE_LABEL}. Ranking is by the retriever "
        f"({retriever.get('kind', '?')} {retriever.get('version', '')}); a high score "
        "is name similarity only and does not establish identity."
    )
    st.html(candidates_html(rows))


def _render_review_form(payload: dict[str, Any]) -> None:
    case = payload["case"]
    case_id = case["case_id"]
    form = _form_for(payload)
    sid = form["submission_id"]
    rows = candidate_rows(case)
    st.subheader("Your review")
    st.caption(
        "Choose one outcome deliberately. 'none_selected' means you selected no "
        "listed record; it is not a clearance."
    )
    if form["stale"]:
        st.error(
            md_plain(
                "This form is stale: the case changed or a different review exists. "
                "Submission is disabled. Reload the case to review the current evidence; "
                "your notes will be carried over."
            )
        )
    outcome = st.radio(
        "Outcome",
        OUTCOMES,
        index=None,
        format_func=OUTCOME_LABELS.get,
        key=f"outcome_{sid}",
        disabled=form["stale"],
    )
    if outcome == "candidate_for_follow_up":
        labels = {row.entity_id: f"#{row.rank} {row.entity_id}" for row in rows}
        st.selectbox(
            "Record that deserves follow-up",
            list(labels),
            index=None,
            format_func=labels.get,
            key=f"candidate_{sid}",
            disabled=form["stale"],
        )
    reason_key = f"reason_{sid}"
    if reason_key not in _state():
        _state()[reason_key] = form["notes"]
    st.text_area(
        "Reason (required)",
        max_chars=REASON_MAX,
        key=reason_key,
        help="Your own words. Do not paste real personal data.",
    )
    st.caption(
        f"Form bound to case revision {form['revision'][:12]}… and packet "
        f"{form['packet_sha256'][:12]}…; submission ID {sid}."
    )
    st.button(
        "Submit review",
        type="primary",
        on_click=_on_submit,
        args=(case_id,),
        disabled=form["stale"],
        key=f"submit_{sid}",
    )


def _render_unsent_notes(case: dict[str, Any]) -> None:
    """Keep a stale form's notes visible; hide them once this form was accepted."""
    form = _state().get("forms", {}).get(case["case_id"])
    accepted = ((case.get("review") or {}).get("submission") or {}).get("submission_id")
    if not form or form.get("sent") or accepted == form["submission_id"]:
        return
    notes = _state().get(f"reason_{form['submission_id']}", "") or form["notes"]
    if notes.strip():
        st.text_area(
            "Your unsent notes (not submitted)",
            value=notes,
            disabled=True,
            key=f"unsent_{form['submission_id']}",
        )


def _render_audit(case_id: str) -> None:
    st.subheader("Audit log")
    st.button("Load audit and verify", on_click=_on_audit, args=(case_id,))
    loaded = _state().get("audit", {}).get(case_id)
    if not loaded:
        return
    current = (_state().get("case_payload") or {}).get("revision")
    revision = loaded.get("revision")
    st.caption(
        md_plain(
            f"Audit loaded at {loaded.get('loaded_at')} (UTC) for case revision "
            f"{str(revision)[:12]}…; press the button again after any change."
        )
    )
    if revision != current:
        st.warning(
            md_plain(
                "This audit view was loaded for an earlier case revision and may be "
                "out of date. Load it again."
            )
        )
    verification = loaded["verification"]
    if verification.get("ok"):
        st.success(
            md_plain(
                f"Audit chain verified locally: {verification.get('count')} events, head "
                f"{str(verification.get('head_sha256'))[:16]}. This is tamper evidence "
                "for this workspace, not authentication of who acted."
            )
        )
    else:
        issues = "; ".join(str(i) for i in verification.get("issues") or ())
        st.error(md_plain(f"Audit verification FAILED: {issues}"))
    st.dataframe(audit_rows(loaded["audit"]), hide_index=True)
    with st.expander("Event payloads (raw JSON, shown as text)"):
        for event in loaded["audit"].get("events") or ():
            st.json(event.get("payload"), expanded=False)


def _page_case() -> None:
    state = _state()
    payload = state.get("case_payload")
    if not payload:
        st.info("No case open. Screen a name or open one from the case list.")
        return
    case = payload["case"]
    case_id = case["case_id"]
    st.header("Current case")
    st.button("Reload case", on_click=_on_reload)
    st.html(case_header_html(case))
    if case.get("packet"):
        _render_candidates(case)
        st.subheader("Prior annotations (reviewer memory)")
        st.caption(
            "Dated earlier reviewer opinions about the same exact record IDs. They are "
            "not identity facts and do not decide this review."
        )
        st.html(memory_html(memory_panel(case)))
        extra = other_context(case)
        if extra:
            with st.expander("Other display-only context"):
                st.json(extra, expanded=False)
    status = case.get("status")
    if status == "awaiting_review" and case.get("next_action") == "submit_review":
        _render_review_form(payload)
    else:
        summary = review_html(case)
        if summary:
            st.html(summary)
        _render_unsent_notes(case)
    if case.get("next_action") == "recover":
        st.button("Recover case", on_click=_on_recover, args=(case_id,))
    if status in ("preparing", "awaiting_review", "incompatible"):
        with st.expander("Cancel this case (not a review outcome)"):
            st.text_input("Cancellation reason", key=f"cancel_reason_{case_id}")
            st.button("Cancel case", on_click=_on_cancel, args=(case_id,))
    _render_audit(case_id)


def _page_cases() -> None:
    state = _state()
    st.header("Case list")
    st.caption("Summaries only: names, evidence and notes are shown inside a case.")
    origin = st.selectbox(
        "Origin filter",
        ("all",) + UI_ORIGINS + ("unknown",),
        key="cases_origin",
        on_change=_on_list_page,
        args=(0,),
    )
    offset = int(state.get("cases_offset", 0))
    try:
        page = _client().list_cases(
            limit=50, offset=offset, origin=None if origin == "all" else origin
        )
    except ApiClientError as exc:
        st.error(md_plain(error_text(exc)))
        return
    cases = page["cases"]
    if not cases:
        st.info("No cases on this page.")
    else:
        st.dataframe(cases, hide_index=True)
        st.selectbox(
            "Case to open",
            [item["case_id"] for item in cases],
            index=None,
            key="open_case_id",
        )
        st.button("Open case", on_click=_on_open)
    cols = st.columns(2)
    with cols[0]:
        if offset > 0:
            st.button("Previous page", on_click=_on_list_page, args=(offset - 50,))
    with cols[1]:
        if page.get("next_offset") is not None:
            st.button("Next page", on_click=_on_list_page, args=(page["next_offset"],))


def _sidebar(client: WatchlistClient) -> None:
    with st.sidebar:
        st.radio("Page", PAGES, key="nav")
        st.button("Sign out", on_click=_on_sign_out)
        st.divider()
        st.caption(md_plain(f"API: {client.config.base_url}"))
        st.caption(
            "API key: configured"
            if client.config.has_key
            else "API key: NOT configured"
        )


def main() -> None:
    """Render one run of the reviewer interface."""
    st.set_page_config(page_title="Watchlist reviewer (research only)", layout="wide")
    st.title("Polyglot Watchlist — reviewer (research only)")
    st.warning(md_plain(RESEARCH_NOTICE))
    st.html(_STYLE)
    if CLIENT_KEY not in _state():
        _render_flash()
        st.caption(
            "Sign in with your own viewer or reviewer API key. It stays in this session."
        )
        with st.form("sign_in"):
            st.text_input(
                "API key", type="password", key="session_api_key", max_chars=512
            )
            st.form_submit_button("Sign in", on_click=_on_sign_in)
        return
    client = _client()
    _state().setdefault("nav", PAGES[0])
    _sidebar(client)
    _render_flash()
    page = _state()["nav"]
    if page == "Screen a name":
        _page_screen()
    elif page == "Current case":
        _page_case()
    else:
        _page_cases()


if __name__ == "__main__":
    main()
