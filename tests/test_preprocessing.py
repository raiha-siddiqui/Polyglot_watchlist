"""Versioned preprocessing views: control equivalence, traces, scripts, designators."""

import builtins
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from watchlist import preprocessing as pp
from watchlist.normalize import normalize_name

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "preprocessing.json"


@pytest.fixture(scope="module")
def config() -> pp.PreprocessingConfig:
    return pp.load_config(CONFIG_PATH)


@pytest.fixture()
def raw_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


CONTROL_SAMPLES = [
    "  LUMA\t VESPER\n",
    "Ｌｕｍａ　Ｖｅｓｐｅｒ",
    "Élo Nivara",
    "Straße",
    "Мира ВЕЛУНЕ",
    "  سيرا   لومين ",
    "L. Vesper",
    "Luma-Vesper",
    "ΐ",
    "ﬃ",
    "星辰贸易有限公司",
    "ООО «Ромашка»",
    "M/V Ocean Lantern",
    "O’Brien – Trading",
    "Mixed Мира سيرا 星辰",
    "x" * 200,
    "ﷺ",
]


@pytest.mark.parametrize("name", CONTROL_SAMPLES)
def test_control_profile_equals_normalize_name(config, name: str) -> None:
    try:
        expected = normalize_name(name)
    except ValueError:
        with pytest.raises(ValueError):
            pp.preprocess(name, config)
        return
    result = pp.preprocess(name, config, pp.CONTROL_PROFILE)
    assert result.text == expected
    assert result.original == name
    assert pp.verify_trace(result)


@pytest.mark.parametrize("name", ["", " \t\n", " 　", "x" * 201])
def test_control_rejects_what_normalize_name_rejects(config, name: str) -> None:
    with pytest.raises(ValueError):
        normalize_name(name)
    with pytest.raises(pp.PreprocessingError) as info:
        pp.preprocess(name, config)
    assert info.value.code in {"blank", "too_long"}


def test_normalize_name_behaviour_is_unchanged() -> None:
    assert normalize_name("Élo") != normalize_name("Elo")
    assert normalize_name("O’Brien") == "o’brien"


@pytest.mark.parametrize(
    "value, code",
    [(None, "not_text"), (b"abc", "not_text"), ("bad\ud800", "invalid_unicode")],
)
def test_invalid_inputs_have_explicit_codes(config, value, code) -> None:
    with pytest.raises(pp.PreprocessingError) as info:
        pp.preprocess(value, config)
    assert info.value.code == code


def test_trace_records_every_transform_in_order(config) -> None:
    result = pp.preprocess("  O’Brien–Café ", config, "latin-diacritics-folded")
    assert [s.name for s in result.trace] == [
        "nfkc",
        "casefold",
        "unify_punctuation",
        "fold_latin_diacritics",
        "whitespace",
    ]
    assert result.trace[0].before == result.original
    assert result.trace[-1].after == result.text == "o'brien-cafe"
    assert all(s.version == "1" for s in result.trace)
    assert pp.verify_trace(result)
    tampered = pp.PreprocessedName(**{**result.__dict__, "text": "other"})
    assert not pp.verify_trace(tampered)


def test_diacritic_folding_only_touches_latin_marks(config) -> None:
    profile = "latin-diacritics-folded"
    assert pp.preprocess("Élo Ñandú", config, profile).text == "elo nandu"
    # Cyrillic й and ё are letters, Arabic harakat are vocalization: untouched.
    assert pp.preprocess("Йошкар Ёлка", config, profile).text == "йошкар ёлка"
    assert pp.preprocess("مُحَمَّد", config, profile).text == "مُحَمَّد"
    # Letters without a decomposition are not guessed at.
    assert pp.preprocess("Øster Łódź", config, profile).text == "øster łodz"
    # Control profile keeps diacritics.
    assert pp.preprocess("Élo", config).text == "élo"


def test_punctuation_unification(config) -> None:
    result = pp.preprocess("O’Brien — “North” － Co", config, "punctuation-unified")
    assert result.text == 'o\'brien - "north" - co'


def test_transliteration_is_a_separate_lossy_view(config) -> None:
    profile = "latin-transliteration"
    assert pp.preprocess("Мира Велуне", config, profile).text == "mira velune"
    assert pp.preprocess("星辰", config, profile).text == "xingchen"
    arabic = pp.preprocess("سيرا لومين", config, profile)
    assert arabic.original == "سيرا لومين" and arabic.text.isascii()
    assert arabic.trace[2].version.startswith("1+anyascii-")
    assert arabic.scripts == frozenset({"ARABIC"})


def test_transliteration_without_anyascii_fails_explicitly(config, monkeypatch) -> None:
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "anyascii":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(pp.PreprocessingError) as info:
        pp.preprocess("Мира", config, "latin-transliteration")
    assert info.value.code == "dependency_unavailable"
    assert pp.preprocess("Мира", config).text == "мира"


def test_core_import_does_not_load_anyascii() -> None:
    code = (
        "import sys, watchlist.preprocessing, watchlist.structured_input, "
        "watchlist.reconciliation; print('anyascii' in sys.modules)"
    )
    output = subprocess.run(
        [sys.executable, "-B", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
    )
    assert output.stdout.strip() == "False"


@pytest.mark.parametrize(
    "name, scripts, status",
    [
        ("Luma Vesper", {"LATIN"}, "supported"),
        ("Мира Велуне", {"CYRILLIC"}, "supported"),
        ("سيرا لومين", {"ARABIC"}, "supported"),
        ("星辰贸易", {"HAN"}, "supported"),
        ("Mira Мира سيرا 星辰", {"LATIN", "CYRILLIC", "ARABIC", "HAN"}, "supported"),
        ("Αθηνά", {"UNKNOWN"}, "unsupported"),
        ("Luma Αθηνά", {"LATIN", "UNKNOWN"}, "partially_supported"),
        ("12345", set(), "no_script_evidence"),
    ],
)
def test_script_support_status_is_visible(config, name, scripts, status) -> None:
    result = pp.preprocess(name, config)
    assert result.scripts == frozenset(scripts)
    assert result.support_status == status
    assert result.script_method == "unicode-name-category-conservative-v1"


def test_rtl_arabic_text_keeps_logical_order(config) -> None:
    result = pp.preprocess("شركة النور للتجارة", config, entity_type="organization")
    assert result.tokens == ("شركة", "النور", "للتجارة")
    assert result.core_tokens == ("النور", "للتجارة")
    assert (
        result.designators[0].text == "شركة" and result.designators[0].rule == "leading"
    )


@pytest.mark.parametrize(
    "name, entity_type, core, designators",
    [
        (
            "Aster Quill Workshop LLC",
            "organization",
            ("aster", "quill", "workshop"),
            ("llc",),
        ),
        ("Aster Quill Co., Ltd.", "organization", ("aster", "quill"), ("co.,", "ltd.")),
        ("Nordlicht GmbH", "organization", ("nordlicht",), ("gmbh",)),
        ("ООО Ромашка", "organization", ("ромашка",), ("ооо",)),
        ("АО Север", "organization", ("север",), ("ао",)),
        ("星辰贸易有限公司", "organization", ("星辰贸易",), ("有限公司",)),
        ("星辰股份有限公司", "organization", ("星辰",), ("股份有限公司",)),
        ("M/V Ocean Lantern", "vessel", ("ocean", "lantern"), ("m/v",)),
        ("MV Ocean Lantern", "vessel", ("ocean", "lantern"), ("mv",)),
        ("M.V. Ocean Lantern", "vessel", ("ocean", "lantern"), ("m.v.",)),
        ("MV Ocean Lantern LLC", "unknown", ("ocean", "lantern"), ("mv", "llc")),
    ],
)
def test_designators_are_separated_not_dropped(
    config, name, entity_type, core, designators
) -> None:
    result = pp.preprocess(name, config, entity_type=entity_type)
    assert result.core_tokens == core
    assert tuple(d.text for d in result.designators) == designators
    # Evidence is never deleted from the full view.
    assert result.text == normalize_name(name)
    for designator in result.designators:
        assert designator.text in result.tokens[designator.token_index]


def test_designator_rules_respect_entity_type_and_position(config) -> None:
    # Vessel prefixes do not apply to organizations and vice versa.
    assert (
        pp.preprocess("MV Ocean", config, entity_type="organization").designators == ()
    )
    assert pp.preprocess("Ocean LLC", config, entity_type="vessel").designators == ()
    assert pp.preprocess("Ocean LLC", config, entity_type="person").designators == ()
    # Interior tokens are never designators.
    inner = pp.preprocess("Blue LLC Harbor", config, entity_type="organization")
    assert inner.designators == () and inner.core_tokens == ("blue", "llc", "harbor")
    # A name made only of designators keeps all tokens as core (regression:
    # "Ltd LLC" used to separate "ltd" and keep "llc", contradicting the docs).
    only = pp.preprocess("LLC", config, entity_type="organization")
    assert only.core_tokens == ("llc",) and only.designators == ()
    for name, core in [("Ltd LLC", ("ltd", "llc")), ("Co Ltd", ("co", "ltd"))]:
        both = pp.preprocess(name, config, entity_type="organization")
        assert both.core_tokens == core and both.designators == ()
    assert (
        pp.preprocess("有限公司", config, entity_type="organization").designators == ()
    )


def test_organisation_spelling_and_unknown_entity_type(config) -> None:
    assert (
        pp.preprocess("X Ltd", config, entity_type="organisation").entity_type
        == "organization"
    )
    with pytest.raises(pp.PreprocessingError) as info:
        pp.preprocess("X", config, entity_type="ship")
    assert info.value.code == "unknown_entity_type"


def test_unknown_profile_is_an_error(config) -> None:
    with pytest.raises(pp.PreprocessingError) as info:
        pp.preprocess("X", config, "aggressive")
    assert info.value.code == "unknown_profile"


def test_output_length_limits_are_profile_specific(config) -> None:
    long_han = "星" * 200
    assert len(pp.preprocess(long_han, config, "latin-transliteration").text) > 200
    assert pp.preprocess(long_han, config).text == long_han


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda c: c.update(extra=1), "top-level"),
        (lambda c: c.update(config_version="v0"), "config_version"),
        (lambda c: c["profiles"][1]["transforms"].append("stem"), "unknown transforms"),
        (
            lambda c: c["profiles"][0]["transforms"].append("unify_punctuation"),
            "v1-compatible",
        ),
        (lambda c: c["profiles"][0].update(max_output_length=300), "v1-compatible"),
        (lambda c: c["profiles"].append(copy.deepcopy(c["profiles"][1])), "unique"),
        (lambda c: c.update(default_profile="missing"), "default_profile"),
        (
            lambda c: c["designators"]["categories"][0]["edge_tokens"].append("L.L.C."),
            "key form",
        ),
        (
            lambda c: c["designators"]["categories"][0]["han_suffixes"].append("Co"),
            "Han",
        ),
        (
            lambda c: c["designators"]["categories"][0]["entity_types"].append("ship"),
            "unknown type",
        ),
        (
            lambda c: c["designators"]["categories"][1]["edge_tokens"].append("mv"),
            "duplicates",
        ),
    ],
)
def test_invalid_configuration_is_rejected(raw_config, mutate, fragment) -> None:
    mutate(raw_config)
    with pytest.raises(pp.PreprocessingError, match=fragment) as info:
        pp.parse_config(raw_config)
    assert info.value.code == "config_invalid"


def test_results_are_deterministic_and_serializable(config) -> None:
    names = ["Mira Мира سيرا 星辰有限公司", "MV Ocean LLC", "Αθηνά"]
    first = [
        json.dumps(
            pp.preprocess(n, config, p.name).to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
        for n in names
        for p in config.profiles
    ]
    second = [
        json.dumps(
            pp.preprocess(n, config, p.name).to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )
        for n in names
        for p in config.profiles
    ]
    assert first == second


def test_collisions_are_measured_per_profile(config) -> None:
    names = ["Élo Nivara", "Elo Nivara", "Мира", "Mira", "O’Brien", "O'Brien"]
    assert pp.view_collisions(names, config) == ()
    assert pp.view_collisions(names, config, "latin-diacritics-folded") == (
        ("elo nivara", ("Elo Nivara", "Élo Nivara")),
        ("o'brien", ("O'Brien", "O’Brien")),
    )
    translit = dict(pp.view_collisions(names, config, "latin-transliteration"))
    assert translit["mira"] == ("Mira", "Мира")
    report = pp.evaluate_views(names, config)
    assert list(report) == [p.name for p in config.profiles]
    assert report["v1-compatible"]["changed_from_control"] == 0
    assert report["v1-compatible"]["collision_groups"] == 0
    assert all(item["inputs"] == len(names) for item in report.values())


def test_cli_show_and_evaluate(tmp_path) -> None:
    env_cmd = [sys.executable, "-B", "-m", "watchlist.preprocessing"]
    shown = subprocess.run(
        [
            *env_cmd,
            "show",
            "--config",
            str(CONFIG_PATH),
            "--entity-type",
            "vessel",
            "--name",
            "M/V Ocean Lantern",
        ],
        capture_output=True,
        check=True,
        cwd=ROOT,
    )
    payload = json.loads(shown.stdout.decode("utf-8"))
    assert payload["designators"][0]["key"] == "m/v"
    fixture = tmp_path / "fixture.json"
    fixture.write_text(
        json.dumps(
            [
                {
                    "entity_id": "f-1",
                    "english": "Blue Harbor",
                    "arabic": "الميناء الأزرق",
                    "partition": "development",
                    "synthetic": True,
                },
                {
                    "entity_id": "f-2",
                    "english": "Other",
                    "arabic": "آخر",
                    "partition": "test",
                    "synthetic": True,
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    evaluated = subprocess.run(
        [
            *env_cmd,
            "evaluate",
            "--config",
            str(CONFIG_PATH),
            "--input",
            str(fixture),
            "--partition",
            "development",
        ],
        capture_output=True,
        check=True,
        cwd=ROOT,
    )
    report = json.loads(evaluated.stdout.decode("utf-8"))
    assert "fictional" in report["evidence"]
    assert report["profiles"]["v1-compatible"]["inputs"] == 2
    assert report["source_partitions"] == ["development", "ambiguity"]
    # Test partitions are refused and a misspelled partition is not "0 inputs".
    for partition, code in [("test", "partition_not_allowed"), ("devel", None)]:
        refused = subprocess.run(
            [
                *env_cmd,
                "evaluate",
                "--config",
                str(CONFIG_PATH),
                "--input",
                str(fixture),
                "--partition",
                partition,
            ],
            capture_output=True,
            cwd=ROOT,
        )
        assert refused.returncode == 2 and refused.stdout == b""
        assert json.loads(refused.stderr.decode("utf-8"))["error"] == (
            code or "partition_not_allowed"
        )
    failed = subprocess.run(
        [*env_cmd, "show", "--config", str(CONFIG_PATH), "--name", " "],
        capture_output=True,
        cwd=ROOT,
    )
    assert failed.returncode == 2
    assert json.loads(failed.stderr.decode("utf-8"))["error"] == "blank"


def test_evaluate_partition_guard_and_ambiguity_mapping(tmp_path) -> None:
    fixture = tmp_path / "fixture.json"
    records = [
        {"english": "Dev", "partition": "development", "synthetic": True},
        {"english": "Amb", "partition": "ambiguity", "synthetic": True},
        {"english": "Held", "partition": "test", "synthetic": True},
    ]
    fixture.write_text(json.dumps(records), encoding="utf-8")
    assert pp._fixture_names(fixture, "development") == ["Amb", "Dev"]
    for partition in ("test", "final-test", "ambiguity", "devel"):
        with pytest.raises(pp.PreprocessingError) as info:
            pp._fixture_names(fixture, partition)
        assert info.value.code == "partition_not_allowed"
    fixture.write_text(json.dumps(records[2:]), encoding="utf-8")
    with pytest.raises(pp.PreprocessingError) as empty:
        pp._fixture_names(fixture, "development")
    assert empty.value.code == "zero_support"


@pytest.mark.parametrize("profile", ["v1-compatible", "punctuation-unified"])
@pytest.mark.parametrize(
    "name, designator",
    [
        ("«ООО» Ромашка", "«ооо»"),
        ("“Ltd” Blue", "“ltd”"),
        ("【有限公司】 Blue", "【有限公司】"),
        ("Blue （LLC）", "(llc)"),
    ],
)
def test_quoted_designators_separate_in_every_profile(
    config, profile, name, designator
) -> None:
    result = pp.preprocess(name, config, profile, entity_type="organization")
    assert len(result.designators) == 1
    assert result.designators[0].key in {"ооо", "ltd", "有限公司", "llc"}
    if profile == "v1-compatible":
        assert result.designators[0].text == designator


def test_core_tokens_depend_on_profile_for_han_suffixes(config) -> None:
    control = pp.preprocess("星辰贸易有限公司", config, entity_type="organization")
    latin = pp.preprocess(
        "星辰贸易有限公司", config, "latin-transliteration", entity_type="organization"
    )
    assert control.core_tokens == ("星辰贸易",) and control.designators
    # Documented limitation: no Han suffix survives transliteration.
    assert latin.designators == () and len(latin.core_tokens) == 1


def test_hyphen_variants_are_public_and_unified(config) -> None:
    result = pp.preprocess(
        "a" + "b".join(pp.HYPHEN_VARIANTS), config, "punctuation-unified"
    )
    assert set(result.text) <= {"a", "b", "-"}
