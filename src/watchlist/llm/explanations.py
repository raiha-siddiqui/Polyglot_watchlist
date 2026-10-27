"""Grounded explanations of a frozen review packet.

The model only *selects* atomic claims and cites packet IDs; it writes no
prose. Code renders every sentence from the cited packet values, and the model
must cover the packet's core facts (count, and each candidate's rank, score
and matched alias), so it cannot present a slanted subset. On any failure the
deterministic claim set is rendered instead. The packet is deep-copied and
never written back: an explanation cannot change candidates, scores, review
state or decisions.
"""

import copy
import json
import math

from watchlist.llm import RESEARCH_LIMITATION, prompts
from watchlist.llm.client import LLMClient, LLMConfig, call_model
from watchlist.llm.schemas import (
    CLAIM_TYPES,
    Claim,
    ExplanationResult,
    canonical,
    sha256_text,
)
from watchlist.llm.validation import validate_claims, winning_evidence

LLM_TEMPLATE = "llm-claims-v2"
FALLBACK_TEMPLATE = "evidence-claims-v2"
MAX_CANDIDATES = 50
MAX_TEXT_CHARS = 10000
_CANDIDATE_FIELDS = (
    "entity_id",
    "display_name",
    "entity_type",
    "rank",
    "matched_alias_id",
    "matched_alias",
    "evidence_ids",
    "similarity_score",
    "score_kind",
)
_EVIDENCE_FIELDS = (
    "evidence_id",
    "entity_id",
    "alias_id",
    "original_text",
    "source",
    "synthetic",
)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS:
        raise ValueError(
            f"{label} must be nonblank text of at most {MAX_TEXT_CHARS} characters"
        )
    return value


def _check_links(c: dict, index: dict[str, dict]) -> None:
    """The review-v1 packet rules (agent.state.validate_packet) for one candidate's evidence."""
    refs = c["evidence_ids"]
    if (
        not isinstance(refs, list)
        or not refs
        or any(not isinstance(r, str) for r in refs)
        or len(set(refs)) != len(refs)
    ):
        raise ValueError(
            "candidate evidence_ids must be a nonempty list of unique strings"
        )
    linked = [index.get(r) for r in refs]
    if any(
        e is None or e["kind"] != "evidence" or e["entity_id"] != c["entity_id"]
        for e in linked
    ):
        raise ValueError("candidate evidence links are invalid")
    if len({e["alias_id"] for e in linked}) != len(linked):
        raise ValueError("candidate evidence repeats an alias ID")
    winners = [e for e in linked if e["alias_id"] == c["matched_alias_id"]]
    if len(winners) != 1 or winners[0]["original_text"] != c["matched_alias"]:
        raise ValueError("winning alias evidence missing or inconsistent")


def build_evidence(packet: object) -> tuple[dict, dict[str, dict], str]:
    """Return (model view, citation index, packet hash) for a review packet.

    Accepts the ``candidates``/``evidence`` shape of a review-v1 state and
    applies its link rules: unique string evidence references, no cross-entity
    or orphan evidence, and one winning evidence item whose text equals the
    matched alias. Only fields needed for explanation are exposed: reviewer
    reasons, memory notes and audit fields are deliberately omitted so that
    earlier opinions are never mixed with source facts.
    """
    if not isinstance(packet, dict):
        raise ValueError("packet must be an object")
    packet = copy.deepcopy(packet)
    candidates, evidence = packet.get("candidates"), packet.get("evidence")
    if (
        not isinstance(candidates, list)
        or len(candidates) > MAX_CANDIDATES
        or not isinstance(evidence, list)
    ):
        raise ValueError("packet needs candidate and evidence lists")
    index: dict[str, dict] = {
        "packet": {"kind": "packet", "candidate_count": len(candidates)}
    }
    items: list[dict] = [
        {"id": "packet", "kind": "packet", "candidate_count": len(candidates)}
    ]
    for e in evidence:
        if not isinstance(e, dict) or any(k not in e for k in _EVIDENCE_FIELDS):
            raise ValueError("evidence item is missing fields")
        for k in _EVIDENCE_FIELDS[:-1]:
            _text(e[k], k)
        if type(e["synthetic"]) is not bool:
            raise ValueError("synthetic must be boolean")
        provenance = e.get("provenance")
        fields = {k: e[k] for k in _EVIDENCE_FIELDS}
        if isinstance(provenance, dict):
            fields["snapshot_id"] = provenance.get("snapshot_id")
            fields["dataset"] = provenance.get("dataset")
        if e["evidence_id"] in index:
            raise ValueError("duplicate citation ID")
        index[e["evidence_id"]] = {"kind": "evidence", **fields}
    used: set[str] = set()
    for rank, c in enumerate(candidates, 1):
        if not isinstance(c, dict) or any(k not in c for k in _CANDIDATE_FIELDS):
            raise ValueError("candidate is missing fields")
        for k in (
            "entity_id",
            "display_name",
            "entity_type",
            "matched_alias_id",
            "matched_alias",
            "score_kind",
        ):
            _text(c[k], k)
        score = c["similarity_score"]
        if type(c["rank"]) is not int or c["rank"] != rank:
            raise ValueError("candidate ranks must be 1..n in order")
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError("similarity_score must be finite")
        if c["entity_id"] in index:
            raise ValueError("duplicate citation ID")
        _check_links(c, index)
        used.update(c["evidence_ids"])
        index[c["entity_id"]] = {
            "kind": "candidate",
            **{k: c[k] for k in _CANDIDATE_FIELDS},
        }
    if used != {k for k, v in index.items() if v["kind"] == "evidence"}:
        raise ValueError("orphan evidence is not linked to any candidate")
    for key, item in index.items():
        if key != "packet":
            items.append(
                {"id": key, **{k: v for k, v in item.items() if k != "evidence_ids"}}
            )
    view = {"query": packet.get("query"), "items": items}
    return view, index, sha256_text(canonical(view))


def _quote(value: object) -> str:
    """Render untrusted strings with visible escapes (bidi/zero-width included)."""
    return json.dumps(value, ensure_ascii=True)


def render_claim(claim: dict) -> str:
    """Deterministic authoritative wording for one validated claim."""
    v, cites = claim["values"], claim["citations"]
    ref = "[" + ", ".join(_quote(c) for c in cites) + "]"
    match claim["claim_type"]:
        case "candidate_count":
            return f"Saved retrieval returned {v['candidate_count']} candidate records {ref}."
        case "candidate_rank":
            return f"Candidate {_quote(cites[0])} is at rank {v['rank']} {ref}."
        case "similarity_score":
            return (
                f"Candidate {_quote(cites[0])} has {_quote(v['score_kind'])} {v['similarity_score']!r}; "
                f"this is a name-similarity score, not an identity probability {ref}."
            )
        case "matched_alias":
            return (
                f"Candidate {_quote(cites[0])} scored highest on alias {_quote(v['matched_alias'])} "
                f"({_quote(v['matched_alias_id'])}) {ref}."
            )
        case "alias_source":
            synthetic = (
                "fictional synthetic record"
                if v["synthetic"]
                else "non-synthetic record"
            )
            return f"Evidence {_quote(cites[0])} comes from source {_quote(v['source'])} ({synthetic}) {ref}."
    raise ValueError("unknown claim type")


def deterministic_claims(index: dict[str, dict]) -> list[dict]:
    """Every fact the packet supports, in rank order, without any model input."""
    claims = [
        {
            "claim_type": "candidate_count",
            "citations": ["packet"],
            "values": {"candidate_count": index["packet"]["candidate_count"]},
        }
    ]
    candidates = sorted(
        (i for i in index.values() if i["kind"] == "candidate"), key=lambda i: i["rank"]
    )
    for c in candidates:
        cid = c["entity_id"]
        claims.append(
            {
                "claim_type": "candidate_rank",
                "citations": [cid],
                "values": {"rank": c["rank"]},
            }
        )
        claims.append(
            {
                "claim_type": "similarity_score",
                "citations": [cid],
                "values": {
                    "similarity_score": c["similarity_score"],
                    "score_kind": c["score_kind"],
                },
            }
        )
        claims.append(
            {
                "claim_type": "matched_alias",
                "citations": [cid, winning_evidence(index, c)],
                "values": {
                    "matched_alias_id": c["matched_alias_id"],
                    "matched_alias": c["matched_alias"],
                },
            }
        )
        for ref in c["evidence_ids"]:
            e = index[ref]
            claims.append(
                {
                    "claim_type": "alias_source",
                    "citations": [ref],
                    "values": {"source": e["source"], "synthetic": e["synthetic"]},
                }
            )
    return claims


def _result(
    claims: list[dict],
    template: str,
    method: str,
    packet_sha: str,
    reason: str | None,
    call: dict | None,
    prompt_id: str | None,
) -> ExplanationResult:
    order = {kind: n for n, kind in enumerate(CLAIM_TYPES)}
    built = tuple(
        Claim(
            c["claim_type"],
            tuple(c["citations"]),
            tuple(sorted(c["values"].items())),
            render_claim(c),
        )
        for c in claims
    )
    if method == "llm_grounded_claims":
        built = tuple(sorted(built, key=lambda c: (order[c.claim_type], c.citations)))
    text = " ".join(c.text for c in built) + " " + RESEARCH_LIMITATION
    return ExplanationResult(
        template_version=template,
        method=method,
        claims=built,
        text=text,
        packet_sha256=packet_sha,
        fallback_reason=reason,
        limitations=RESEARCH_LIMITATION,
        model_call=call,
        prompt_id=prompt_id,
    )


def explain_packet(
    packet: dict, client: LLMClient | None = None, config: LLMConfig | None = None
) -> ExplanationResult:
    """Explain a frozen packet with model-selected claims, or deterministically.

    Raises ValueError only when the packet itself is malformed. The returned
    object is advisory; callers must not copy any of it into workflow state.
    """
    view, index, packet_sha = build_evidence(packet)
    if client is None:
        return _result(
            deterministic_claims(index),
            FALLBACK_TEMPLATE,
            "deterministic_fallback",
            packet_sha,
            "model_not_configured",
            None,
            None,
        )
    request = prompts.explain_request(
        view, config.max_output_tokens["explain"] if config else 8192
    )
    if config and request.size_chars() > config.max_request_chars:
        return _result(
            deterministic_claims(index),
            FALLBACK_TEMPLATE,
            "deterministic_fallback",
            packet_sha,
            "request_too_large",
            None,
            request.prompt_id,
        )
    response, error = call_model(client, request)
    call = None if response is None else response.accounting()
    if error is None:
        try:
            claims = validate_claims(
                response.text, index, config.max_output_chars if config else 20000
            )
        except Exception as exc:  # noqa: BLE001 - any validation failure must fall back.
            error = f"validation:{getattr(exc, 'code', type(exc).__name__)}"
    if error:
        if call is not None:
            call = {**call, "validation": f"rejected:{error}"}
        return _result(
            deterministic_claims(index),
            FALLBACK_TEMPLATE,
            "deterministic_fallback",
            packet_sha,
            error,
            call,
            request.prompt_id,
        )
    return _result(
        claims,
        LLM_TEMPLATE,
        "llm_grounded_claims",
        packet_sha,
        None,
        {**call, "validation": "accepted"},
        request.prompt_id,
    )
