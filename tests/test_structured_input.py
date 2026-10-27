"""Structured screening request validation: explicit errors, never guesses."""

import pytest

from watchlist.structured_input import (
    ISO_3166_ALPHA2,
    CountryCode,
    DatedFact,
    ExtractedField,
    Identifier,
    PartialDate,
    StructuredInputError,
    imo_checksum_valid,
    parse_partial_date,
    parse_screening_request,
    unconfirmed_consequential,
    validate_screening_request,
)


def codes(raw) -> set[tuple[str, str]]:
    _, errors = validate_screening_request(raw)
    return {(e.field, e.code) for e in errors}


def test_full_valid_request_round_trips() -> None:
    query = parse_screening_request(
        {
            "name": "MV Ocean Lantern",
            "aliases": ["Океанский Фонарь", "海洋灯笼", "فانوس المحيط"],
            "entity_type": "vessel",
            "countries": ["PA", {"code": "LR", "role": "flag"}],
            "dates": [{"kind": "build", "value": "2001-05"}],
            "identifiers": [
                {"scheme": "imo", "value": "IMO 9074729"},
                {"scheme": "registration", "value": "ab-12/3", "issuer_country": "PA"},
            ],
        }
    )
    assert query.name == "MV Ocean Lantern"
    assert query.aliases == ("Океанский Фонарь", "海洋灯笼", "فانوس المحيط")
    assert query.countries == (
        CountryCode("PA", "unspecified"),
        CountryCode("LR", "flag"),
    )
    assert query.dates == (DatedFact("build", PartialDate(2001, 5)),)
    assert query.identifiers == (
        Identifier("imo", "IMO 9074729", "9074729"),
        Identifier("registration", "ab-12/3", "ab123", "PA"),
    )
    assert query.version == "structured-input-v1"


def test_minimal_request_defaults_to_unknown_type() -> None:
    query = parse_screening_request({"name": "Aster Quill"})
    assert (
        query.entity_type == "unknown" and query.aliases == () and query.countries == ()
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({}, {("name", "required")}),
        ({"name": ""}, {("name", "blank")}),
        ({"name": " 　"}, {("name", "blank")}),
        ({"name": 5}, {("name", "not_text")}),
        ({"name": "x" * 201}, {("name", "too_long")}),
        ({"name": "bad\x00name"}, {("name", "control_character")}),
        ({"name": "bad\ud800"}, {("name", "invalid_unicode")}),
        ({"name": "A", "extra": 1}, {("extra", "unknown_field")}),
        ({"name": "A", "aliases": "B"}, {("aliases", "not_list")}),
        ({"name": "A", "aliases": ["a"]}, {("aliases/0", "duplicate")}),
        ({"name": "A", "aliases": ["B", " b "]}, {("aliases/1", "duplicate")}),
        ({"name": "A", "aliases": ["B"] * 21}, {("aliases", "too_many")}),
        # Visually blank names (zero-width space, ZWJ + word joiner).
        ({"name": "​"}, {("name", "no_letter_or_digit")}),
        ({"name": "‍ ⁠"}, {("name", "no_letter_or_digit")}),
        ({"name": "-- ."}, {("name", "no_letter_or_digit")}),
        # Bidi override/embedding/isolate controls can spoof displayed text.
        ({"name": "Acme‮Ltd"}, {("name", "bidi_control")}),
        ({"name": "⁧Acme⁩"}, {("name", "bidi_control")}),
        ({"name": "A", "aliases": ["‪B"]}, {("aliases/0", "bidi_control")}),
        ({"name": "A", "aliases": ["​"]}, {("aliases/0", "no_letter_or_digit")}),
    ],
)
def test_name_and_alias_errors(raw, expected) -> None:
    assert codes(raw) == expected


def test_joiners_needed_by_arabic_persian_and_indic_stay_allowed() -> None:
    for name in ["می‌خواهم", "क्‍ष", "شركة‍نور", "9"]:
        assert parse_screening_request({"name": name}).name == name


def test_name_limit_matches_cli_boundary() -> None:
    assert parse_screening_request({"name": "x" * 200}).name == "x" * 200


def test_persons_are_out_of_default_scope() -> None:
    assert codes({"name": "A", "entity_type": "person"}) == {
        ("entity_type", "out_of_scope_entity_type")
    }
    assert codes({"name": "A", "entity_type": "ship"}) == {
        ("entity_type", "unknown_entity_type")
    }
    assert (
        parse_screening_request(
            {"name": "A", "entity_type": "organisation"}
        ).entity_type
        == "organization"
    )


def test_country_code_validation() -> None:
    assert len(ISO_3166_ALPHA2) == 249
    assert codes({"name": "A", "countries": ["de"]}) == {
        ("countries/0", "invalid_country_code")
    }
    for bad in ["UK", "EU", "XK", "ZZ", "DEU", "Germany", ""]:
        assert codes({"name": "A", "countries": [bad]}) == {
            ("countries/0", "invalid_country_code")
        }
    assert codes({"name": "A", "countries": [{"code": "DE", "role": "moon"}]}) == {
        ("countries/0/role", "unknown_country_role")
    }
    assert codes({"name": "A", "countries": [{"code": "DE", "role": ["x"]}]}) == {
        ("countries/0/role", "unknown_country_role")
    }
    assert codes({"name": "A", "countries": ["DE", "DE"]}) == {
        ("countries/1", "duplicate")
    }
    query = parse_screening_request(
        {"name": "A", "countries": ["DE", {"code": "DE", "role": "address"}]}
    )
    assert len(query.countries) == 2


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2001", PartialDate(2001)),
        ("2001-02", PartialDate(2001, 2)),
        ("2000-02-29", PartialDate(2000, 2, 29)),
        ("1999-12-31", PartialDate(1999, 12, 31)),
    ],
)
def test_partial_dates(value, expected) -> None:
    date, errors = parse_partial_date(value)
    assert errors == [] and date == expected and date.isoformat() == value
    assert date.precision == ["year", "month", "day"][value.count("-")]


@pytest.mark.parametrize(
    "value, code",
    [
        ("2001-02-29", "invalid_date"),
        ("2001-13", "invalid_date"),
        ("2001-00", "invalid_date"),
        ("2001-04-31", "invalid_date"),
        ("0999", "invalid_date"),
        ("01-02-2001", "invalid_date_format"),
        ("2001/02/03", "invalid_date_format"),
        ("2001-2-3", "invalid_date_format"),
        ("２００１", "invalid_date_format"),
        ("", "invalid_date_format"),
        (2001, "not_text"),
        ("2001-02-03T00:00", "invalid_date_format"),
    ],
)
def test_invalid_dates_are_reported_explicitly(value, code) -> None:
    date, errors = parse_partial_date(value, "dates/0/value")
    assert date is None and [e.code for e in errors] == [code]
    assert errors[0].field == "dates/0/value"


def test_date_kinds_follow_entity_type() -> None:
    assert codes(
        {
            "name": "A",
            "entity_type": "vessel",
            "dates": [{"kind": "incorporation", "value": "2001"}],
        }
    ) == {("dates/0/kind", "unsupported_date_kind")}
    assert codes({"name": "A", "dates": [{"kind": "birth", "value": "2001"}]}) == {
        ("dates/0/kind", "unsupported_date_kind")
    }
    assert codes({"name": "A", "dates": [{"kind": "build"}]}) == {
        ("dates/0", "not_object")
    }
    assert codes(
        {
            "name": "A",
            "dates": [
                {"kind": "build", "value": "2001"},
                {"kind": "build", "value": "2001"},
            ],
        }
    ) == {("dates/1", "duplicate")}


@pytest.mark.parametrize(
    "digits, valid",
    [
        ("9074729", True),
        ("9074728", False),
        ("1234567", True),
        ("123456", False),
        ("907472a", False),
        ("９０７４７２９", False),
    ],
)
def test_imo_checksum(digits, valid) -> None:
    assert imo_checksum_valid(digits) is valid


@pytest.mark.parametrize(
    "identifier, field, code",
    [
        (
            {"scheme": "imo", "value": "9074728"},
            "identifiers/0/value",
            "invalid_imo_checksum",
        ),
        (
            {"scheme": "imo", "value": "IMO-9074729"},
            "identifiers/0/value",
            "invalid_imo_format",
        ),
        (
            {"scheme": "imo", "value": "907472"},
            "identifiers/0/value",
            "invalid_imo_format",
        ),
        (
            {"scheme": "imo", "value": "９０７４７２９"},
            "identifiers/0/value",
            "invalid_imo_format",
        ),
        (
            {"scheme": "imo", "value": "9074729", "issuer_country": "PA"},
            "identifiers/0/issuer_country",
            "not_applicable",
        ),
        (
            {"scheme": "lei", "value": "X"},
            "identifiers/0/scheme",
            "unknown_identifier_scheme",
        ),
        (
            {"scheme": ["imo"], "value": "X"},
            "identifiers/0/scheme",
            "unknown_identifier_scheme",
        ),
        ({"scheme": "registration", "value": "  "}, "identifiers/0/value", "blank"),
        (
            {"scheme": "registration", "value": "- ./"},
            "identifiers/0/value",
            "invalid_identifier",
        ),
        (
            {"scheme": "registration", "value": "x" * 65},
            "identifiers/0/value",
            "invalid_identifier",
        ),
        (
            {"scheme": "registration", "value": "a\tb"},
            "identifiers/0/value",
            "invalid_identifier",
        ),
        (
            {"scheme": "registration", "value": "1", "issuer_country": "de"},
            "identifiers/0/issuer_country",
            "invalid_country_code",
        ),
        (
            {"scheme": "registration", "value": "1", "note": "x"},
            "identifiers/0",
            "unknown_field",
        ),
        ("9074729", "identifiers/0", "not_object"),
        (
            {"scheme": "imo", "value": "0000000"},
            "identifiers/0/value",
            "invalid_imo_number",
        ),
    ],
)
def test_identifier_errors(identifier, field, code) -> None:
    assert codes(
        {"name": "A", "entity_type": "vessel", "identifiers": [identifier]}
    ) == {(field, code)}


def test_imo_is_not_accepted_for_organizations_and_duplicates_are_errors() -> None:
    assert codes(
        {
            "name": "A",
            "entity_type": "organization",
            "identifiers": [{"scheme": "imo", "value": "9074729"}],
        }
    ) == {("identifiers/0", "not_applicable")}
    assert codes(
        {
            "name": "A",
            "identifiers": [
                {"scheme": "registration", "value": "HRB 123"},
                {"scheme": "registration", "value": "hrb-123"},
            ],
        }
    ) == {("identifiers/1", "duplicate")}


def test_all_errors_are_collected_in_deterministic_order() -> None:
    raw = {
        "name": "",
        "countries": ["xx"],
        "dates": [{"kind": "build", "value": "2001-02-30"}],
        "identifiers": [{"scheme": "imo", "value": "1"}],
        "zzz": 1,
    }
    with pytest.raises(StructuredInputError) as info:
        parse_screening_request(raw)
    fields = [e.field for e in info.value.errors]
    assert fields == sorted(fields)
    assert len(info.value.errors) == 5
    with pytest.raises(StructuredInputError) as again:
        parse_screening_request(dict(reversed(list(raw.items()))))
    assert again.value.errors == info.value.errors
    assert codes("not a mapping") == {("", "not_object")}


def test_extraction_contract_requires_confirmation() -> None:
    text = "Vessel Ocean Lantern, IMO 9074729"
    fields = [
        ExtractedField("name", "Ocean Lantern", text, 7, 20),
        ExtractedField("identifiers", "IMO 9074729", text, 22, 33, confirmed=True),
        ExtractedField("note", "x", text, 0, 1),
        ExtractedField("countries", "PA", text, 30, 99),
    ]
    errors = unconfirmed_consequential(fields)
    assert [(e.field, e.code) for e in errors] == [
        ("countries", "invalid_span"),
        ("name", "unconfirmed_extraction"),
        ("note", "unknown_field"),
    ]


def test_extraction_contract_closes_reviewed_gaps() -> None:
    text = "Ocean Lantern aka Sea Lamp"
    fields = [
        # Aliases feed retrieval, so they need confirmation too.
        ExtractedField("aliases", "Sea Lamp", text, 18, 26),
        # Unknown or misspelled field names are errors, not silently passed.
        ExtractedField("identifier", "1", text, 0, 1, confirmed=True),
        ExtractedField("Name", "O", text, 0, 1, confirmed=True),
        # A bool is not a span offset.
        ExtractedField("entity_type", "vessel", text, True, 2, confirmed=True),
        # Name/alias values must be the quoted span text.
        ExtractedField("name", "Ocean Lanterns", text, 0, 13, confirmed=True),
        # Truthy non-bool confirmation is not confirmation.
        ExtractedField("dates", "2001", text, 0, 1, confirmed=1),  # type: ignore[arg-type]
    ]
    errors = unconfirmed_consequential(fields)
    assert [(e.field, e.code) for e in errors] == [
        ("Name", "unknown_field"),
        ("aliases", "unconfirmed_extraction"),
        ("dates", "unconfirmed_extraction"),
        ("entity_type", "invalid_span"),
        ("identifier", "unknown_field"),
        ("name", "value_not_in_span"),
    ]
    confirmed = [
        ExtractedField("aliases", "Sea Lamp", text, 18, 26, confirmed=True),
        ExtractedField("name", "Ocean Lantern", text, 0, 13, confirmed=True),
    ]
    assert unconfirmed_consequential(confirmed) == ()


def test_contradictory_same_kind_dates_are_errors() -> None:
    for second in ("2005", "2001-05", "2001-05-02"):
        assert codes(
            {
                "name": "A",
                "entity_type": "organization",
                "dates": [
                    {"kind": "incorporation", "value": "2001"},
                    {"kind": "incorporation", "value": second},
                ],
            }
        ) == {("dates/1", "conflicting_values")}
    # Different kinds may carry different dates.
    query = parse_screening_request(
        {
            "name": "A",
            "entity_type": "organization",
            "dates": [
                {"kind": "incorporation", "value": "2001"},
                {"kind": "dissolution", "value": "2005"},
            ],
        }
    )
    assert len(query.dates) == 2


@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "−", "－"])
def test_registration_keys_ignore_every_hyphen_variant(dash: str) -> None:
    query = parse_screening_request(
        {
            "name": "A",
            "identifiers": [
                {
                    "scheme": "registration",
                    "value": f"AB{dash}12",
                    "issuer_country": "DE",
                }
            ],
        }
    )
    assert query.identifiers[0].key == "ab12"
