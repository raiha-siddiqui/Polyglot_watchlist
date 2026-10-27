"""Attribute evidence reconciliation: explicit statuses, provenance, no fusion."""

import json

import pytest

from watchlist.reconciliation import (
    NOTICE,
    EvidenceSource,
    parse_candidate_record,
    reconcile,
)
from watchlist.structured_input import StructuredInputError, parse_screening_request


def src(field: str, source: str = "fictional-a") -> dict[str, str]:
    return {"source_id": source, "record_id": "cand-1", "field": field}


def candidate(**fields) -> dict:
    return {"record_id": "cand-1", "entity_type": "vessel", **fields}


def by_attribute(report) -> dict[str, list]:
    grouped: dict[str, list] = {}
    for item in report.items:
        grouped.setdefault(item.attribute, []).append(item)
    return grouped


def test_vessel_example_has_every_status() -> None:
    query = parse_screening_request(
        {
            "name": "MV Ocean Lantern",
            "entity_type": "vessel",
            "countries": [{"code": "PA", "role": "flag"}],
            "dates": [{"kind": "build", "value": "2001"}],
            "identifiers": [{"scheme": "imo", "value": "IMO 9074729"}],
        }
    )
    record = parse_candidate_record(
        candidate(
            countries=[
                {"code": "LR", "role": "flag", "source": src("flag")},
                {"code": "GR", "role": "address", "source": src("owner_address")},
            ],
            dates=[{"kind": "build", "value": "2001-05", "source": src("year_built")}],
            identifiers=[{"scheme": "imo", "value": "9074729", "source": src("imo")}],
        )
    )
    report = reconcile(query, record)
    items = by_attribute(report)
    assert items["entity_type"][0].status == "agree"
    assert items["country:flag"][0].status == "conflict"
    assert items["country:flag"][0].candidate_values == ("LR",)
    assert items["country:flag"][0].provenance == (
        EvidenceSource("fictional-a", "cand-1", "flag"),
    )
    assert items["country:address"][0].status == "missing"
    assert items["country:address"][0].details == ("query_missing",)
    assert items["date:build"][0].status == "not_comparable"
    assert items["date:build"][0].details == ("precision_differs_consistent",)
    assert items["identifier:imo"][0].status == "agree"
    assert report.status_counts() == {
        "agree": 2,
        "conflict": 1,
        "not_comparable": 1,
        "missing": 1,
    }
    assert report.notice == NOTICE
    assert "probability" not in json.dumps(report.to_dict())
    assert not hasattr(report, "score")


def test_missing_is_not_conflict() -> None:
    query = parse_screening_request(
        {
            "name": "A",
            "entity_type": "organization",
            "countries": ["DE"],
            "dates": [{"kind": "incorporation", "value": "1999"}],
        }
    )
    report = reconcile(
        query, parse_candidate_record(candidate(entity_type="organization"))
    )
    statuses = {i.attribute: (i.status, i.details) for i in report.items}
    assert statuses == {
        "entity_type": ("agree", ()),
        "country:unspecified": ("missing", ("candidate_missing",)),
        "date:incorporation": ("missing", ("candidate_missing",)),
    }


def test_unknown_entity_type_is_not_comparable() -> None:
    query = parse_screening_request({"name": "A"})
    report = reconcile(query, parse_candidate_record(candidate()))
    assert report.items[0].status == "not_comparable"
    organization = parse_screening_request({"name": "A", "entity_type": "organization"})
    assert (
        reconcile(organization, parse_candidate_record(candidate())).items[0].status
        == "conflict"
    )


@pytest.mark.parametrize(
    "query_date, candidate_dates, status, details",
    [
        ("2001-05-03", ["2001-05-03"], "agree", ("equal",)),
        ("2001", ["2001"], "agree", ("equal",)),
        (
            "2001-05",
            ["2001-05-03"],
            "not_comparable",
            ("precision_differs_consistent",),
        ),
        ("2001-05-03", ["2001-06"], "conflict", ("values_differ",)),
        ("2001", ["2002-01-01"], "conflict", ("values_differ",)),
        ("2001", ["2001", "2003"], "agree", ("candidate_values_disagree", "equal")),
    ],
)
def test_date_reconciliation(query_date, candidate_dates, status, details) -> None:
    query = parse_screening_request(
        {
            "name": "A",
            "entity_type": "vessel",
            "dates": [{"kind": "build", "value": query_date}],
        }
    )
    record = parse_candidate_record(
        candidate(
            dates=[
                {"kind": "build", "value": value, "source": src(f"built-{i}")}
                for i, value in enumerate(candidate_dates)
            ]
        )
    )
    item = by_attribute(reconcile(query, record))["date:build"][0]
    assert (item.status, item.details) == (status, details)
    assert len(item.provenance) == len(candidate_dates)


def test_conflicting_sources_are_shown_not_resolved() -> None:
    query = parse_screening_request(
        {
            "name": "A",
            "entity_type": "vessel",
            "countries": [{"code": "PA", "role": "flag"}],
        }
    )
    record = parse_candidate_record(
        candidate(
            countries=[
                {"code": "PA", "role": "flag", "source": src("flag", "fictional-a")},
                {"code": "LR", "role": "flag", "source": src("flag", "fictional-b")},
            ]
        )
    )
    item = by_attribute(reconcile(query, record))["country:flag"][0]
    assert item.status == "agree"
    assert item.details == ("candidate_has_additional_values",)
    assert item.candidate_values == ("LR", "PA")
    assert {p.source_id for p in item.provenance} == {"fictional-a", "fictional-b"}


def test_unspecified_country_role_compares_with_any_role() -> None:
    query = parse_screening_request({"name": "A", "countries": ["CY"]})
    record = parse_candidate_record(
        candidate(
            countries=[{"code": "CY", "role": "registration", "source": src("reg")}]
        )
    )
    item = by_attribute(reconcile(query, record))["country:unspecified"][0]
    assert (item.status, item.details) == ("agree", ("role_unspecified",))


def test_registration_identifiers_respect_issuer() -> None:
    def item_for(query_id: dict, candidate_ids: list[dict]):
        query = parse_screening_request(
            {"name": "A", "entity_type": "organization", "identifiers": [query_id]}
        )
        record = parse_candidate_record(
            candidate(
                entity_type="organization",
                identifiers=[
                    {**c, "source": src(f"id-{i}")} for i, c in enumerate(candidate_ids)
                ],
            )
        )
        return by_attribute(reconcile(query, record))["identifier:registration"]

    same = item_for(
        {"scheme": "registration", "value": "HRB 12345", "issuer_country": "DE"},
        [{"scheme": "registration", "value": "hrb-12345", "issuer_country": "DE"}],
    )
    assert (same[0].status, same[0].query_values) == ("agree", ("DE:hrb12345",))
    other = item_for(
        {"scheme": "registration", "value": "HRB 12345", "issuer_country": "DE"},
        [{"scheme": "registration", "value": "HRB 12345", "issuer_country": "AT"}],
    )
    assert (other[0].status, other[0].details) == (
        "not_comparable",
        ("different_issuers",),
    )
    loose = item_for(
        {"scheme": "registration", "value": "123"},
        [{"scheme": "registration", "value": "124", "issuer_country": "CY"}],
    )
    assert (loose[0].status, loose[0].details) == (
        "conflict",
        ("issuer_unspecified", "values_differ"),
    )


def test_candidate_only_identifiers_are_reported_as_query_missing() -> None:
    query = parse_screening_request({"name": "A", "entity_type": "vessel"})
    record = parse_candidate_record(
        candidate(
            identifiers=[{"scheme": "imo", "value": "9074729", "source": src("imo")}]
        )
    )
    item = by_attribute(reconcile(query, record))["identifier:imo"][0]
    assert (item.status, item.details, item.candidate_values) == (
        "missing",
        ("query_missing",),
        ("9074729",),
    )


@pytest.mark.parametrize(
    "raw, field, code",
    [
        (
            candidate(countries=[{"code": "PA", "role": "flag"}]),
            "countries/0/source",
            "invalid_source",
        ),
        (
            candidate(countries=[{"code": "pa", "source": src("x")}]),
            "countries/0/code",
            "invalid_country_code",
        ),
        (
            candidate(
                dates=[{"kind": "build", "value": "2001-02-30", "source": src("x")}]
            ),
            "dates/0/value",
            "invalid_date",
        ),
        (
            candidate(dates=[{"kind": "birth", "value": "2001", "source": src("x")}]),
            "dates/0",
            "invalid_date_entry",
        ),
        (
            candidate(
                identifiers=[{"scheme": "imo", "value": "9074728", "source": src("x")}]
            ),
            "identifiers/0/value",
            "invalid_imo_checksum",
        ),
        ({"entity_type": "vessel"}, "record_id", "required"),
        (candidate(entity_type="person"), "entity_type", "out_of_scope_entity_type"),
        (candidate(score=0.9), "score", "unknown_field"),
    ],
)
def test_invalid_candidates_are_rejected(raw, field, code) -> None:
    with pytest.raises(StructuredInputError) as info:
        parse_candidate_record(raw)
    assert (field, code) in {(e.field, e.code) for e in info.value.errors}


def test_reconciliation_is_deterministic_and_order_independent() -> None:
    query = parse_screening_request(
        {
            "name": "Ocean",
            "entity_type": "vessel",
            "countries": [{"code": "PA", "role": "flag"}, "GR"],
            "dates": [{"kind": "build", "value": "2001"}],
            "identifiers": [{"scheme": "imo", "value": "9074729"}],
        }
    )
    entries = dict(
        countries=[
            {"code": "LR", "role": "flag", "source": src("f1")},
            {"code": "GR", "role": "address", "source": src("a1")},
        ],
        dates=[
            {"kind": "build", "value": "2002", "source": src("b1")},
            {"kind": "registration", "value": "2005", "source": src("r1")},
        ],
        identifiers=[
            {"scheme": "imo", "value": "1234567", "source": src("i1")},
            {"scheme": "registration", "value": "X1", "source": src("x1")},
        ],
    )
    reversed_entries = {k: list(reversed(v)) for k, v in entries.items()}
    first = reconcile(query, parse_candidate_record(candidate(**entries))).to_dict()
    second = reconcile(
        query, parse_candidate_record(candidate(**reversed_entries))
    ).to_dict()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    attributes = [i["attribute"] for i in first["items"]]
    assert attributes == sorted(attributes)


def test_foreign_issuer_identifiers_are_never_dropped() -> None:
    # Regression: with one comparable identifier, a different-issuer one vanished.
    query = parse_screening_request(
        {
            "name": "A",
            "entity_type": "organization",
            "identifiers": [
                {"scheme": "registration", "value": "123", "issuer_country": "DE"}
            ],
        }
    )
    record = parse_candidate_record(
        candidate(
            entity_type="organization",
            identifiers=[
                {
                    "scheme": "registration",
                    "value": "123",
                    "issuer_country": "FR",
                    "source": src("fr", "fictional-fr"),
                },
                {"scheme": "registration", "value": "777", "source": src("x")},
            ],
        )
    )
    items = by_attribute(reconcile(query, record))["identifier:registration"]
    summary = sorted(
        (i.status, i.details, i.candidate_values, tuple(p.field for p in i.provenance))
        for i in items
    )
    assert summary == [
        ("conflict", ("issuer_unspecified", "values_differ"), ("777",), ("x",)),
        ("not_comparable", ("different_issuers",), ("FR:123",), ("fr",)),
    ]
    shown = {v for i in items for v in i.candidate_values}
    assert shown == {"777", "FR:123"}


def test_unicode_dash_registration_ids_agree() -> None:
    query = parse_screening_request(
        {
            "name": "A",
            "entity_type": "organization",
            "identifiers": [
                {"scheme": "registration", "value": "AB-12", "issuer_country": "DE"}
            ],
        }
    )
    record = parse_candidate_record(
        candidate(
            entity_type="organization",
            identifiers=[
                {
                    "scheme": "registration",
                    "value": "AB–12",
                    "issuer_country": "DE",
                    "source": src("id"),
                }
            ],
        )
    )
    item = by_attribute(reconcile(query, record))["identifier:registration"][0]
    assert (item.status, item.candidate_values) == ("agree", ("DE:ab12",))


def test_mixed_candidate_roles_stay_visible() -> None:
    query = parse_screening_request({"name": "A", "countries": ["DE"]})
    record = parse_candidate_record(
        candidate(
            countries=[
                {"code": "FR", "role": "flag", "source": src("flag")},
                {"code": "DE", "role": "address", "source": src("addr")},
            ]
        )
    )
    item = by_attribute(reconcile(query, record))["country:unspecified"][0]
    assert item.status == "agree"
    assert item.candidate_values == ("address:DE", "flag:FR")
    assert item.query_values == ("DE",)
