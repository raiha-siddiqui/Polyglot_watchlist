"""Typed parsing and validation of a structured screening request (Step 10).

A request names one fictional/reference-style subject plus optional aliases,
entity type, country codes, partial dates and identifiers. Parsing never
guesses: malformed values become explicit :class:`FieldError` items and no
partially repaired query is returned. Persons are outside the default scope
(see docs/design.md and the Step 10 plan), so ``entity_type: person`` is
rejected rather than silently accepted.

Validated attributes are supporting evidence for HUMAN review. They never
establish identity, wrongdoing, sanctions status or clearance.

The messy-text extraction contract (:class:`ExtractedField`) is defined here
for Step 23: extracted consequential values must be confirmed before retrieval.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import datetime as _dt
import re
import unicodedata

from watchlist.normalize import MAX_NAME_LENGTH, normalize_name
from watchlist.preprocessing import HYPHEN_VARIANTS

STRUCTURED_INPUT_VERSION = "structured-input-v1"
ENTITY_TYPES = frozenset({"organization", "vessel", "unknown"})
OUT_OF_SCOPE_ENTITY_TYPES = frozenset({"person"})
_ENTITY_TYPE_SPELLINGS = {"organisation": "organization"}
COUNTRY_ROLES = frozenset(
    {"unspecified", "jurisdiction", "registration", "flag", "address"}
)
DATE_KINDS = {
    "organization": frozenset({"incorporation", "registration", "dissolution"}),
    "vessel": frozenset({"build", "registration"}),
    "unknown": frozenset({"incorporation", "registration", "dissolution", "build"}),
}
IDENTIFIER_SCHEMES = frozenset({"imo", "registration"})
MAX_ALIASES = 20
MAX_LIST_ITEMS = 20
MAX_IDENTIFIER_LENGTH = 64
_FIELDS = frozenset(
    {"name", "aliases", "entity_type", "countries", "dates", "identifiers"}
)

# Officially assigned ISO 3166-1 alpha-2 codes (249). User-assigned codes such
# as XK and exceptionally reserved codes such as UK/EU are not accepted.
ISO_3166_ALPHA2 = frozenset(
    """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ
BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY
HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP
KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY
MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ
NA NC NE NF NG NI NL NO NP NR NU NZ OM
PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW
SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ
TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ
UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split()
)

_PARTIAL_DATE = re.compile(
    r"(?P<year>[0-9]{4})(?:-(?P<month>[0-9]{2})(?:-(?P<day>[0-9]{2}))?)?"
)
_IMO = re.compile(r"(?:IMO[ ]?)?(?P<digits>[0-9]{7})")
# Every hyphen/dash variant is a separator too, so ``AB–12`` keys like ``AB-12``.
_ID_SEPARATORS = str.maketrans("", "", " -./" + HYPHEN_VARIANTS)
# Embedding, override and isolate controls can make displayed text differ from
# its logical order. ZWJ/ZWNJ stay allowed: Arabic, Persian and Indic need them.
BIDI_CONTROLS = frozenset("؜‎‏‪‫‬‭‮⁦⁧⁨⁩")


@dataclass(frozen=True)
class FieldError:
    """One explicit validation failure; ``field`` is a JSON-pointer-like path."""

    field: str
    code: str
    message: str


class StructuredInputError(ValueError):
    """All validation errors for one request, in deterministic order."""

    def __init__(self, errors: Sequence[FieldError]) -> None:
        self.errors = tuple(sorted(errors, key=lambda e: (e.field, e.code, e.message)))
        super().__init__("; ".join(f"{e.field}: {e.code}" for e in self.errors))


@dataclass(frozen=True)
class CountryCode:
    """A validated alpha-2 code and the role it was supplied for."""

    code: str
    role: str


@dataclass(frozen=True, order=True)
class PartialDate:
    """YYYY, YYYY-MM or YYYY-MM-DD; missing parts stay None, never invented."""

    year: int
    month: int | None = None
    day: int | None = None

    @property
    def precision(self) -> str:
        return (
            "day"
            if self.day is not None
            else "month"
            if self.month is not None
            else "year"
        )

    def isoformat(self) -> str:
        parts = [f"{self.year:04d}"]
        if self.month is not None:
            parts.append(f"{self.month:02d}")
        if self.day is not None:
            parts.append(f"{self.day:02d}")
        return "-".join(parts)


@dataclass(frozen=True)
class DatedFact:
    """A partial date with its kind (incorporation, build, ...)."""

    kind: str
    date: PartialDate


@dataclass(frozen=True)
class Identifier:
    """A validated identifier: original text plus a separator-free comparison key.

    For ``imo`` the key is the seven checksum-valid digits. For ``registration``
    the key is NFKC+casefold without spaces, dots, slashes or any hyphen/dash
    variant (U+2010-U+2015, U+2212, ...); the
    optional ``issuer_country`` scopes which registry issued it.
    """

    scheme: str
    value: str
    key: str
    issuer_country: str | None = None


@dataclass(frozen=True)
class ScreeningQuery:
    """A fully validated structured request; aliases keep their original text."""

    name: str
    aliases: tuple[str, ...]
    entity_type: str
    countries: tuple[CountryCode, ...]
    dates: tuple[DatedFact, ...]
    identifiers: tuple[Identifier, ...]
    version: str = STRUCTURED_INPUT_VERSION


# --- field validators (shared with reconciliation) ---------------------------


def _name_errors(value: object, field: str) -> list[FieldError]:
    if not isinstance(value, str):
        return [FieldError(field, "not_text", "must be a string")]
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return [FieldError(field, "invalid_unicode", "must be valid Unicode text")]
    if any(unicodedata.category(c) == "Cc" and not c.isspace() for c in value):
        return [
            FieldError(
                field, "control_character", "must not contain control characters"
            )
        ]
    if any(c in BIDI_CONTROLS for c in value):
        return [
            FieldError(
                field,
                "bidi_control",
                "must not contain bidirectional embedding/override/isolate controls",
            )
        ]
    if len(value) > MAX_NAME_LENGTH:
        return [
            FieldError(
                field, "too_long", f"must contain at most {MAX_NAME_LENGTH} characters"
            )
        ]
    try:
        normalize_name(value)
    except ValueError as exc:
        code = "blank" if "blank" in str(exc) else "too_long"
        return [FieldError(field, code, str(exc))]
    if not any(unicodedata.category(c)[0] in "LN" for c in value):
        # Catches visually blank names such as U+200B or U+2060 only.
        return [
            FieldError(field, "no_letter_or_digit", "must contain a letter or digit")
        ]
    return []


def entity_type_or_error(
    value: object, field: str = "entity_type"
) -> tuple[str | None, list[FieldError]]:
    """Canonical entity type, or explicit errors (persons are out of scope)."""
    if not isinstance(value, str):
        return None, [FieldError(field, "not_text", "must be a string")]
    canonical = _ENTITY_TYPE_SPELLINGS.get(value, value)
    if canonical in OUT_OF_SCOPE_ENTITY_TYPES:
        return None, [
            FieldError(
                field,
                "out_of_scope_entity_type",
                "person screening is outside the default research scope",
            )
        ]
    if canonical not in ENTITY_TYPES:
        return None, [
            FieldError(
                field, "unknown_entity_type", "must be organization, vessel or unknown"
            )
        ]
    return canonical, []


def country_or_error(value: object, field: str) -> tuple[str | None, list[FieldError]]:
    """Exact uppercase ISO 3166-1 alpha-2 code; lower case or names are rejected."""
    if not isinstance(value, str):
        return None, [FieldError(field, "not_text", "country code must be a string")]
    if value not in ISO_3166_ALPHA2:
        hint = " (use uppercase)" if value.upper() in ISO_3166_ALPHA2 else ""
        return None, [
            FieldError(
                field,
                "invalid_country_code",
                f"not an officially assigned ISO 3166-1 alpha-2 code{hint}",
            )
        ]
    return value, []


def parse_partial_date(
    value: object, field: str = "date"
) -> tuple[PartialDate | None, list[FieldError]]:
    """Parse YYYY, YYYY-MM or YYYY-MM-DD; impossible calendar dates are errors."""
    if not isinstance(value, str):
        return None, [FieldError(field, "not_text", "date must be a string")]
    match = _PARTIAL_DATE.fullmatch(value)
    if match is None:
        return None, [
            FieldError(
                field,
                "invalid_date_format",
                "must be YYYY, YYYY-MM or YYYY-MM-DD with ASCII digits",
            )
        ]
    year = int(match["year"])
    month = int(match["month"]) if match["month"] else None
    day = int(match["day"]) if match["day"] else None
    if year < 1000:
        return None, [FieldError(field, "invalid_date", "year must be 1000 or later")]
    if month is not None and not 1 <= month <= 12:
        return None, [FieldError(field, "invalid_date", "month must be 01-12")]
    if day is not None:
        try:
            _dt.date(year, month or 1, day)
        except ValueError:
            return None, [
                FieldError(field, "invalid_date", "day does not exist in that month")
            ]
    return PartialDate(year, month, day), []


def imo_checksum_valid(digits: str) -> bool:
    """IMO ship number rule: sum(d_i * (7..2)) for six digits ends in the 7th."""
    if len(digits) != 7 or not digits.isascii() or not digits.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(digits[:6], range(7, 1, -1)))
    return total % 10 == int(digits[6])


def identifier_or_error(
    value: object, field: str
) -> tuple[Identifier | None, list[FieldError]]:
    """Validate ``{"scheme", "value"[, "issuer_country"]}`` identifier objects."""
    if not isinstance(value, Mapping):
        return None, [FieldError(field, "not_object", "identifier must be an object")]
    extra = sorted(set(value) - {"scheme", "value", "issuer_country"})
    if extra:
        return None, [
            FieldError(field, "unknown_field", f"unknown identifier fields: {extra}")
        ]
    scheme, raw = value.get("scheme"), value.get("value")
    if not isinstance(scheme, str) or scheme not in IDENTIFIER_SCHEMES:
        return None, [
            FieldError(
                f"{field}/scheme",
                "unknown_identifier_scheme",
                "scheme must be imo or registration",
            )
        ]
    if not isinstance(raw, str) or not raw.strip():
        return None, [
            FieldError(
                f"{field}/value", "blank", "identifier value must be nonblank text"
            )
        ]
    if len(raw) > MAX_IDENTIFIER_LENGTH or any(
        unicodedata.category(c).startswith("C") for c in raw
    ):
        return None, [
            FieldError(
                f"{field}/value",
                "invalid_identifier",
                f"at most {MAX_IDENTIFIER_LENGTH} printable characters",
            )
        ]
    issuer = value.get("issuer_country")
    if scheme == "imo":
        if issuer is not None:
            return None, [
                FieldError(
                    f"{field}/issuer_country",
                    "not_applicable",
                    "IMO numbers are not country-issued",
                )
            ]
        match = _IMO.fullmatch(raw.strip())
        if match is None:
            return None, [
                FieldError(
                    f"{field}/value",
                    "invalid_imo_format",
                    "IMO number must be 7 ASCII digits, optionally prefixed 'IMO'",
                )
            ]
        if match["digits"] == "0000000":
            return None, [
                FieldError(
                    f"{field}/value",
                    "invalid_imo_number",
                    "0000000 is a placeholder, not an IMO number",
                )
            ]
        if not imo_checksum_valid(match["digits"]):
            return None, [
                FieldError(
                    f"{field}/value",
                    "invalid_imo_checksum",
                    "IMO check digit does not match",
                )
            ]
        return Identifier("imo", raw, match["digits"]), []
    issuer_code: str | None = None
    if issuer is not None:
        issuer_code, errors = country_or_error(issuer, f"{field}/issuer_country")
        if errors:
            return None, errors
    key = unicodedata.normalize("NFKC", raw).casefold().translate(_ID_SEPARATORS)
    if not key:
        return None, [
            FieldError(
                f"{field}/value",
                "invalid_identifier",
                "identifier has no characters besides separators",
            )
        ]
    return Identifier("registration", raw, key, issuer_code), []


# --- request parsing --------------------------------------------------------


def _list(
    raw: Mapping[str, object], key: str, limit: int, errors: list[FieldError]
) -> list[object]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        errors.append(FieldError(key, "not_list", "must be a list"))
        return []
    if len(value) > limit:
        errors.append(FieldError(key, "too_many", f"at most {limit} items"))
        return []
    return value


def parse_screening_request(raw: object) -> ScreeningQuery:
    """Validate a JSON-like mapping; raise StructuredInputError with ALL errors.

    Countries may be ``"DE"`` (role ``unspecified``) or ``{"code", "role"}``.
    Dates are ``{"kind", "value"}`` with kinds allowed for the entity type.
    Exact duplicates are errors, not silently merged, so the caller sees them.
    A date kind takes one value: differing values of the same kind (including
    ``2001`` with ``2001-05``) are ``conflicting_values`` errors.
    """
    if not isinstance(raw, Mapping):
        raise StructuredInputError(
            [FieldError("", "not_object", "request must be an object")]
        )
    errors: list[FieldError] = []
    for key in sorted(set(raw) - _FIELDS, key=str):
        errors.append(FieldError(str(key), "unknown_field", "unknown request field"))
    if "name" not in raw:
        errors.append(FieldError("name", "required", "name is required"))
    else:
        errors.extend(_name_errors(raw["name"], "name"))

    aliases: list[str] = []
    seen_aliases: set[str] = set()
    if isinstance(raw.get("name"), str) and not _name_errors(raw["name"], "name"):
        seen_aliases.add(normalize_name(raw["name"]))
    for index, alias in enumerate(_list(raw, "aliases", MAX_ALIASES, errors)):
        alias_errors = _name_errors(alias, f"aliases/{index}")
        if not alias_errors:
            assert isinstance(alias, str)
            if normalize_name(alias) in seen_aliases:
                alias_errors = [
                    FieldError(
                        f"aliases/{index}",
                        "duplicate",
                        "alias duplicates the name or another alias",
                    )
                ]
            else:
                seen_aliases.add(normalize_name(alias))
                aliases.append(alias)
        errors.extend(alias_errors)

    entity_type, type_errors = entity_type_or_error(raw.get("entity_type", "unknown"))
    errors.extend(type_errors)

    countries: list[CountryCode] = []
    for index, item in enumerate(_list(raw, "countries", MAX_LIST_ITEMS, errors)):
        field = f"countries/{index}"
        code_value: object = item
        role: object = "unspecified"
        if isinstance(item, Mapping):
            if set(item) - {"code", "role"}:
                errors.append(
                    FieldError(
                        field, "unknown_field", "country objects take code and role"
                    )
                )
                continue
            code_value, role = item.get("code"), item.get("role", "unspecified")
        if not isinstance(role, str) or role not in COUNTRY_ROLES:
            errors.append(
                FieldError(
                    f"{field}/role",
                    "unknown_country_role",
                    f"role must be one of {sorted(COUNTRY_ROLES)}",
                )
            )
            continue
        code, code_errors = country_or_error(code_value, field)
        errors.extend(code_errors)
        if code is not None:
            country = CountryCode(code, str(role))
            if country in countries:
                errors.append(FieldError(field, "duplicate", "duplicate country entry"))
            else:
                countries.append(country)

    dates: list[DatedFact] = []
    allowed_kinds = DATE_KINDS.get(entity_type or "unknown", frozenset())
    for index, item in enumerate(_list(raw, "dates", MAX_LIST_ITEMS, errors)):
        field = f"dates/{index}"
        if not isinstance(item, Mapping) or set(item) != {"kind", "value"}:
            errors.append(
                FieldError(field, "not_object", "date needs exactly kind and value")
            )
            continue
        if not isinstance(item["kind"], str) or item["kind"] not in allowed_kinds:
            errors.append(
                FieldError(
                    f"{field}/kind",
                    "unsupported_date_kind",
                    f"allowed kinds: {sorted(allowed_kinds)}",
                )
            )
            continue
        date, date_errors = parse_partial_date(item["value"], f"{field}/value")
        errors.extend(date_errors)
        if date is not None:
            fact = DatedFact(str(item["kind"]), date)
            if fact in dates:
                errors.append(FieldError(field, "duplicate", "duplicate date entry"))
            elif any(d.kind == fact.kind for d in dates):
                errors.append(
                    FieldError(
                        field,
                        "conflicting_values",
                        f"another {fact.kind} date was already given",
                    )
                )
            else:
                dates.append(fact)

    identifiers: list[Identifier] = []
    for index, item in enumerate(_list(raw, "identifiers", MAX_LIST_ITEMS, errors)):
        identifier, id_errors = identifier_or_error(item, f"identifiers/{index}")
        errors.extend(id_errors)
        if identifier is not None:
            if identifier.scheme == "imo" and entity_type == "organization":
                errors.append(
                    FieldError(
                        f"identifiers/{index}",
                        "not_applicable",
                        "IMO ship numbers apply to vessels, not organizations",
                    )
                )
            elif any(
                (i.scheme, i.key, i.issuer_country)
                == (identifier.scheme, identifier.key, identifier.issuer_country)
                for i in identifiers
            ):
                errors.append(
                    FieldError(
                        f"identifiers/{index}", "duplicate", "duplicate identifier"
                    )
                )
            else:
                identifiers.append(identifier)

    if errors:
        raise StructuredInputError(errors)
    assert isinstance(raw["name"], str) and entity_type is not None
    return ScreeningQuery(
        name=raw["name"],
        aliases=tuple(aliases),
        entity_type=entity_type,
        countries=tuple(countries),
        dates=tuple(dates),
        identifiers=tuple(identifiers),
    )


def validate_screening_request(
    raw: object,
) -> tuple[ScreeningQuery | None, tuple[FieldError, ...]]:
    """Non-raising form: ``(query, ())`` or ``(None, errors)``."""
    try:
        return parse_screening_request(raw), ()
    except StructuredInputError as exc:
        return None, exc.errors


# --- messy-text extraction contract (implementation is Step 23) -------------

# Every request field feeds retrieval or reconciliation (aliases are queried
# like the name), so all are consequential. Any other field name is an error.
CONSEQUENTIAL_FIELDS = _FIELDS
# Text fields whose value must be quoted verbatim from the source span.
VERBATIM_FIELDS = frozenset({"name", "aliases"})


@dataclass(frozen=True)
class ExtractedField:
    """One value proposed by a future extractor, with its source span.

    ``confirmed`` must be set by a human before the value may reach retrieval.
    The extractor output is untrusted data; it never sets confirmation itself.
    """

    field: str
    value: object
    source_text: str
    start: int
    end: int
    confirmed: bool = False


def unconfirmed_consequential(
    fields: Sequence[ExtractedField],
) -> tuple[FieldError, ...]:
    """Errors for every extracted value that may not reach retrieval yet.

    Unknown field names, malformed spans (non-int, bool or out of range),
    name/alias values that differ from their quoted span, and any value
    without human confirmation are all reported; nothing passes by default.
    """
    errors = []
    for item in fields:
        if not isinstance(item.field, str) or item.field not in CONSEQUENTIAL_FIELDS:
            errors.append(
                FieldError(
                    str(item.field), "unknown_field", "not a screening request field"
                )
            )
        elif (
            not isinstance(item.source_text, str)
            or type(item.start) is not int
            or type(item.end) is not int
            or not 0 <= item.start <= item.end <= len(item.source_text)
        ):
            errors.append(
                FieldError(item.field, "invalid_span", "span outside source text")
            )
        elif (
            item.field in VERBATIM_FIELDS
            and item.value != item.source_text[item.start : item.end]
        ):
            errors.append(
                FieldError(
                    item.field,
                    "value_not_in_span",
                    "extracted text differs from the source span",
                )
            )
        elif item.confirmed is not True:
            errors.append(
                FieldError(
                    item.field,
                    "unconfirmed_extraction",
                    "extracted value requires human confirmation before retrieval",
                )
            )
    return tuple(sorted(errors, key=lambda e: (e.field, e.code)))
