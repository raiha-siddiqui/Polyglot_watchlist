"""Free-text request parsing with exact-span grounding and a deterministic fallback.

The model is consulted only for messy text. Its output is admitted only when
every field quotes a whole-word span of the untrusted input. Any failure
produces a name-only deterministic parse with the reason recorded. Every
model-extracted field, the name included, is marked for human confirmation
and never feeds retrieval automatically: an instruction hidden in the request
could otherwise choose what is searched.
"""

from collections.abc import Collection
from dataclasses import replace

from watchlist.llm import prompts
from watchlist.llm.client import LLMClient, LLMConfig, call_model
from watchlist.llm.schemas import CONSEQUENTIAL_FIELDS, StructuredQuery
from watchlist.llm.validation import disallowed_controls, validate_parse
from watchlist.normalize import normalize_name

MAX_REQUEST_CHARS = 1000
_STRUCTURE_MARKERS = set(':;,()[]{}/@#|="')


def _check_envelope(text: object, limit: int) -> str:
    if not isinstance(text, str) or len(text) > limit:
        raise ValueError(f"request must be text of at most {limit} characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("request must contain valid Unicode text") from exc
    return text


def needs_model(text: str) -> bool:
    """True for messy text; a short plain name never needs a model call."""
    try:
        normalize_name(text)
    except ValueError:
        return True
    return (
        len(text.split()) > 6
        or any(ch.isdigit() for ch in text)
        or any(ch in _STRUCTURE_MARKERS for ch in text)
    )


def deterministic_parse(
    text: str, reason: str, method: str = "deterministic"
) -> StructuredQuery:
    """Name-only parse: the whole request (trimmed) is the name; nothing is extracted."""
    warnings = []
    controls = disallowed_controls(text)
    if controls:
        warnings.append(
            "input contains bidi/invisible/control characters: "
            + ", ".join(sorted(set(controls)))
        )
    name = text.strip()
    try:
        normalize_name(name)
    except ValueError as exc:
        name, reason = None, f"{reason}; deterministic_name_invalid: {exc}"
    return StructuredQuery(
        source_text=text,
        name=name,
        aliases=(),
        entity_type=None,
        country=None,
        dates=(),
        identifiers=(),
        method=method,
        fallback_reason=reason,
        warnings=tuple(warnings),
    )


def parse_request(
    text: object, client: LLMClient | None = None, config: LLMConfig | None = None
) -> StructuredQuery:
    """Parse a free-text search request into a grounded structured query.

    Raises ValueError only for an invalid transport envelope. Every model,
    schema or grounding failure returns a ``deterministic_fallback`` result.
    """
    limit = config.max_input_chars if config else MAX_REQUEST_CHARS
    text = _check_envelope(text, limit)
    if client is None:
        return deterministic_parse(text, "model_not_configured")
    if not needs_model(text):
        return deterministic_parse(text, "plain_name_no_model_call")
    request = prompts.parse_request(
        text, config.max_output_tokens["parse"] if config else 4096
    )
    if config and request.size_chars() > config.max_request_chars:
        return _with_call(
            deterministic_parse(text, "request_too_large", "deterministic_fallback"),
            None,
            request.prompt_id,
        )
    response, error = call_model(client, request)
    call = None if response is None else response.accounting()
    if error:
        result = deterministic_parse(text, error, "deterministic_fallback")
        return _with_call(result, call, request.prompt_id)
    max_chars = config.max_output_chars if config else 20000
    try:
        fields, ambiguities = validate_parse(response.text, text, max_chars)
    except Exception as exc:  # noqa: BLE001 - ValidationFailure or any validator bug must fall back.
        code = getattr(exc, "code", type(exc).__name__)
        result = deterministic_parse(
            text, f"validation:{code}", "deterministic_fallback"
        )
        return _with_call(
            result, {**call, "validation": f"rejected:{code}"}, request.prompt_id
        )

    def values(kind: str) -> tuple[str, ...]:
        return tuple(g.value for g in fields if g.field == kind)

    entity_types, countries = values("entity_type"), values("country")
    if len(countries) > 1:
        ambiguities.append("several countries quoted")
    present = {g.field for g in fields}
    confirm = tuple(f for f in CONSEQUENTIAL_FIELDS if f in present)
    return StructuredQuery(
        source_text=text,
        name=values("name")[0],
        aliases=values("alias"),
        entity_type=entity_types[0] if len(entity_types) == 1 else None,
        country=countries[0] if len(countries) == 1 else None,
        dates=values("date"),
        identifiers=values("identifier"),
        method="llm_grounded",
        fallback_reason=None,
        grounding=tuple(fields),
        ambiguities=tuple(ambiguities),
        requires_confirmation=confirm,
        warnings=(),
        model_call={**call, "validation": "accepted"},
        prompt_id=request.prompt_id,
    )


def _with_call(
    result: StructuredQuery, call: dict | None, prompt_id: str
) -> StructuredQuery:
    return replace(result, model_call=call, prompt_id=prompt_id)


def retrieval_name(
    query: StructuredQuery, confirmed: Collection[str] = ()
) -> str | None:
    """The name retrieval may use, or ``None`` until a human has confirmed it.

    A deterministic parse (the request as typed) is usable directly. A
    model-extracted name needs ``'name'`` in ``confirmed``, which a caller sets
    only after a person accepted it. A name with hidden/bidi characters is
    never returned; the query's warnings say why.
    """
    if query.name is None or disallowed_controls(query.name):
        return None
    if "name" in query.requires_confirmation and "name" not in confirmed:
        return None
    return query.name
