"""Pure display helpers for the reviewer interface (no Streamlit import).

Rule for record text: every name, alias, reason, note, explanation, actor
label or other value that came from the API is passed through ``esc`` before
it is placed in HTML, and is wrapped in an element with ``dir="auto"`` so
right-to-left scripts (for example Arabic) render in their own direction.
``app.py`` sends these strings to ``st.html`` (no Markdown processing,
sanitized by Streamlit) and never to ``st.markdown(..., unsafe_allow_html=True)``.

Nothing here selects or ranks an outcome. Scores are labelled as raw,
uncalibrated similarity, and prior annotations as dated reviewer opinions.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re
from typing import Any, Mapping, Sequence

from watchlist.ui.client import OUTCOMES, ApiClientError

__all__ = [
    "MEMORY_PROVIDER",
    "OUTCOME_LABELS",
    "RESEARCH_NOTICE",
    "SCORE_LABEL",
    "CandidateRow",
    "MemoryCard",
    "MemoryKey",
    "MemoryPanel",
    "audit_rows",
    "bidi",
    "candidate_rows",
    "candidates_html",
    "case_header_html",
    "error_text",
    "esc",
    "memory_html",
    "md_plain",
    "memory_panel",
    "other_context",
    "review_choice_errors",
    "review_html",
]

RESEARCH_NOTICE = (
    "Research-only prototype over fictional reference data. It shows possible "
    "record matches for human review. Similarity scores are uncalibrated and are "
    "not identity probabilities. Nothing shown here establishes identity, "
    "wrongdoing, sanctions status or clearance; 'none selected' is not a "
    "clearance. Only you, the human reviewer, record an outcome."
)
SCORE_LABEL = "Uncalibrated similarity score (raw; not a probability)"
MEMORY_PROVIDER = "case_memory"

OUTCOME_LABELS: dict[str, str] = {
    "candidate_for_follow_up": (
        "candidate_for_follow_up: one listed record deserves further follow-up "
        "(not an identity finding)"
    ),
    "none_selected": (
        "none_selected: no listed record selected for follow-up (NOT a clearance)"
    ),
    "unresolved": "unresolved: cannot decide from this evidence",
}
assert tuple(OUTCOME_LABELS) == OUTCOMES

_STATUS_TEXT = {
    "preparing": "Preparing: candidates are not ready yet.",
    "awaiting_review": "Awaiting a human review.",
    "finalizing": "Review accepted; the final audit event is still pending.",
    "completed": "Completed: one human review was recorded.",
    "cancelled": "Cancelled before review (cancellation is not an outcome).",
    "incompatible": "Incompatible with the running configuration; it can only be cancelled.",
    "inconsistent": "Saved records are inconsistent; nothing was repaired automatically.",
}

# Reviewer-facing guidance per stable error code. The API message is shown too.
_ERROR_GUIDANCE = {
    "stale_submission": (
        "This case changed after you opened the form. Your form was not "
        "submitted. Reload the case and review the current evidence."
    ),
    "submission_conflict": (
        "A different review was already sent with this form's ID. Nothing new "
        "was recorded. Reload the case."
    ),
    "invalid_state": "The case does not accept this action in its current state.",
    "request_conflict": (
        "This screening request ID was already used for different input. Start "
        "a new screening form."
    ),
    "recovery_required": "The case needs recovery before it can be reviewed.",
    "unauthenticated": "The API key was refused. Check WATCHLIST_API_KEY.",
    "forbidden": "Your API key's role cannot perform this action.",
    "case_not_found": "No case with this ID exists in the workspace.",
    "queue_full": "The API is busy. Nothing was recorded; try again shortly.",
    "operation_pending": (
        "The API is still working on this. Do not send a new form: reload the "
        "case first."
    ),
    "api_timeout": (
        "No answer arrived in time. The operation may still finish: reload the "
        "case before doing anything else."
    ),
    "api_unreachable": "The API could not be reached. Is it running?",
    "workspace_busy": "The review workspace is busy; try again shortly.",
    "retrieval_failed": "Candidate retrieval failed. No result was inferred.",
    "context_provider_failed": "A context provider failed. No result was inferred.",
    "invalid_input": "The input was refused by the API.",
}


def esc(value: object) -> str:
    """HTML-escape any value (quotes included); ``None`` becomes an empty string."""
    return html.escape("" if value is None else str(value), quote=True)


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|<>~$&:=^\"'])")


def md_plain(value: object) -> str:
    """Backslash-escape Markdown punctuation so API text shows literally.

    Used for alert boxes (``st.error`` and friends render Markdown): every
    ASCII punctuation character that could start Markdown, HTML, LaTeX
    (``$``) or Streamlit directives (``:red[...]``) is escaped. Newlines are
    collapsed so text cannot start a heading or list.
    """
    text = " ".join(("" if value is None else str(value)).split())
    return _MD_SPECIAL.sub(r"\\\1", text)


def bidi(value: object, *, tag: str = "span", cls: str | None = None) -> str:
    """Escaped text in an element with ``dir="auto"`` (right-to-left safe)."""
    if tag not in ("span", "div", "td", "p", "bdi"):
        raise ValueError("unsupported tag")
    klass = "" if cls is None else f' class="{esc(cls)}"'
    return f'<{tag} dir="auto"{klass}>{esc(value)}</{tag}>'


def _short(digest: object, size: int = 12) -> str:
    text = "" if digest is None else str(digest)
    return text[:size] + ("…" if len(text) > size else "")


# -- candidates -----------------------------------------------------------------


@dataclass(frozen=True)
class CandidateRow:
    """One possible record match with its alias evidence and provenance."""

    rank: int
    entity_id: str
    display_name: str
    entity_type: str
    matched_alias: str
    matched_alias_id: str
    score: float
    score_kind: str
    source: str
    synthetic: bool | None
    attribution: str
    provenance: str


def candidate_rows(case: Mapping[str, Any]) -> tuple[CandidateRow, ...]:
    """Candidates of a case view joined with their alias evidence, in rank order."""
    packet = case.get("packet") or {}
    retrieval = packet.get("retrieval") or {}
    evidence = {item["evidence_id"]: item for item in retrieval.get("evidence", ())}
    rows = []
    for cand in sorted(retrieval.get("candidates", ()), key=lambda c: c["rank"]):
        linked = [evidence[e] for e in cand.get("evidence_ids", ()) if e in evidence]
        # The evidence row of the alias that actually matched, in its original
        # script; fall back to the first linked row.
        first = next(
            (e for e in linked if e.get("alias_id") == cand.get("matched_alias_id")),
            linked[0] if linked else {},
        )
        provenance = first.get("provenance")
        rows.append(
            CandidateRow(
                rank=int(cand["rank"]),
                entity_id=str(cand["entity_id"]),
                display_name=str(cand.get("display_name", "")),
                entity_type=str(cand.get("entity_type", "")),
                matched_alias=str(first.get("original_text", cand["matched_alias"])),
                matched_alias_id=str(cand.get("matched_alias_id", "")),
                score=float(cand["score"]),
                score_kind=str(cand.get("score_kind", "")),
                source=str(first.get("source", "")),
                synthetic=first.get("synthetic"),
                attribution=str(first.get("attribution", "")),
                provenance=(
                    "none recorded"
                    if not provenance
                    else json.dumps(provenance, sort_keys=True, ensure_ascii=False)
                ),
            )
        )
    return tuple(rows)


def candidates_html(rows: Sequence[CandidateRow]) -> str:
    """An escaped HTML table of candidates; empty results say so explicitly."""
    if not rows:
        return (
            '<div class="wl-empty"><p>No candidate records were retrieved for this '
            "name. This is not a clearance and not a finding; it only means the "
            "retriever returned nothing from the fictional reference snapshot.</p></div>"
        )
    head = (
        "<tr><th>Rank</th><th>Entity ID</th><th>Record name</th>"
        "<th>Matched alias (original script)</th>"
        f"<th>{esc(SCORE_LABEL)}</th><th>Provenance</th></tr>"
    )
    body = []
    for row in rows:
        synthetic = {True: "synthetic", False: "not synthetic"}.get(
            row.synthetic, "unknown"
        )
        body.append(
            "<tr>"
            f"<td>{row.rank}</td>"
            f"<td><code>{esc(row.entity_id)}</code></td>"
            f"{bidi(row.display_name, tag='td')}"
            f"{bidi(row.matched_alias, tag='td')}"
            f"<td>{row.score:.2f} <small>({esc(row.score_kind)})</small></td>"
            f"<td>{bidi(row.source)} · {esc(synthetic)}<br>"
            f"<small>{bidi(row.attribution)}</small><br>"
            f"<small>{bidi(row.provenance)}</small></td>"
            "</tr>"
        )
    return (
        '<table class="wl-candidates"><thead>'
        + head
        + "</thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
    )


# -- case header and review ---------------------------------------------------------


def case_header_html(case: Mapping[str, Any]) -> str:
    """Escaped summary of a case: query, origin, status and identifiers."""
    header = case.get("header") or {}
    status = str(case.get("status", ""))
    issues = list(case.get("issues") or ()) + list(
        case.get("compatibility_issues") or ()
    )
    parts = [
        '<div class="wl-case">',
        f"<p><b>Screened name:</b> {bidi(header.get('name'), tag='bdi')}</p>",
        f"<p><b>Normalized:</b> {bidi(header.get('normalized_name'), tag='bdi')}</p>",
        f"<p><b>Case:</b> <code>{esc(case.get('case_id'))}</code> · "
        f"<b>Origin:</b> {esc(header.get('origin'))} (fictional data) · "
        f"<b>Created:</b> {esc(header.get('created_at'))} · "
        f"<b>Started by:</b> {bidi(header.get('actor'), tag='bdi')}</p>",
        f"<p><b>Status:</b> {esc(status)} — {esc(_STATUS_TEXT.get(status, 'Unknown status.'))}"
        f" · <b>Next action:</b> {esc(case.get('next_action'))}</p>",
    ]
    if issues:
        parts.append(
            "<ul>"
            + "".join(f"<li>{bidi(item, tag='bdi')}</li>" for item in issues)
            + "</ul>"
        )
    parts.append("</div>")
    return "".join(parts)


def review_html(case: Mapping[str, Any]) -> str:
    """Escaped accepted review, template explanation and cancellation, if any."""
    parts: list[str] = []
    review = case.get("review")
    if review:
        sub = review.get("submission") or {}
        outcome = str(sub.get("outcome", ""))
        parts.append(
            '<div class="wl-review"><p><b>Recorded human review</b> at '
            f"{esc(review.get('submitted_at'))} by {bidi(sub.get('actor'), tag='bdi')}"
            f" (origin {esc(sub.get('origin'))})</p>"
            f"<p><b>Outcome:</b> {esc(OUTCOME_LABELS.get(outcome, outcome))}</p>"
        )
        if sub.get("candidate_entity_id") is not None:
            parts.append(
                f"<p><b>Selected record:</b> <code>{esc(sub['candidate_entity_id'])}</code></p>"
            )
        parts.append(
            f"<p><b>Reason:</b> {bidi(sub.get('reason'), tag='bdi')}</p></div>"
        )
    explanation = case.get("explanation")
    if explanation:
        parts.append(
            '<div class="wl-explanation"><p><b>Evidence explanation (template '
            f"{esc(explanation.get('template_version'))}):</b></p>"
            f"{bidi(explanation.get('text'), tag='p')}</div>"
        )
    cancellation = case.get("cancellation")
    if cancellation:
        parts.append(
            '<div class="wl-cancel"><p><b>Cancelled</b> at '
            f"{esc(cancellation.get('cancelled_at'))} by "
            f"{bidi(cancellation.get('actor'), tag='bdi')}. Reason: "
            f"{bidi(cancellation.get('reason'), tag='bdi')}</p>"
            "<p>Cancellation is not a review outcome.</p></div>"
        )
    return "".join(parts)


def review_choice_errors(
    outcome: str | None,
    candidate_entity_id: str | None,
    reason: str,
    candidate_ids: Sequence[str],
    *,
    reason_max: int = 1000,
) -> list[str]:
    """Client-side checks before sending a form; the service re-validates everything."""
    errors = []
    if outcome not in OUTCOMES:
        errors.append("Choose an outcome deliberately; none is preselected.")
    if outcome == "candidate_for_follow_up":
        if candidate_entity_id not in candidate_ids:
            errors.append("Choose which listed record deserves follow-up.")
    elif candidate_entity_id is not None:
        errors.append("A record can be selected only for candidate_for_follow_up.")
    if not reason.strip():
        errors.append("A reason is required.")
    elif len(reason) > reason_max:
        errors.append(f"The reason must be at most {reason_max} characters.")
    return errors


# -- prior-annotation memory --------------------------------------------------------------


@dataclass(frozen=True)
class MemoryCard:
    """One dated prior annotation: an earlier reviewer opinion, not an identity fact."""

    reviewed_at: str
    case_id: str
    origin: str
    outcome: str
    relation: str
    interpretation: str
    reviewer_opinion: str
    actor_label: str
    status: str


@dataclass(frozen=True)
class MemoryKey:
    """Prior annotations about one exact reference-record ID."""

    entity_id: str
    namespace_id: str
    shown_count: int
    eligible_count: int
    differing_outcomes: bool
    omitted: dict[str, int]
    cards: tuple[MemoryCard, ...]


@dataclass(frozen=True)
class MemoryPanel:
    """The memory context frozen into a packet (or why it is unavailable)."""

    status: str
    notice: str
    message: str
    lookup_at: str
    warnings: tuple[str, ...]
    keys: tuple[MemoryKey, ...]


def memory_panel(case: Mapping[str, Any]) -> MemoryPanel | None:
    """Parse ``packet.context['case_memory']``; ``None`` when no memory is attached."""
    packet = case.get("packet") or {}
    entry = (packet.get("context") or {}).get(MEMORY_PROVIDER)
    if not isinstance(entry, Mapping):
        return None
    data = entry.get("data")
    if not isinstance(data, Mapping):
        return MemoryPanel("malformed", "", "Memory context is malformed.", "", (), ())
    keys = []
    for key in data.get("keys") or ():
        if not isinstance(key, Mapping):
            continue
        cards = tuple(
            MemoryCard(
                reviewed_at=str(card.get("reviewed_at", "")),
                case_id=str(card.get("case_id", "")),
                origin=str(card.get("origin", "")),
                outcome=str(card.get("outcome", "")),
                relation=str(card.get("relation", "")),
                interpretation=str(card.get("interpretation", "")),
                reviewer_opinion=str(card.get("reviewer_opinion", "")),
                actor_label=str(card.get("actor_label", "")),
                status=str(card.get("status", "")),
            )
            for card in key.get("cards") or ()
            if isinstance(card, Mapping)
        )
        omitted = key.get("omitted") or {}
        keys.append(
            MemoryKey(
                entity_id=str(key.get("source_entity_id", key.get("entity_id", ""))),
                namespace_id=str(key.get("namespace_id", "")),
                shown_count=int(key.get("shown_count", len(cards))),
                eligible_count=int(key.get("eligible_count", len(cards))),
                differing_outcomes=bool(key.get("differing_outcomes", False)),
                omitted={
                    str(k): int(v)
                    for k, v in omitted.items()
                    if isinstance(v, int) and v
                },
                cards=cards,
            )
        )
    return MemoryPanel(
        status=str(data.get("status", "")),
        notice=str(data.get("notice", "")),
        message=str(data.get("message", "")),
        lookup_at=str(data.get("lookup_at", "")),
        warnings=tuple(str(w) for w in data.get("warnings") or ()),
        keys=tuple(keys),
    )


def memory_html(panel: MemoryPanel | None) -> str:
    """Escaped prior-annotation panel, kept separate from candidate evidence."""
    if panel is None:
        return (
            '<div class="wl-memory"><p>No reviewer-memory provider was attached '
            "to this case. This says nothing about earlier reviews.</p></div>"
        )
    parts = ['<div class="wl-memory">']
    if panel.notice:
        parts.append(f"<p><i>{bidi(panel.notice, tag='bdi')}</i></p>")
    if panel.status != "ok":
        parts.append(
            f"<p><b>Memory status:</b> {esc(panel.status)}. "
            f"{bidi(panel.message, tag='bdi')}</p></div>"
        )
        return "".join(parts)
    parts.append(f"<p>Looked up at {esc(panel.lookup_at)}.</p>")
    parts.extend(f"<p><b>Warning:</b> {bidi(w, tag='bdi')}</p>" for w in panel.warnings)
    for key in panel.keys:
        omitted = ", ".join(f"{esc(k)}: {v}" for k, v in sorted(key.omitted.items()))
        parts.append(
            f"<p><b>Record <code>{esc(key.entity_id)}</code></b> — "
            f"{key.shown_count} shown of {key.eligible_count} eligible prior "
            f"annotation(s){'; omitted ' + omitted if omitted else ''}"
            f"{'; earlier reviewers differed' if key.differing_outcomes else ''}.</p>"
        )
        if not key.cards:
            parts.append("<p><small>No displayable prior annotations.</small></p>")
            continue
        parts.append("<ul>")
        for card in key.cards:
            parts.append(
                f"<li><b>{esc(card.reviewed_at)}</b> — earlier case "
                f"<code>{esc(card.case_id)}</code> (origin {esc(card.origin)}): "
                f"{esc(card.outcome)} [{esc(card.relation)}]. "
                f"<small>{bidi(card.interpretation, tag='bdi')}</small><br>"
                f"Earlier reviewer opinion: {bidi(card.reviewer_opinion, tag='bdi')} "
                f"<small>(label {bidi(card.actor_label, tag='bdi')})</small></li>"
            )
        parts.append("</ul>")
    parts.append("</div>")
    return "".join(parts)


def other_context(case: Mapping[str, Any]) -> dict[str, Any]:
    """Display-only context from providers other than reviewer memory."""
    packet = case.get("packet") or {}
    return {
        name: entry
        for name, entry in (packet.get("context") or {}).items()
        if name != MEMORY_PROVIDER
    }


# -- audit ---------------------------------------------------------------------------------


def audit_rows(audit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Plain rows (no HTML) for the audit table; payloads are shown separately."""
    return [
        {
            "sequence": event.get("sequence"),
            "event_type": event.get("event_type"),
            "created_at": event.get("created_at"),
            "event_sha256": _short(event.get("event_sha256"), 16),
            "previous_sha256": _short(event.get("previous_sha256"), 16),
        }
        for event in audit.get("events") or ()
    ]


# -- errors ---------------------------------------------------------------------------------


def error_text(error: ApiClientError) -> str:
    """Plain-text, reviewer-facing description of an API failure (never the key)."""
    guidance = _ERROR_GUIDANCE.get(error.code, "The request failed.")
    status = "no response" if error.status is None else f"HTTP {error.status}"
    retry = " It is safe to retry." if error.retryable else ""
    return f"{guidance} ({error.code}, {status}){retry} API message: {error.message}"
