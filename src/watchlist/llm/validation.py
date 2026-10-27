"""Strict validators that admit only grounded, schema-exact model output.

Validators are deliberately conservative: a single unsupported field or claim
rejects the whole output and the caller falls back to deterministic code.
Grounding checks are exact-match checks on whole-word input spans or frozen
packet values. The model never contributes prose: explanation claims carry
no free-text field, and every sentence a reader sees is rendered by code.

Command (replay mode only uses recorded receipts; live mode is refused unless
``config/llm.json`` approvals are complete and ``--allow-live`` is passed)::

    python -m watchlist.llm.validation evaluate --config config/llm.json \\
        --cases CASES.json --receipts RECEIPTS.jsonl --output NEW_DIR
"""

import argparse
import json
import math
from pathlib import Path
import re
import sys
import unicodedata

from watchlist.llm.schemas import (
    CLAIM_TYPES,
    ENTITY_TYPES,
    PARSE_FIELDS,
    GroundedField,
    ValidationFailure,
)
from watchlist.normalize import normalize_name

MAX_FIELDS = 20
MAX_CLAIMS = 200
MIN_NAME_LETTERS = 2
MAX_NAME_TOKENS = 12

# ZWNJ/ZWJ (U+200C/U+200D) are legitimate joiners in several scripts. Every
# other format character (bidi controls, invisible operators, soft hyphen, tag
# characters) is refused, as are invisible marks and fillers that are not Cf.
_ALLOWED_FORMAT = frozenset({0x200C, 0x200D})
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),  # combining grapheme joiner
    (0x115F, 0x1160),  # Hangul choseong/jungseong fillers
    (0x17B4, 0x17B5),  # Khmer inherent vowels (invisible)
    (0x180B, 0x180F),  # Mongolian variation selectors / vowel separator
    (0x2800, 0x2800),  # Braille blank
    (0x3164, 0x3164),  # Hangul filler
    (0xFE00, 0xFE0F),  # variation selectors
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0xE0000, 0xE007F),  # tag characters
    (0xE0100, 0xE01EF),  # variation selectors supplement
)
# Characters that structure prompts or data, never part of a name to search.
_NAME_STRUCTURE = frozenset(';:<>{}[]|=@#\\"`\n\r\t')

ENTITY_TYPE_KEYWORDS = {
    "person": ("person", "individual", "man", "woman", "mr", "mrs", "ms", "dr"),
    "organisation": (
        "organisation",
        "organization",
        "company",
        "firm",
        "business",
        "workshop",
        "ltd",
        "llc",
        "inc",
        "corporation",
        "corp",
        "bank",
        "foundation",
        "group",
    ),
}
_DATE = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?|\d{1,2}[./-]\d{1,2}[./-]\d{4}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9./-]{1,63}")
_COUNTRY = re.compile(r"[^\W\d_]+(?:[ .\'-][^\W\d_]+)*")

CLAIM_SPECS: dict[str, tuple[tuple[str, ...], dict[str, tuple[type, ...]]]] = {
    "candidate_count": (("packet",), {"candidate_count": (int,)}),
    "candidate_rank": (("candidate",), {"rank": (int,)}),
    "similarity_score": (
        ("candidate",),
        {"similarity_score": (int, float), "score_kind": (str,)},
    ),
    "matched_alias": (
        ("candidate", "evidence"),
        {"matched_alias_id": (str,), "matched_alias": (str,)},
    ),
    "alias_source": (("evidence",), {"source": (str,), "synthetic": (bool,)}),
}
assert set(CLAIM_SPECS) == set(CLAIM_TYPES)


def _fail(code: str, message: str) -> ValidationFailure:
    return ValidationFailure(code, message)


def is_hidden_character(ch: str) -> bool:
    """True for code points that can hide, reorder or smuggle text."""
    cp = ord(ch)
    category = unicodedata.category(ch)
    if category in ("Cc", "Cs", "Co", "Cn"):
        return True
    if category == "Cf" and cp not in _ALLOWED_FORMAT:
        return True
    return any(low <= cp <= high for low, high in _INVISIBLE_RANGES)


def disallowed_controls(text: str) -> list[str]:
    """Return code points (as U+XXXX) that could hide or reorder text."""
    return [f"U+{ord(ch):04X}" for ch in text if is_hidden_character(ch)]


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-finite JSON constant {name}")


def strict_json(text: object, max_chars: int) -> dict:
    """Decode one bounded JSON object, rejecting duplicates, NaN and trailing data.

    A single surrounding Markdown code fence is tolerated because it carries no
    content; anything else outside the object is rejected.
    """
    if not isinstance(text, str):
        raise _fail("wrong_type", "model output must be text")
    if len(text) > max_chars:
        raise _fail("overlong_output", f"model output exceeds {max_chars} characters")
    body = text.strip()
    fence = re.fullmatch(r"```(?:json)?\n(.*)\n```", body, re.DOTALL)
    if fence:
        body = fence.group(1)
    try:
        value = json.loads(
            body,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError) as exc:
        raise _fail("invalid_json", type(exc).__name__) from None
    if not isinstance(value, dict):
        raise _fail("wrong_schema", "top-level JSON must be an object")
    return value


def _exact_keys(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise _fail(
            "wrong_schema", f"{label} must have exactly the keys {sorted(keys)}"
        )
    return value


def _word_char(ch: str) -> bool:
    """Letters, digits, combining marks and joiners continue a word in any script."""
    return (
        ch.isalnum()
        or ch == "_"
        or unicodedata.category(ch).startswith("M")
        or ord(ch) in _ALLOWED_FORMAT
    )


def aligned_spans(source: str, quote: str) -> list[int]:
    """Start offsets where ``quote`` occurs as whole words (not inside a longer word).

    A span is aligned when it neither starts nor ends with whitespace, and the
    characters just outside it are not word characters. All occurrences are
    checked, so a whole-word occurrence later in the text is still found.
    """
    if not quote or quote != quote.strip():
        return []
    starts, start = [], source.find(quote)
    while start >= 0:
        end = start + len(quote)
        before_ok = start == 0 or not (
            _word_char(source[start - 1]) and _word_char(quote[0])
        )
        after_ok = end == len(source) or not (
            _word_char(source[end]) and _word_char(quote[-1])
        )
        if before_ok and after_ok:
            starts.append(start)
        start = source.find(quote, start + 1)
    return starts


def _check_name(value: str) -> None:
    """A searchable name: valid, short, with letters and no prompt/data structure."""
    try:
        normalize_name(value)
    except ValueError as exc:
        raise _fail("invalid_name", str(exc)) from None
    if any(ch in _NAME_STRUCTURE for ch in value):
        raise _fail("invalid_name", "name contains structural punctuation")
    if sum(ch.isalpha() for ch in value) < MIN_NAME_LETTERS:
        raise _fail("invalid_name", f"name needs at least {MIN_NAME_LETTERS} letters")
    if len(value.split()) > MAX_NAME_TOKENS:
        raise _fail("invalid_name", f"name has more than {MAX_NAME_TOKENS} words")


def _check_field(kind: str, value: str, quote: str, transform: str) -> None:
    """Field-specific grounding: values are copies of the quote, never derived guesses."""
    if kind == "entity_type":
        if transform != "entity_type_keyword" or value not in ENTITY_TYPES:
            raise _fail(
                "ungrounded_field", "entity_type needs a recognised keyword transform"
            )
        if quote.casefold().removesuffix(".") not in ENTITY_TYPE_KEYWORDS[value]:
            raise _fail(
                "ungrounded_field", "entity_type keyword does not state that type"
            )
        return
    if transform != "exact" or value != quote:
        raise _fail(
            "ungrounded_field", f"{kind} value must equal its quoted input span"
        )
    if kind in ("name", "alias"):
        _check_name(value)
    elif kind == "date" and not _DATE.fullmatch(value):
        raise _fail(
            "ungrounded_field", "date must be copied in a recognised numeric form"
        )
    elif kind == "identifier" and (
        not _IDENTIFIER.fullmatch(value) or not any(c.isdigit() for c in value)
    ):
        raise _fail(
            "ungrounded_field",
            "identifier must be a copied alphanumeric code with a digit",
        )
    elif kind == "country" and (len(value) > 60 or not _COUNTRY.fullmatch(value)):
        raise _fail("ungrounded_field", "country must be copied letters")


def validate_parse(
    text: object, source_text: str, max_chars: int
) -> tuple[list[GroundedField], list[str]]:
    """Return grounded fields and recorded ambiguities, or raise ValidationFailure.

    Every field must quote a whole-word span of the untrusted input. Repeated
    spans are kept but recorded as ambiguous. Exactly one name is required.
    """
    data = _exact_keys(strict_json(text, max_chars), {"fields"}, "parse output")
    fields = data["fields"]
    if not isinstance(fields, list) or not fields or len(fields) > MAX_FIELDS:
        raise _fail("wrong_schema", f"fields must be a list of 1..{MAX_FIELDS} objects")
    grounded: list[GroundedField] = []
    ambiguities: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in fields:
        item = _exact_keys(item, {"field", "quote", "value", "transform"}, "field")
        kind, quote, value, transform = (
            item["field"],
            item["quote"],
            item["value"],
            item["transform"],
        )
        if kind not in PARSE_FIELDS or not all(
            isinstance(v, str) for v in (quote, value, transform)
        ):
            raise _fail("wrong_schema", "field kind or value types are invalid")
        if not quote.strip() or len(quote) > 200 or len(value) > 200:
            raise _fail(
                "wrong_schema",
                "quote/value must be nonblank and at most 200 characters",
            )
        if disallowed_controls(quote) or disallowed_controls(value):
            raise _fail(
                "unicode_control",
                "bidi, invisible or control characters in an extracted field",
            )
        if quote not in source_text:
            raise _fail(
                "ungrounded_field",
                f"{kind} quote is not an exact substring of the input",
            )
        starts = aligned_spans(source_text, quote)
        if not starts:
            raise _fail(
                "unaligned_span", f"{kind} quote is not a whole-word span of the input"
            )
        _check_field(kind, value, quote, transform)
        if (kind, value) in seen:
            continue
        seen.add((kind, value))
        if len(starts) > 1:
            ambiguities.append(
                f"{kind} quote occurs {len(starts)} times; first occurrence recorded"
            )
        grounded.append(
            GroundedField(
                kind, value, quote, starts[0], starts[0] + len(quote), transform
            )
        )
    names = [g for g in grounded if g.field == "name"]
    if len(names) != 1:
        raise _fail("wrong_schema", "exactly one name field is required")
    if len({g.value for g in grounded if g.field == "entity_type"}) > 1:
        ambiguities.append("conflicting entity_type keywords")
    return grounded, ambiguities


def winning_evidence(index: dict[str, dict], candidate: dict) -> str:
    """The single evidence ID holding the candidate's matched alias (checked in build_evidence)."""
    return next(
        r
        for r in candidate["evidence_ids"]
        if index[r]["alias_id"] == candidate["matched_alias_id"]
    )


def required_claims(index: dict[str, dict]) -> set[tuple[str, tuple[str, ...]]]:
    """Claims a model output must contain so no candidate or core fact is silently dropped."""
    required = {("candidate_count", ("packet",))}
    for item in index.values():
        if item["kind"] == "candidate":
            cid = item["entity_id"]
            required |= {
                ("candidate_rank", (cid,)),
                ("similarity_score", (cid,)),
                ("matched_alias", (cid, winning_evidence(index, item))),
            }
    return required


def validate_claims(text: object, index: dict[str, dict], max_chars: int) -> list[dict]:
    """Return normalized claims whose citations and values exactly match the packet.

    ``index`` maps every citable ID to a dict with a ``kind`` (packet,
    candidate or evidence) and its frozen fields. Claims have no free-text
    field. Any forged citation, wrong value, unknown key, duplicate or missing
    core claim (count, and rank/score/matched alias of every candidate)
    rejects the entire output.
    """
    data = _exact_keys(strict_json(text, max_chars), {"claims"}, "explanation output")
    claims = data["claims"]
    if not isinstance(claims, list) or not claims or len(claims) > MAX_CLAIMS:
        raise _fail("wrong_schema", f"claims must be a list of 1..{MAX_CLAIMS} objects")
    accepted: list[dict] = []
    seen: set[tuple] = set()
    for claim in claims:
        claim = _exact_keys(claim, {"claim_type", "citations", "values"}, "claim")
        kind = claim["claim_type"]
        if kind not in CLAIM_SPECS:
            raise _fail("wrong_schema", "unknown claim_type")
        kinds, value_types = CLAIM_SPECS[kind]
        citations = claim["citations"]
        if (
            not isinstance(citations, list)
            or len(citations) != len(kinds)
            or not all(isinstance(c, str) for c in citations)
        ):
            raise _fail("wrong_schema", f"{kind} needs citations of kinds {kinds}")
        items = []
        for cited, wanted in zip(citations, kinds):
            item = index.get(cited)
            if item is None:
                raise _fail(
                    "forged_citation", "citation is not an ID in the frozen packet"
                )
            if item["kind"] != wanted:
                raise _fail(
                    "forged_citation",
                    f"citation kind {item['kind']} where {wanted} is required",
                )
            items.append(item)
        values = _exact_keys(claim["values"], set(value_types), f"{kind} values")
        for key, types in value_types.items():
            value = values[key]
            if type(value) not in types or (
                type(value) is float and not math.isfinite(value)
            ):
                raise _fail("wrong_schema", f"{key} has the wrong type")
        _check_claim_values(kind, values, items)
        key = (kind, tuple(citations))
        if key in seen:
            raise _fail("wrong_schema", "duplicate claim")
        seen.add(key)
        accepted.append(
            {"claim_type": kind, "citations": list(citations), "values": dict(values)}
        )
    missing = required_claims(index) - seen
    if missing:
        raise _fail(
            "incomplete_coverage", f"{len(missing)} required packet claims are missing"
        )
    return accepted


def _check_claim_values(kind: str, values: dict, items: list[dict]) -> None:
    """Exact equality against frozen packet fields; nothing is rounded or inferred."""
    first = items[0]
    if kind == "matched_alias":
        candidate, evidence = items
        ok = (
            candidate["matched_alias_id"]
            == values["matched_alias_id"]
            == evidence["alias_id"]
            and candidate["matched_alias"]
            == values["matched_alias"]
            == evidence["original_text"]
            and evidence["entity_id"] == candidate["entity_id"]
        )
    else:
        ok = all(
            type(first.get(key)) is type(value) and first.get(key) == value
            for key, value in values.items()
        )
    if not ok:
        raise _fail(
            "value_mismatch", f"{kind} values do not exactly match the cited evidence"
        )


# --- Step 23 replay/live evaluation CLI ---------------------------------------------------------


def load_cases(path: Path) -> list[dict]:
    """Read ``[{case_id, task: parse|explain, input}]``; inputs are untrusted fictional data."""
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases file must contain a nonempty list")
    ids = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"case_id", "task", "input"}:
            raise ValueError("each case needs exactly case_id, task and input")
        if (
            not isinstance(case["case_id"], str)
            or case["case_id"] in ids
            or case["task"] not in ("parse", "explain")
        ):
            raise ValueError("invalid or duplicate case")
        ids.add(case["case_id"])
    return cases


def evaluate_cases(cases: list[dict], client, config) -> list[dict]:
    """Run every case through the production parse/explain path; record outcome, never impute."""
    from watchlist.llm.explanations import (
        explain_packet,
    )  # Lazy: those modules import this one.
    from watchlist.llm.parsing import parse_request

    records = []
    for case in cases:
        record = {"case_id": case["case_id"], "task": case["task"]}
        try:
            if case["task"] == "parse":
                result = parse_request(case["input"], client, config)
                record.update(
                    method=result.method,
                    fallback_reason=result.fallback_reason,
                    requires_confirmation=list(result.requires_confirmation),
                )
            else:
                result = explain_packet(case["input"], client, config)
                record.update(
                    method=result.method,
                    fallback_reason=result.fallback_reason,
                    packet_sha256=result.packet_sha256,
                )
            record["model_call"] = result.model_call
        except ValueError as exc:
            record.update(
                method="input_error",
                fallback_reason=f"input_error:{type(exc).__name__}",
                model_call=None,
            )
        records.append(record)
    return records


def summarize_evaluation(records: list[dict]) -> dict:
    """Counts with denominators; tokens/cost/latency come only from recorded calls."""
    calls = [r["model_call"] for r in records if r["model_call"]]

    def total(key: str) -> float | None:
        values = [c.get(key) for c in calls]
        return (
            None
            if not values or any(v is None for v in values)
            else round(sum(values), 8)
        )

    methods: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for r in records:
        methods[r["method"]] = methods.get(r["method"], 0) + 1
        if r["fallback_reason"]:
            reasons[r["fallback_reason"]] = reasons.get(r["fallback_reason"], 0) + 1
    accepted = sum(
        r["method"] in ("llm_grounded", "llm_grounded_claims") for r in records
    )
    return {
        "cases": len(records),
        "model_calls_returned": len(calls),
        "accepted_model_outputs": {"count": accepted, "denominator": len(records)},
        "methods": dict(sorted(methods.items())),
        "fallback_reasons": dict(sorted(reasons.items())),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "cost_usd_estimate": total("cost_usd"),
        "latency_ms_total": total("latency_ms"),
        "evidence_kinds": sorted({c["evidence_kind"] for c in calls}),
    }


def evaluation_status(summary: dict) -> str:
    if summary["model_calls_returned"] == 0:
        return "BLOCKED_no_llm_outputs"
    if "fake_fixture" in summary["evidence_kinds"]:
        return "fixture_only_not_an_llm_result"
    return "measured_on_provider_receipts"


def main(argv: list[str] | None = None) -> int:
    from watchlist.llm import RESEARCH_LIMITATION
    from watchlist.llm.client import (
        AnthropicClient,
        RecordingClient,
        ReplayClient,
        load_config,
    )
    from watchlist.llm.schemas import LLMError, canonical, sha256_text

    parser = argparse.ArgumentParser(prog="python -m watchlist.llm.validation")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("evaluate")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--cases", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument(
        "--receipts",
        type=Path,
        help="replay mode: recorded receipts (JSON list or JSONL)",
    )
    p.add_argument(
        "--allow-live",
        action="store_true",
        help="live mode: requires complete config approvals",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        cases = load_cases(args.cases)
        if args.output.exists():
            raise ValueError("output directory must not already exist")
        if (args.receipts is None) == (not args.allow_live):
            raise ValueError(
                "pass exactly one of --receipts (replay) or --allow-live (live)"
            )
        if args.receipts is not None:
            client, mode = ReplayClient.from_file(args.receipts, config.model), "replay"
            records = evaluate_cases(cases, client, config)
            args.output.mkdir(parents=True, exist_ok=False)
        else:
            live = AnthropicClient(
                config, allow_live=True
            )  # Refuses before any output is created.
            args.output.mkdir(parents=True, exist_ok=False)
            client, mode = RecordingClient(live, args.output / "receipts.jsonl"), "live"
            records = evaluate_cases(cases, client, config)
        summary = summarize_evaluation(records)
        report = {
            "schema_version": "llm-validation-report-v1",
            "mode": mode,
            "status": evaluation_status(summary),
            "config_sha256": config.sha256,
            "model": config.model,
            "cases_sha256": sha256_text(args.cases.read_text(encoding="utf-8")),
            "summary": summary,
            "limitations": RESEARCH_LIMITATION,
        }
        with (args.output / "report.json").open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        with (args.output / "cases.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            for record in records:
                handle.write(canonical(record) + "\n")
    except (LLMError, ValueError, OSError) as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(args.output),
                "summary": summary,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
