"""Deterministic attribute evidence between a query and one candidate record.

For each structured attribute the result is an evidence item with status
``agree``, ``conflict``, ``not_comparable`` or ``missing`` plus the values and
the candidate-side provenance that produced it:

* missing is not disagreement; it says which side lacks the attribute;
* conflict means the recorded values differ. It is shown, never resolved, and
  does not mean the records describe different entities;
* consistent values at different precision (``2001`` vs ``2001-05``) and
  identifiers from different issuers are ``not_comparable``, never guessed.

There is deliberately no identity inference, weighting, score fusion or
probability here, and name similarity is kept entirely separate. Items are
supporting evidence for a HUMAN reviewer of a POSSIBLE reference-record match.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from watchlist.structured_input import (
    COUNTRY_ROLES,
    DATE_KINDS,
    FieldError,
    Identifier,
    PartialDate,
    ScreeningQuery,
    StructuredInputError,
    country_or_error,
    entity_type_or_error,
    identifier_or_error,
    parse_partial_date,
)

RECONCILIATION_VERSION = "reconciliation-v1"
STATUSES = ("agree", "conflict", "not_comparable", "missing")
NOTICE = (
    "Attribute evidence for human review of a possible reference-record match. "
    "It does not establish identity, wrongdoing, sanctions status or clearance."
)


@dataclass(frozen=True, order=True)
class EvidenceSource:
    """Where a candidate value came from (source, record and field path)."""

    source_id: str
    record_id: str
    field: str


@dataclass(frozen=True)
class SourcedCountry:
    code: str
    role: str
    source: EvidenceSource


@dataclass(frozen=True)
class SourcedDate:
    kind: str
    date: PartialDate
    source: EvidenceSource


@dataclass(frozen=True)
class SourcedIdentifier:
    identifier: Identifier
    source: EvidenceSource


@dataclass(frozen=True)
class CandidateRecord:
    """Structured attributes of one reference record, each with provenance."""

    record_id: str
    entity_type: str
    countries: tuple[SourcedCountry, ...] = ()
    dates: tuple[SourcedDate, ...] = ()
    identifiers: tuple[SourcedIdentifier, ...] = ()


@dataclass(frozen=True)
class EvidenceItem:
    """One per-attribute comparison outcome; values are display strings."""

    attribute: str
    status: str
    details: tuple[str, ...]
    query_values: tuple[str, ...]
    candidate_values: tuple[str, ...]
    provenance: tuple[EvidenceSource, ...]


@dataclass(frozen=True)
class ReconciliationReport:
    """All evidence items for one (query, candidate) pair, deterministically ordered."""

    record_id: str
    items: tuple[EvidenceItem, ...]
    version: str = RECONCILIATION_VERSION
    notice: str = NOTICE

    def status_counts(self) -> dict[str, int]:
        """Counts per status for display only; never combined into a score."""
        return {
            status: sum(item.status == status for item in self.items)
            for status in STATUSES
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "version": self.version,
            "notice": self.notice,
            "status_counts": self.status_counts(),
            "items": [
                {
                    "attribute": i.attribute,
                    "status": i.status,
                    "details": list(i.details),
                    "query_values": list(i.query_values),
                    "candidate_values": list(i.candidate_values),
                    "provenance": [
                        {
                            "source_id": p.source_id,
                            "record_id": p.record_id,
                            "field": p.field,
                        }
                        for p in i.provenance
                    ],
                }
                for i in self.items
            ],
        }


def _item(
    attribute: str,
    status: str,
    details: Iterable[str],
    query: Iterable[str],
    candidate: Iterable[str],
    provenance: Iterable[EvidenceSource],
) -> EvidenceItem:
    return EvidenceItem(
        attribute,
        status,
        tuple(sorted(set(details))),
        tuple(sorted(set(query))),
        tuple(sorted(set(candidate))),
        tuple(sorted(set(provenance))),
    )


# --- candidate parsing ------------------------------------------------------


def _source_or_error(
    value: object, field: str
) -> tuple[EvidenceSource | None, list[FieldError]]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"source_id", "record_id", "field"}
        or not all(isinstance(value[k], str) and value[k].strip() for k in value)
    ):
        return None, [
            FieldError(
                field,
                "invalid_source",
                "source needs nonblank source_id, record_id and field",
            )
        ]
    return EvidenceSource(value["source_id"], value["record_id"], value["field"]), []


def parse_candidate_record(raw: object) -> CandidateRecord:
    """Validate a candidate mapping with the same rules as query attributes.

    Every attribute value must carry a ``source`` object; values without
    provenance are rejected rather than trusted.
    """
    if not isinstance(raw, Mapping):
        raise StructuredInputError(
            [FieldError("", "not_object", "candidate must be an object")]
        )
    errors: list[FieldError] = []
    extra = sorted(
        set(raw) - {"record_id", "entity_type", "countries", "dates", "identifiers"},
        key=str,
    )
    errors.extend(
        FieldError(str(k), "unknown_field", "unknown candidate field") for k in extra
    )
    record_id = raw.get("record_id")
    if not isinstance(record_id, str) or not record_id.strip():
        errors.append(
            FieldError("record_id", "required", "record_id must be nonblank text")
        )
    entity_type, type_errors = entity_type_or_error(raw.get("entity_type", "unknown"))
    errors.extend(type_errors)

    def entries(key: str) -> list[tuple[str, Mapping[str, object], EvidenceSource]]:
        value = raw.get(key, [])
        if not isinstance(value, list):
            errors.append(FieldError(key, "not_list", "must be a list"))
            return []
        found = []
        for index, item in enumerate(value):
            field = f"{key}/{index}"
            if not isinstance(item, Mapping):
                errors.append(FieldError(field, "not_object", "must be an object"))
                continue
            source, source_errors = _source_or_error(
                item.get("source"), f"{field}/source"
            )
            errors.extend(source_errors)
            if source is not None:
                found.append((field, item, source))
        return found

    countries: list[SourcedCountry] = []
    for field, item, source in entries("countries"):
        role = item.get("role", "unspecified")
        if (
            set(item) - {"code", "role", "source"}
            or not isinstance(role, str)
            or role not in COUNTRY_ROLES
        ):
            errors.append(
                FieldError(
                    field, "invalid_country_entry", "country takes code, role, source"
                )
            )
            continue
        code, code_errors = country_or_error(item.get("code"), f"{field}/code")
        errors.extend(code_errors)
        if code is not None:
            countries.append(SourcedCountry(code, role, source))

    dates: list[SourcedDate] = []
    for field, item, source in entries("dates"):
        kind = item.get("kind")
        if (
            set(item) != {"kind", "value", "source"}
            or not isinstance(kind, str)
            or kind not in DATE_KINDS["unknown"]
        ):
            errors.append(
                FieldError(
                    field,
                    "invalid_date_entry",
                    "date takes a known kind, value and source",
                )
            )
            continue
        date, date_errors = parse_partial_date(item["value"], f"{field}/value")
        errors.extend(date_errors)
        if date is not None:
            dates.append(SourcedDate(kind, date, source))

    identifiers: list[SourcedIdentifier] = []
    for field, item, source in entries("identifiers"):
        fields = {k: v for k, v in item.items() if k != "source"}
        identifier, id_errors = identifier_or_error(fields, field)
        errors.extend(id_errors)
        if identifier is not None:
            identifiers.append(SourcedIdentifier(identifier, source))

    if errors:
        raise StructuredInputError(errors)
    assert isinstance(record_id, str) and entity_type is not None
    return CandidateRecord(
        record_id, entity_type, tuple(countries), tuple(dates), tuple(identifiers)
    )


# --- comparisons ------------------------------------------------------------


def _entity_type_items(
    query: ScreeningQuery, candidate: CandidateRecord
) -> list[EvidenceItem]:
    q, c = query.entity_type, candidate.entity_type
    if q == "unknown" or c == "unknown":
        status, details = "not_comparable", ["entity_type_unknown"]
    else:
        status, details = ("agree", []) if q == c else ("conflict", ["values_differ"])
    return [_item("entity_type", status, details, [q], [c], [])]


def _country_items(
    query: ScreeningQuery, candidate: CandidateRecord
) -> list[EvidenceItem]:
    """Compare per query role; ``unspecified`` on either side matches any role.

    When the compared candidate values carry more than one role, each displayed
    value is ``role:code`` so the reviewer still sees which role it came from.
    """
    items: list[EvidenceItem] = []
    used: set[SourcedCountry] = set()
    for entry in query.countries:
        pool = [
            c
            for c in candidate.countries
            if entry.role == "unspecified" or c.role in {entry.role, "unspecified"}
        ]
        used.update(pool)
        attribute = f"country:{entry.role}"
        if not pool:
            items.append(
                _item(attribute, "missing", ["candidate_missing"], [entry.code], [], [])
            )
            continue
        codes = {c.code for c in pool}
        mixed_roles = len({c.role for c in pool}) > 1
        shown = {f"{c.role}:{c.code}" if mixed_roles else c.code for c in pool}
        details = (
            ["role_unspecified"]
            if entry.role == "unspecified" or any(c.role == "unspecified" for c in pool)
            else []
        )
        if entry.code in codes:
            status = "agree"
            if len(codes) > 1:
                details.append("candidate_has_additional_values")
        else:
            status = "conflict"
            details.append("values_differ")
        items.append(
            _item(
                attribute,
                status,
                details,
                [entry.code],
                shown,
                [c.source for c in pool],
            )
        )
    for role in sorted({c.role for c in candidate.countries if c not in used}):
        rest = [c for c in candidate.countries if c not in used and c.role == role]
        items.append(
            _item(
                f"country:{role}",
                "missing",
                ["query_missing"],
                [],
                [c.code for c in rest],
                [c.source for c in rest],
            )
        )
    return items


def _date_relation(left: PartialDate, right: PartialDate) -> str:
    """``equal``, ``consistent`` (agree on shared precision only) or ``differ``."""
    shared = [(left.year, right.year)]
    if left.month is not None and right.month is not None:
        shared.append((left.month, right.month))
        if left.day is not None and right.day is not None:
            shared.append((left.day, right.day))
    if any(a != b for a, b in shared):
        return "differ"
    return "equal" if left == right else "consistent"


def _date_items(
    query: ScreeningQuery, candidate: CandidateRecord
) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    kinds = sorted({d.kind for d in query.dates} | {d.kind for d in candidate.dates})
    for kind in kinds:
        attribute = f"date:{kind}"
        mine = [d.date for d in query.dates if d.kind == kind]
        theirs = [d for d in candidate.dates if d.kind == kind]
        sources = [d.source for d in theirs]
        shown = [d.date.isoformat() for d in theirs]
        if not theirs:
            items.extend(
                _item(
                    attribute, "missing", ["candidate_missing"], [q.isoformat()], [], []
                )
                for q in mine
            )
            continue
        if not mine:
            items.append(
                _item(attribute, "missing", ["query_missing"], [], shown, sources)
            )
            continue
        internal = {d.date for d in theirs}
        base = (
            ["candidate_values_disagree"]
            if any(_date_relation(a, b) == "differ" for a in internal for b in internal)
            else []
        )
        for q in mine:
            relations = {_date_relation(q, d.date) for d in theirs}
            if "equal" in relations:
                status, detail = "agree", "equal"
            elif "consistent" in relations:
                status, detail = "not_comparable", "precision_differs_consistent"
            else:
                status, detail = "conflict", "values_differ"
            items.append(
                _item(
                    attribute, status, [*base, detail], [q.isoformat()], shown, sources
                )
            )
    return items


def _identifier_label(identifier: Identifier) -> str:
    if identifier.issuer_country is None:
        return identifier.key
    return f"{identifier.issuer_country}:{identifier.key}"


def _identifier_items(
    query: ScreeningQuery, candidate: CandidateRecord
) -> list[EvidenceItem]:
    """Same-scheme comparisons; registration IDs only within a compatible issuer.

    Same-scheme candidate identifiers from an incompatible issuer are always
    shown as their own ``not_comparable`` / ``different_issuers`` item, even
    when other identifiers were comparable, so no evidence is dropped.
    """
    items: list[EvidenceItem] = []
    used: set[SourcedIdentifier] = set()
    for mine in query.identifiers:
        attribute = f"identifier:{mine.scheme}"
        same_scheme = [
            c for c in candidate.identifiers if c.identifier.scheme == mine.scheme
        ]
        pool = [
            c
            for c in same_scheme
            if None in {mine.issuer_country, c.identifier.issuer_country}
            or mine.issuer_country == c.identifier.issuer_country
        ]
        used.update(same_scheme)
        label = _identifier_label(mine)
        if not same_scheme:
            items.append(
                _item(attribute, "missing", ["candidate_missing"], [label], [], [])
            )
            continue
        foreign = [c for c in same_scheme if c not in pool]
        if foreign:
            items.append(
                _item(
                    attribute,
                    "not_comparable",
                    ["different_issuers"],
                    [label],
                    [_identifier_label(c.identifier) for c in foreign],
                    [c.source for c in foreign],
                )
            )
        if not pool:
            continue
        details = (
            ["issuer_unspecified"]
            if mine.scheme == "registration"
            and any(
                None in {mine.issuer_country, c.identifier.issuer_country} for c in pool
            )
            else []
        )
        if any(c.identifier.key == mine.key for c in pool):
            status = "agree"
            if len({c.identifier.key for c in pool}) > 1:
                details.append("candidate_has_additional_values")
        else:
            status = "conflict"
            details.append("values_differ")
        items.append(
            _item(
                attribute,
                status,
                details,
                [label],
                [_identifier_label(c.identifier) for c in pool],
                [c.source for c in pool],
            )
        )
    for scheme in sorted(
        {c.identifier.scheme for c in candidate.identifiers if c not in used}
    ):
        rest = [
            c
            for c in candidate.identifiers
            if c not in used and c.identifier.scheme == scheme
        ]
        items.append(
            _item(
                f"identifier:{scheme}",
                "missing",
                ["query_missing"],
                [],
                [_identifier_label(c.identifier) for c in rest],
                [c.source for c in rest],
            )
        )
    return items


def reconcile(
    query: ScreeningQuery, candidate: CandidateRecord
) -> ReconciliationReport:
    """Per-attribute evidence for one candidate; deterministic and score-free."""
    items = [
        *_entity_type_items(query, candidate),
        *_country_items(query, candidate),
        *_date_items(query, candidate),
        *_identifier_items(query, candidate),
    ]
    items.sort(
        key=lambda i: (i.attribute, i.query_values, i.candidate_values, i.status)
    )
    return ReconciliationReport(candidate.record_id, tuple(items))
