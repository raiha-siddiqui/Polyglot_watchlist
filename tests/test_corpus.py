"""Fictional offline acceptance checks for candidate evidence, never real labels."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
import sqlite3
import stat

import pytest

from watchlist import corpus
from watchlist.corpus import (
    CorpusValidationError,
    audit_candidate_release,
    build_candidate_release,
    load_candidate_release,
    prepare_source,
)
from watchlist.scripts import SCRIPT_METHOD, UNICODE_VERSION, script_set


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def fictional_record(source="opensanctions_un", provider="17", name="Lumen Orchard"):
    field = {
        "opensanctions_un": "properties.name",
        "ofac_sdn": "sdnEntry.lastName",
        "eu_consolidated": "sanctionEntity.nameAlias@wholeName",
    }[source]
    schema = {
        "opensanctions_un": "Organization",
        "ofac_sdn": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML#sdnList.sdnEntry:Entity",
        "eu_consolidated": "export.sanctionEntity:code=enterprise;classificationCode=E",
    }[source]
    basis = {
        "opensanctions_un": "ftm-explicit-schema",
        "ofac_sdn": None,
        "eu_consolidated": "eu-subjectType-classificationCode-E",
    }[source]
    return {
        "source_id": source,
        "provider_id": provider,
        "entity_type": "organization",
        "primary_name": name,
        "aliases": [
            {"value": name, "field": field, "language": None, "attributes": []}
        ],
        "provider_schema": schema,
        "referents": [],
        "provider_datasets": [],
        "provider_topics": ["sanction"] if source == "opensanctions_un" else [],
        "classification_basis": basis,
        "qualified_id": f"{source}:{provider}",
        "source_role": "sanctions_reference",
    }


def selection_statistics(records):
    aliases = [alias for record in records for alias in record["aliases"]]
    names = {alias["value"] for alias in aliases}
    counts = Counter(
        name
        for record in records
        for name in {alias["value"] for alias in record["aliases"]}
    )
    return {
        "input_records": len(records) + 3,
        "selected_provider_groups": len(records),
        "excluded_person_records": 1,
        "excluded_other_records": 2,
        "excluded_by_limit": 0,
        "alias_evidence_count": len(aliases),
        "unique_name_strings": len(names),
        "repeated_name_evidence": len(aliases) - len(names),
        "same_name_distinct_provider_groups": sum(
            count > 1 for count in counts.values()
        ),
        "provider_referents": sum(len(record["referents"]) for record in records),
        "name_language_counts": dict(
            Counter(alias["language"] or "unknown" for alias in aliases)
        ),
        "name_script_counts": dict(
            Counter(
                script
                for alias in aliases
                for script in (script_set(alias["value"]) or {"NO_SCRIPT_EVIDENCE"})
            )
        ),
        "script_count_method": SCRIPT_METHOD,
        "unicode_version": UNICODE_VERSION,
        "script_count_unit": "alias evidence; mixed scripts count in each; UNKNOWN is unresolved",
        "cross_source_identity_adjudicated": False,
    }


class FictionalInputs:
    """Author an invented receipt/permission pair; no downloaded source bytes."""

    def __init__(self, root, records=None, source="opensanctions_un"):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.records = records if records is not None else [fictional_record(source)]
        self.selection = b"".join(encoded(record) for record in self.records)
        self.selection_path = self.root / f"{source}.jsonl"
        self.receipt_path = self.root / "receipt.json"
        self.permission_path = self.root / "purpose.json"
        self.evidence_path = self.root / "fictional-terms.txt"
        self.evidence_path.write_bytes(
            b"Authored fictional permission evidence. No real publisher grant.\n"
        )
        evidence = {
            "path": self.evidence_path.name,
            "sha256": digest(self.evidence_path.read_bytes()),
        }
        self.receipt = {
            "schema_version": 1,
            "source_id": source,
            "role": "sanctions_reference",
            "status": "success",
            "requested_url": "https://example.invalid/fictional.xml",
            "final_url": "https://example.invalid/fictional.xml",
            "permission": {
                "status": "approved",
                "reviewed_at": "2026-10-25",
                "license": "fictional fixture only",
                "terms_urls": ["https://example.invalid/fictional-terms"],
                "evidence_paths": [evidence["path"]],
                "rationale": "Authored offline test only.",
            },
            "permission_evidence": [deepcopy(evidence)],
            "policy_sha256": digest(b"fictional policy"),
            "max_response_body_bytes": 10000,
            "request_timeout_seconds": 30,
            "source_deadline_seconds": 120,
            "attempts": [
                {
                    "attempt": 1,
                    "bytes_received": 15,
                    "status": "success",
                    "http_status": 200,
                    "complete_body_sha256": digest(b"fictional body!"),
                }
            ],
            "bytes_received": 15,
            "complete_body_sha256": digest(b"fictional body!"),
            "complete_body_bytes": 15,
            "normalized_selection_sha256": digest(self.selection),
            "records_path": self.selection_path.name,
            "raw_retained": False,
            "attribution": "All names and receipts are fictional test evidence.",
            "parser_version": "authored-fixture-v1",
            "source_version": "fictional-2026-10-25",
            "source_version_field": "fixture.version",
            "version_fallback": None,
            "selected_provider_schemas": sorted(
                {record["provider_schema"] for record in self.records}
            ),
            "container_format": None,
            "container_member": None,
            "decoded_bytes": None,
            "decoded_sha256": None,
            "stats": selection_statistics(self.records),
        }
        name_fields = {}
        for record in self.records:
            for alias in record["aliases"]:
                name_fields.setdefault(alias["field"], set()).update(
                    pair[0] for pair in alias["attributes"]
                )
        self.permission = {
            "schema_version": 1,
            "review_id": "fictional-purpose-review-1",
            "source_id": source,
            "status": "approved",
            "purpose": "local_candidate_corpus_preparation",
            "operations": ["retain_selected_names", "prepare_candidate_corpus"],
            "receipt_sha256": "filled-on-save",
            "selection_sha256": digest(self.selection),
            "record_fields": sorted(corpus.RECORD_FIELDS),
            "alias_fields": sorted(corpus.ALIAS_FIELDS),
            "entity_types": ["organization"],
            "name_fields": {key: sorted(value) for key, value in name_fields.items()}
            or {"properties.name": []},
            "evidence": [deepcopy(evidence)],
            "reviewed_at": "2026-10-25",
            "reviewer": "fictional test role; not a human-review claim",
            "attribution": "Authored fictional fixture.",
            "conditions": ["Candidate preparation only."],
        }

    def save(self, *, pin_receipt=True):
        self.selection_path.write_bytes(self.selection)
        self.receipt_path.write_bytes(encoded(self.receipt))
        if pin_receipt:
            self.permission["receipt_sha256"] = digest(self.receipt_path.read_bytes())
        self.permission_path.write_bytes(encoded(self.permission))
        return {
            "expected_receipt_sha256": digest(self.receipt_path.read_bytes()),
            "expected_permission_sha256": digest(self.permission_path.read_bytes()),
            "evidence_root": self.root,
        }

    def prepare(self):
        kwargs = self.save()
        return prepare_source(
            self.selection_path, self.receipt_path, self.permission_path, **kwargs
        )


@pytest.fixture
def inputs(tmp_path):
    return FictionalInputs(tmp_path / "inputs")


def test_full_evidence_and_source_local_collisions_survive_publication(tmp_path):
    first = fictional_record()
    first["referents"] = ["17:previous"]
    first["aliases"] += [
        {
            "value": "Lumen Orchard",
            "field": "properties.alias",
            "language": "en",
            "attributes": [["qualifier", "original"]],
        },
        {
            "value": "Lumen Orchard",
            "field": "properties.alias",
            "language": "fr",
            "attributes": [["qualifier", "original"]],
        },
        {
            "value": "灯园示例",
            "field": "properties.alias",
            "language": "zh",
            "attributes": [],
        },
    ]
    a = FictionalInputs(tmp_path / "a", [first]).prepare()
    b = FictionalInputs(
        tmp_path / "b", [fictional_record("ofac_sdn", name="Lumen Orchard")], "ofac_sdn"
    ).prepare()
    path = tmp_path / "candidate.sqlite"
    built = build_candidate_release(path, (b, a), release_id="fictional-v1")
    loaded = load_candidate_release(
        path, expected_artifact_sha256=built.artifact_sha256
    )
    assert loaded == built
    assert {item.source_record_key for item in loaded.observations} == {
        ("opensanctions_un", "17"),
        ("ofac_sdn", "17"),
    }
    original = next(
        item for item in loaded.observations if item.source_id == "opensanctions_un"
    )
    assert original.referents == ("17:previous",)
    assert [item.value for item in original.aliases] == [
        item["value"] for item in first["aliases"]
    ]
    assert [item.language for item in original.aliases] == [None, "en", "fr", "zh"]
    assert original.aliases[1].attributes == (("qualifier", "original"),)
    assert len({alias.evidence_id for alias in original.aliases}) == 4
    assert len({item.observation_id for item in loaded.observations}) == 2
    summary = audit_candidate_release(path)
    assert summary["source_observations"] == 2 and summary["name_evidence"] == 5
    assert summary["status"] == "prepared_candidate"
    assert not summary["reviewed_corpus_accepted"] and not summary["training_eligible"]
    assert not summary["evaluation_eligible"] and not summary["fresh_final_eligible"]
    assert (
        summary["human_review_count"]
        == summary["adjudicated_label_count"]
        == summary["partition_count"]
        == 0
    )
    assert (
        summary["accepted_independent_families"]
        == summary["accepted_reference_families"]
        == summary["accepted_absent_families"]
        == 0
    )
    assert not summary["raw_replay_available"]
    assert path.stat().st_mode & 0o777 == 0o400
    with pytest.raises(CorpusValidationError, match="not an accepted reviewed corpus"):
        audit_candidate_release(path, require_reviewed_corpus=True)


def test_publisher_refresh_and_permission_version_have_distinct_observations(inputs):
    a = inputs.prepare()
    inputs.receipt["source_version"] = "fictional-refresh"
    b = inputs.prepare()
    inputs.permission["review_id"] = "fictional-second-purpose-review"
    c = inputs.prepare()
    assert len({item.snapshot_id for item in (a, b, c)}) == 3
    assert len({item.observations[0].observation_id for item in (a, b, c)}) == 3
    assert a.observations[0].source_record_key == b.observations[0].source_record_key


def test_eu_boolean_whitespace_is_preserved_without_weak_alias_inference(tmp_path):
    record = fictional_record("eu_consolidated")
    record["aliases"][0]["attributes"] = [
        ["strong", " \tfalse\r\n"],
        ["nameLanguage", "EN"],
    ]
    record["aliases"][0]["language"] = "EN"
    source = FictionalInputs(tmp_path / "eu", [record], "eu_consolidated").prepare()
    alias = source.observations[0].aliases[0]
    assert alias.attributes == (("strong", " \tfalse\r\n"), ("nameLanguage", "EN"))
    assert not hasattr(alias, "weak") and not hasattr(alias, "label")


@pytest.mark.parametrize(
    "key,value",
    [
        ("status", "pending"),
        ("purpose", "download_only"),
        ("source_id", "ofac_sdn"),
        ("schema_version", True),
        ("operations", ["prepare_candidate_corpus"]),
        ("operations", ["retain_selected_names", "prepare_candidate_corpus", "train"]),
        ("record_fields", ["primary_name"]),
        ("alias_fields", ["value", "field"]),
        ("entity_types", ["person"]),
        ("name_fields", {"properties.alias": []}),
        ("conditions", []),
        ("reviewer", ""),
        ("reviewed_at", "yesterday"),
        ("selection_sha256", "f" * 64),
        ("training_eligible", True),
        ("evidence", []),
    ],
)
def test_missing_or_expanded_purpose_permissions_fail(inputs, key, value):
    inputs.permission[key] = value
    with pytest.raises(CorpusValidationError):
        inputs.prepare()
    assert not list(inputs.root.glob("*.sqlite"))


def test_permission_must_pin_the_exact_receipt(inputs):
    kwargs = inputs.save(pin_receipt=False)
    with pytest.raises(CorpusValidationError, match="exact candidate inputs"):
        prepare_source(
            inputs.selection_path, inputs.receipt_path, inputs.permission_path, **kwargs
        )


@pytest.mark.parametrize("which", ["receipt", "permission", "selection", "evidence"])
def test_changed_input_hash_is_rejected(inputs, which):
    kwargs = inputs.save()
    path = {
        "receipt": inputs.receipt_path,
        "permission": inputs.permission_path,
        "selection": inputs.selection_path,
        "evidence": inputs.evidence_path,
    }[which]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(CorpusValidationError):
        prepare_source(
            inputs.selection_path, inputs.receipt_path, inputs.permission_path, **kwargs
        )


@pytest.mark.parametrize(
    "key,value",
    [
        ("status", "failed"),
        ("schema_version", True),
        ("complete_body_sha256", None),
        ("complete_body_sha256", "f" * 64),
        ("complete_body_bytes", 14),
        ("complete_body_bytes", True),
        ("bytes_received", 16),
        ("max_response_body_bytes", 14),
        ("raw_retained", True),
        ("role", "supplemental_unlabelled"),
        ("source_id", "ofac_sdn"),
        ("records_path", "../opensanctions_un.jsonl"),
        ("version_fallback", "complete_body_sha256"),
        ("source_version_field", None),
        ("normalized_selection_sha256", "0" * 64),
        ("selected_provider_schemas", []),
        ("attempts", []),
        ("extra_payload", {"unapproved": "field"}),
    ],
)
def test_receipt_inconsistency_fails_admission(inputs, key, value):
    inputs.receipt[key] = value
    with pytest.raises(CorpusValidationError):
        inputs.prepare()


@pytest.mark.parametrize(
    "key,value",
    [
        ("status", "failed"),
        ("attempt", 2),
        ("attempt", True),
        ("bytes_received", 14),
        ("http_status", 206),
        ("http_status", True),
        ("complete_body_sha256", "a" * 64),
        ("partial_body_sha256", "a" * 64),
    ],
)
def test_failed_partial_or_misordered_final_attempt_is_rejected(inputs, key, value):
    inputs.receipt["attempts"][0][key] = value
    with pytest.raises(CorpusValidationError):
        inputs.prepare()


def test_prior_failed_transfer_is_counted_without_promoting_partial_digest(inputs):
    last = inputs.receipt["attempts"][0]
    last["attempt"] = 2
    inputs.receipt["attempts"].insert(
        0,
        {
            "attempt": 1,
            "bytes_received": 4,
            "status": "failed",
            "partial_body_sha256": digest(b"part"),
            "error_code": "fixture_interrupted",
        },
    )
    inputs.receipt["bytes_received"] = 19
    prepared = inputs.prepare()
    assert prepared.source_id == "opensanctions_un"
    inputs.receipt["complete_body_sha256"] = digest(b"part")
    with pytest.raises(CorpusValidationError):
        inputs.prepare()


def test_explicit_content_hash_fallback_and_historical_no_http_status(inputs):
    inputs.receipt["source_version"] = inputs.receipt["source_version_field"] = None
    inputs.receipt["version_fallback"] = "complete_body_sha256"
    del inputs.receipt["attempts"][0]["http_status"]
    assert inputs.prepare().observations
    inputs.receipt["version_fallback"] = None
    with pytest.raises(CorpusValidationError, match="fallback"):
        inputs.prepare()


@pytest.mark.parametrize(
    "key",
    [
        "input_records",
        "selected_provider_groups",
        "excluded_person_records",
        "alias_evidence_count",
        "unique_name_strings",
        "repeated_name_evidence",
        "same_name_distinct_provider_groups",
        "provider_referents",
    ],
)
def test_incomplete_or_false_receipt_counts_are_rejected(inputs, key):
    inputs.receipt["stats"][key] += 1
    with pytest.raises(CorpusValidationError):
        inputs.prepare()


@pytest.mark.parametrize(
    "key,value",
    [
        ("name_language_counts", {"en": 1}),
        ("name_script_counts", {"Han": 1}),
        ("cross_source_identity_adjudicated", True),
        ("selected_provider_groups", True),
        ("unicode_version", "fictional-other-version"),
    ],
)
def test_no_language_inference_or_adjudication_claim_in_stats(inputs, key, value):
    inputs.receipt["stats"][key] = value
    with pytest.raises(CorpusValidationError):
        inputs.prepare()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_language",
        "extra_field",
        "duplicate_attribute",
        "unpermitted_attribute",
        "unpermitted_name_field",
        "duplicate_evidence",
        "missing_primary",
        "invalid_name_control",
        "person_type",
        "person_schema",
        "ambiguous_legal_entity",
        "source_mismatch",
        "qualified_key",
        "duplicate_provider",
        "out_of_order",
        "missing_newline",
        "blank_line",
        "duplicate_json_key",
    ],
)
def test_selection_shape_and_evidence_completeness(tmp_path, mutation):
    record = fictional_record()
    records = [record]
    if mutation == "missing_language":
        del record["aliases"][0]["language"]
    elif mutation == "extra_field":
        record["private_address"] = "unapproved"
    elif mutation in {"duplicate_attribute", "unpermitted_attribute"}:
        record["aliases"][0]["attributes"] = [["flag", "first"]] * (
            2 if mutation == "duplicate_attribute" else 1
        )
    elif mutation == "unpermitted_name_field":
        record["aliases"][0]["field"] = "properties.unapproved"
    elif mutation == "duplicate_evidence":
        record["aliases"] *= 2
    elif mutation == "missing_primary":
        record["primary_name"] = "Copper Kite"
    elif mutation == "invalid_name_control":
        record["aliases"][0]["value"] += "\x01"
    elif mutation == "person_type":
        record["entity_type"] = "person"
    elif mutation == "person_schema":
        record["provider_schema"] = "Person"
    elif mutation == "ambiguous_legal_entity":
        record["provider_schema"] = "LegalEntity"
    elif mutation == "source_mismatch":
        record["source_id"] = "ofac_sdn"
    elif mutation == "qualified_key":
        record["qualified_id"] = "17"
    elif mutation == "duplicate_provider":
        records.append(deepcopy(record))
    elif mutation == "out_of_order":
        records.insert(0, fictional_record(provider="99", name="Velvet Compass"))
    # Invalid absent language needs temporary fixture statistics only; admission
    # still sees the unmodified malformed JSON field set.
    stats_records = deepcopy(records)
    for item in stats_records:
        for alias in item["aliases"]:
            alias.setdefault("language", None)
    inputs = FictionalInputs(tmp_path / mutation, stats_records)
    inputs.selection = b"".join(encoded(item) for item in records)
    if mutation in {"unpermitted_attribute", "unpermitted_name_field"}:
        inputs.permission["name_fields"] = {"properties.name": []}
    if mutation == "missing_newline":
        inputs.selection = inputs.selection.rstrip(b"\n")
    if mutation == "blank_line":
        inputs.selection += b"\n"
    if mutation == "duplicate_json_key":
        inputs.selection = inputs.selection.replace(
            b'"provider_id": "17"', b'"provider_id":"17","provider_id":"99"'
        )
    inputs.receipt["normalized_selection_sha256"] = inputs.permission[
        "selection_sha256"
    ] = digest(inputs.selection)
    with pytest.raises(CorpusValidationError):
        inputs.prepare()


def test_narrow_legal_entity_mapping_needs_both_dataset_and_topic(tmp_path):
    record = fictional_record()
    record["provider_schema"] = "LegalEntity"
    record["classification_basis"] = "reviewed-un-entity-mapping"
    record["provider_datasets"] = ["un_sc_sanctions"]
    assert FictionalInputs(tmp_path / "valid", [record]).prepare().observations
    record["provider_topics"] = []
    with pytest.raises(CorpusValidationError, match="non-person"):
        FictionalInputs(tmp_path / "invalid", [record]).prepare()


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "folder/../outside",
        "folder//file",
        "./file",
        "C:\\file",
    ],
)
def test_permission_evidence_cannot_escape_root(inputs, path):
    inputs.permission["evidence"][0]["path"] = path
    with pytest.raises(CorpusValidationError, match="path"):
        inputs.prepare()


def test_symlinked_evidence_and_parent_directories_are_rejected(inputs):
    original = inputs.evidence_path.read_bytes()
    outside = inputs.root.parent / "outside-evidence"
    outside.write_bytes(original)
    inputs.evidence_path.unlink()
    inputs.evidence_path.symlink_to(outside)
    with pytest.raises((CorpusValidationError, OSError)):
        inputs.prepare()
    assert outside.read_bytes() == original
    inputs.evidence_path.unlink()
    inputs.evidence_path.write_bytes(original)
    linked_root = inputs.root.parent / "linked-inputs"
    linked_root.symlink_to(inputs.root, target_is_directory=True)
    kwargs = inputs.save()
    kwargs["evidence_root"] = linked_root
    with pytest.raises((CorpusValidationError, OSError)):
        prepare_source(
            inputs.selection_path, inputs.receipt_path, inputs.permission_path, **kwargs
        )


def test_evidence_missing_and_extra_are_not_silently_ignored(inputs):
    prepared = inputs.prepare()
    with pytest.raises(CorpusValidationError, match="incomplete"):
        build_candidate_release(
            inputs.root / "missing.sqlite",
            [replace(prepared, evidence=())],
            release_id="missing",
        )
    extra = corpus.SavedEvidence("unreferenced.txt", digest(b"extra"), b"extra")
    with pytest.raises(CorpusValidationError, match="unreferenced"):
        build_candidate_release(
            inputs.root / "extra.sqlite",
            [replace(prepared, evidence=prepared.evidence + (extra,))],
            release_id="extra",
        )


def test_prepare_buffers_are_revalidated_and_source_files_not_reopened(
    inputs, tmp_path
):
    prepared = inputs.prepare()
    changed = replace(prepared.observations[0], primary_name="Changed name")
    forged = replace(prepared, observations=(changed,))
    path = tmp_path / "candidate.sqlite"
    with pytest.raises(CorpusValidationError, match="prepared source differs"):
        build_candidate_release(path, [forged], release_id="fictional")
    assert not path.exists()
    inputs.selection_path.write_bytes(b"source changed after preparation")
    build_candidate_release(path, [prepared], release_id="fictional")
    assert load_candidate_release(path).observations[0].primary_name == "Lumen Orchard"
    assert inputs.selection_path.read_bytes() == b"source changed after preparation"


@pytest.mark.parametrize(
    "existing", ["file", "directory", "symlink", "source_path", "source_hardlink"]
)
def test_no_overwrite_and_preservation_of_all_inputs(inputs, tmp_path, existing):
    prepared = inputs.prepare()
    path = tmp_path / "existing.sqlite"
    if existing == "file":
        path.write_bytes(b"older artifact")
    elif existing == "directory":
        path.mkdir()
    elif existing == "symlink":
        path.symlink_to(tmp_path / "missing-target")
    elif existing == "source_path":
        path = inputs.selection_path
    else:
        os.link(inputs.selection_path, path)
    before = {item.name: item.read_bytes() for item in inputs.root.iterdir()}
    with pytest.raises(FileExistsError):
        build_candidate_release(path, [prepared], release_id="fictional")
    assert {item.name: item.read_bytes() for item in inputs.root.iterdir()} == before
    if existing == "file":
        assert path.read_bytes() == b"older artifact"
    if existing == "symlink":
        assert path.is_symlink() and not (tmp_path / "missing-target").exists()
    assert not list(tmp_path.glob(".candidate-*.tmp"))


@pytest.mark.parametrize("failure", ["write", "validation", "link", "interrupt"])
def test_interrupted_stage_never_leaves_final_or_partial_artifact(
    inputs, tmp_path, monkeypatch, failure
):
    prepared = inputs.prepare()
    path = tmp_path / "candidate.sqlite"
    before = {item.name: item.read_bytes() for item in inputs.root.iterdir()}

    def fail(*args, **kwargs):
        if failure == "interrupt":
            raise KeyboardInterrupt("fictional interruption")
        raise OSError("fictional staging failure")

    if failure in {"write", "interrupt"}:
        monkeypatch.setattr(corpus.os, "write", fail)
    elif failure == "link":
        monkeypatch.setattr(corpus.os, "link", fail)
    else:
        load = corpus._load_bytes
        calls = 0

        def fail_staged(payload, expected):
            nonlocal calls
            calls += 1
            if calls == 2:
                fail()
            return load(payload, expected)

        monkeypatch.setattr(corpus, "_load_bytes", fail_staged)
    with pytest.raises((OSError, KeyboardInterrupt)):
        build_candidate_release(path, [prepared], release_id="fictional")
    assert not path.exists() and not list(tmp_path.glob(".candidate-*.tmp"))
    assert {item.name: item.read_bytes() for item in inputs.root.iterdir()} == before


def test_atomic_link_refuses_competing_destination(inputs, tmp_path, monkeypatch):
    prepared = inputs.prepare()
    path = tmp_path / "candidate.sqlite"
    original = corpus.os.link

    def racing_link(source, target, **kwargs):
        fd = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=kwargs["dst_dir_fd"],
        )
        os.write(fd, b"competing complete artifact")
        os.close(fd)
        return original(source, target, **kwargs)

    monkeypatch.setattr(corpus.os, "link", racing_link)
    with pytest.raises(FileExistsError):
        build_candidate_release(path, [prepared], release_id="fictional")
    assert path.read_bytes() == b"competing complete artifact"
    assert not list(tmp_path.glob(".candidate-*.tmp"))


def test_missing_or_symlinked_output_parent_is_not_created(inputs, tmp_path):
    prepared = inputs.prepare()
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        build_candidate_release(
            missing / "candidate.sqlite", [prepared], release_id="fictional"
        )
    assert not missing.exists()
    linked = tmp_path / "linked"
    linked.symlink_to(inputs.root, target_is_directory=True)
    with pytest.raises(OSError):
        build_candidate_release(
            linked / "candidate.sqlite", [prepared], release_id="fictional"
        )
    assert not (inputs.root / "candidate.sqlite").exists()


@pytest.mark.parametrize(
    "change",
    [
        "name",
        "name_language",
        "attributes",
        "observation",
        "delete_name",
        "delete_observation",
        "source_hash",
        "selection",
        "permission_evidence",
        "evidence_hash",
        "receipt",
        "manifest_claim",
        "manifest_digest",
        "extra_table",
        "extra_trigger",
        "schema_version",
        "application_id",
    ],
)
def test_load_reconstructs_rows_and_rejects_corruption_without_an_external_pin(
    inputs, tmp_path, change
):
    path = tmp_path / "candidate.sqlite"
    build_candidate_release(path, [inputs.prepare()], release_id="fictional")
    path.chmod(0o600)
    with sqlite3.connect(path) as connection:
        statements = {
            "name": "UPDATE candidate_names SET value = 'Velvet Compass'",
            "name_language": "UPDATE candidate_names SET language = 'ar'",
            "attributes": "UPDATE candidate_names SET attributes_json = x'5b5d20'",
            "observation": "UPDATE candidate_observations SET primary_name = 'Velvet Compass'",
            "delete_name": "DELETE FROM candidate_names",
            "delete_observation": "DELETE FROM candidate_observations",
            "source_hash": "UPDATE candidate_sources SET selection_sha256 = 'bad'",
            "selection": "UPDATE candidate_sources SET selection_bytes = x'7b7d0a'",
            "permission_evidence": "UPDATE candidate_permission_evidence SET payload = x'6368616e676564'",
            "evidence_hash": "UPDATE candidate_permission_evidence SET sha256 = 'bad'",
            "receipt": "UPDATE candidate_sources SET receipt_bytes = x'7b7d'",
            "manifest_digest": "UPDATE candidate_manifest SET logical_sha256 = '0000000000000000000000000000000000000000000000000000000000000000'",
            "extra_table": "CREATE TABLE hidden_labels (label TEXT)",
            "extra_trigger": "CREATE TRIGGER hidden_trigger AFTER INSERT ON candidate_names BEGIN DELETE FROM candidate_names; END",
            "schema_version": "PRAGMA user_version = 99",
            "application_id": "PRAGMA application_id = 1347896115",
        }
        if change == "manifest_claim":
            manifest = json.loads(
                connection.execute(
                    "SELECT manifest_json FROM candidate_manifest"
                ).fetchone()[0]
            )
            manifest["reviewed_corpus_accepted"] = True
            payload = json.dumps(
                manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
            connection.execute(
                "UPDATE candidate_manifest SET manifest_json = ?, logical_sha256 = ?",
                (payload, digest(payload)),
            )
        else:
            connection.execute(statements[change])
    before = path.read_bytes()
    with pytest.raises(CorpusValidationError):
        load_candidate_release(path)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*-journal")) and not list(tmp_path.glob("*-wal"))


def test_coordinated_name_and_display_name_change_still_fails_source_reconstruction(
    inputs, tmp_path
):
    path = tmp_path / "candidate.sqlite"
    build_candidate_release(path, [inputs.prepare()], release_id="fictional")
    path.chmod(0o600)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE candidate_names SET value = 'Copper Kite'")
        connection.execute(
            "UPDATE candidate_observations SET primary_name = 'Copper Kite'"
        )
    with pytest.raises(CorpusValidationError, match="observations differ"):
        load_candidate_release(path)


def test_loading_is_read_only_self_contained_and_checks_external_digest(
    inputs, tmp_path
):
    path = tmp_path / "candidate.sqlite"
    built = build_candidate_release(path, [inputs.prepare()], release_id="fictional")
    for source_file in inputs.root.iterdir():
        source_file.unlink()
    before = (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ctime_ns)
    assert load_candidate_release(path).logical_sha256 == built.logical_sha256
    assert before == (
        path.read_bytes(),
        path.stat().st_mtime_ns,
        path.stat().st_ctime_ns,
    )
    with pytest.raises(CorpusValidationError, match="trusted digest"):
        load_candidate_release(path, expected_artifact_sha256="0" * 64)
    assert set(item.name for item in tmp_path.iterdir()) == {
        "inputs",
        "candidate.sqlite",
    }
    with pytest.raises(FileNotFoundError):
        load_candidate_release(tmp_path / "missing.sqlite")
    assert not (tmp_path / "missing.sqlite").exists()


@pytest.mark.parametrize(
    "payload", [b"not sqlite", b"SQLite format 3\x00" + b"\x00" * 200]
)
def test_unrelated_and_truncated_images_fail_without_writes(tmp_path, payload):
    path = tmp_path / "unrelated"
    path.write_bytes(payload)
    with pytest.raises(CorpusValidationError):
        load_candidate_release(path)
    assert path.read_bytes() == payload


def test_legacy_database_identity_is_rejected_unchanged(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA application_id = 1347900211")
        connection.execute("CREATE TABLE legacy_evidence (name TEXT)")
        connection.execute(
            "INSERT INTO legacy_evidence VALUES ('Fictional old record')"
        )
    before = path.read_bytes()
    with pytest.raises(CorpusValidationError, match="schema"):
        load_candidate_release(path)
    assert path.read_bytes() == before


def test_logical_hash_and_artifact_order_are_reproducible(inputs, tmp_path):
    a = inputs.prepare()
    other = FictionalInputs(
        tmp_path / "other",
        [fictional_record("ofac_sdn", name="Velvet Compass")],
        "ofac_sdn",
    ).prepare()
    first = build_candidate_release(
        tmp_path / "first.sqlite", [a, other], release_id="fictional"
    )
    second = build_candidate_release(
        tmp_path / "second.sqlite", [other, a], release_id="fictional"
    )
    assert first.logical_sha256 == second.logical_sha256
    assert first.artifact_sha256 == second.artifact_sha256
    with pytest.raises(CorpusValidationError, match="unique source"):
        build_candidate_release(
            tmp_path / "duplicate.sqlite", [a, a], release_id="fictional"
        )


def test_empty_selection_is_honest_candidate_evidence_not_a_corpus(tmp_path):
    source = FictionalInputs(tmp_path / "empty", []).prepare()
    path = tmp_path / "candidate.sqlite"
    build_candidate_release(path, [source], release_id="empty-candidate")
    summary = audit_candidate_release(path)
    assert summary["source_observations"] == summary["name_evidence"] == 0
    assert not summary["reviewed_corpus_accepted"]


def test_byte_limits_and_nonfinite_metadata_fail_before_output(
    inputs, tmp_path, monkeypatch
):
    kwargs = inputs.save()
    monkeypatch.setattr(corpus, "MAX_SELECTION_BYTES", 4)
    with pytest.raises(CorpusValidationError, match="bounded"):
        prepare_source(
            inputs.selection_path, inputs.receipt_path, inputs.permission_path, **kwargs
        )
    monkeypatch.undo()
    inputs.permission_path.write_bytes(
        inputs.permission_path.read_bytes().replace(
            b'"schema_version": 1', b'"schema_version": 1e999'
        )
    )
    kwargs["expected_permission_sha256"] = digest(inputs.permission_path.read_bytes())
    with pytest.raises(CorpusValidationError, match="nonfinite"):
        prepare_source(
            inputs.selection_path, inputs.receipt_path, inputs.permission_path, **kwargs
        )
    assert not list(tmp_path.glob("*.sqlite"))


def test_json_duplicate_permission_keys_are_not_overwritten(inputs):
    kwargs = inputs.save()
    payload = inputs.permission_path.read_bytes().replace(
        b'"status": "approved"', b'"status":"blocked","status":"approved"'
    )
    inputs.permission_path.write_bytes(payload)
    kwargs["expected_permission_sha256"] = digest(payload)
    with pytest.raises(CorpusValidationError, match="duplicate JSON"):
        prepare_source(
            inputs.selection_path, inputs.receipt_path, inputs.permission_path, **kwargs
        )


def fictional_zip_inputs(tmp_path):
    inputs = FictionalInputs(
        tmp_path / "ofac-zip", [fictional_record("ofac_sdn")], "ofac_sdn"
    )
    decoded_hash = digest(b"fictional decoded xml")
    snapshot = {
        "version": "fictional-2026-10-25",
        "version_field": "fictional.publisher.date",
        "resource_url": inputs.receipt["requested_url"],
        "resource_bytes": 15,
        "published_checksum": None,
        "checksum_comparison": None,
        "publisher_archive_checksum_available": False,
        "decoded_bytes": 21,
        "decoded_sha256": decoded_hash,
        "decoded_hash_field": "fictional.xml.sha256",
        "published_object_path": "fictional/SDN_XML.ZIP",
    }
    inputs.receipt.update(
        {
            "container_format": "zip",
            "container_member": "SDN.XML",
            "decoded_bytes": 21,
            "decoded_sha256": decoded_hash,
            "publisher_snapshot": deepcopy(snapshot),
            "publisher_checksum_verified": None,
            "publisher_decoded_checksum_verified": True,
        }
    )
    inputs.receipt["permission_evidence"][0]["publisher_snapshot"] = deepcopy(snapshot)
    return inputs


def test_ofac_decoded_checksum_evidence_is_retained_and_bound(tmp_path):
    inputs = fictional_zip_inputs(tmp_path)
    source = inputs.prepare()
    stored = json.loads(source.receipt_bytes)
    assert stored["publisher_decoded_checksum_verified"] is True
    assert stored["decoded_sha256"] == digest(b"fictional decoded xml")
    assert stored["publisher_snapshot"]["publisher_archive_checksum_available"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "member",
        "decoded_bytes",
        "decoded_hash",
        "missing_decoded_hash",
        "unverified",
        "unbacked_snapshot",
        "snapshot_bytes",
        "partial_snapshot",
        "false_zip_checksum",
        "version_mismatch",
        "false_body_checksum",
    ],
)
def test_inconsistent_container_or_publisher_evidence_is_rejected(tmp_path, mutation):
    inputs = fictional_zip_inputs(tmp_path)
    if mutation == "member":
        inputs.receipt["container_member"] = "../SDN.XML"
    elif mutation == "decoded_bytes":
        inputs.receipt["decoded_bytes"] += 1
    elif mutation == "decoded_hash":
        inputs.receipt["decoded_sha256"] = "0" * 64
    elif mutation == "missing_decoded_hash":
        inputs.receipt["decoded_sha256"] = None
    elif mutation == "unverified":
        inputs.receipt["publisher_decoded_checksum_verified"] = False
    elif mutation == "unbacked_snapshot":
        del inputs.receipt["permission_evidence"][0]["publisher_snapshot"]
    elif mutation == "snapshot_bytes":
        inputs.receipt["publisher_snapshot"]["resource_bytes"] = 14
    elif mutation == "partial_snapshot":
        del inputs.receipt["publisher_snapshot"]["decoded_hash_field"]
    elif mutation == "false_zip_checksum":
        inputs.receipt["publisher_checksum_verified"] = True
    elif mutation == "version_mismatch":
        inputs.receipt["source_version_field"] = "fictional.publisher.date"
        inputs.receipt["source_version"] = "different"
    elif mutation == "false_body_checksum":
        inputs.receipt["publisher_snapshot"]["published_checksum"] = "0" * 64
        inputs.receipt["publisher_snapshot"]["checksum_comparison"] = "sha256"
        inputs.receipt["publisher_checksum_verified"] = True
    if mutation != "unbacked_snapshot":
        inputs.receipt["permission_evidence"][0]["publisher_snapshot"] = deepcopy(
            inputs.receipt["publisher_snapshot"]
        )
    with pytest.raises(CorpusValidationError):
        inputs.prepare()


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "response_version_evidence",
            {
                "secret": "not allowed",  # pragma: allowlist secret -- fictional rejected metadata
            },
        ),
        ("finished_at", "unknown"),
        ("requested_url", "file:///tmp/source"),
        (
            "final_url",
            "https://user:password@example.invalid/private",  # pragma: allowlist secret -- fictional rejected credentials
        ),
        (
            "final_url_evidence",
            {
                "url_sha256": "0" * 64,
                "query_present": False,
                "signing_query_redacted": False,
            },
        ),
    ],
)
def test_optional_receipt_metadata_is_not_an_unchecked_payload(inputs, field, value):
    inputs.receipt[field] = value
    with pytest.raises(CorpusValidationError):
        inputs.prepare()


def test_file_change_during_read_is_rejected_even_when_content_is_identical(
    inputs, monkeypatch
):
    kwargs = inputs.save()
    original = inputs.selection_path.read_bytes()
    # Same-byte writes can share one filesystem timestamp tick. Backdate the
    # fixture so the injected rewrite always changes observable metadata.
    before = inputs.selection_path.stat()
    os.utime(inputs.selection_path, ns=(before.st_atime_ns, 0))
    identity = inputs.selection_path.stat().st_ino
    real_read = os.read
    modified = False

    def replacing_read(descriptor, count):
        nonlocal modified
        chunk = real_read(descriptor, count)
        if not modified and os.fstat(descriptor).st_ino == identity:
            modified = True
            inputs.selection_path.write_bytes(original)
        return chunk

    monkeypatch.setattr(corpus.os, "read", replacing_read)
    with pytest.raises(CorpusValidationError, match="changed while being read"):
        prepare_source(
            inputs.selection_path, inputs.receipt_path, inputs.permission_path, **kwargs
        )
    assert modified
    assert inputs.selection_path.read_bytes() == original


def test_after_link_sync_failure_can_leave_only_a_fully_validated_artifact(
    inputs, tmp_path, monkeypatch
):
    prepared = inputs.prepare()
    path = tmp_path / "candidate.sqlite"
    real_sync = os.fsync

    def failing_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("fictional directory sync failure")
        return real_sync(descriptor)

    monkeypatch.setattr(corpus.os, "fsync", failing_directory_sync)
    with pytest.raises(OSError, match="fictional directory"):
        build_candidate_release(path, [prepared], release_id="fictional")
    assert path.exists()
    assert load_candidate_release(path).summary()["status"] == "prepared_candidate"
    assert not list(tmp_path.glob(".candidate-*.tmp"))


def test_summary_cannot_promote_a_replaced_manifest(inputs, tmp_path):
    built = build_candidate_release(
        tmp_path / "candidate.sqlite", [inputs.prepare()], release_id="fictional"
    )
    changed = built.summary()
    changed["reviewed_corpus_accepted"] = True
    payload = encoded(changed)
    with pytest.raises(CorpusValidationError, match="summary"):
        replace(built, manifest_bytes=payload, logical_sha256=digest(payload)).summary()


def test_appended_bytes_and_damaged_sqlite_header_are_rejected(inputs, tmp_path):
    path = tmp_path / "candidate.sqlite"
    build_candidate_release(path, [inputs.prepare()], release_id="fictional")
    raw = path.read_bytes()
    path.chmod(0o600)
    for invalid in (raw + b"hidden data", raw[:-1], raw[:18] + b"\x02\x02" + raw[20:]):
        path.write_bytes(invalid)
        with pytest.raises(CorpusValidationError, match="SQLite image"):
            load_candidate_release(path)


@pytest.fixture
def fictional_full_inputs(tmp_path):
    from watchlist.ingest.full_acquire import (
        FictionalReply,
        FictionalTransport,
        FullSourcePolicy,
        acquire_fictional,
        prepare_full_profile,
    )
    from watchlist.ingest.resource_guard import (
        BudgetLedger,
        GIB,
        ResourceGuard,
        ResourceSnapshot,
    )

    source_id = "opensanctions_un"
    policy = FullSourcePolicy(
        source_id,
        "https://data.opensanctions.org/artifacts/un_sc_sanctions/20261025000000-fictional/entities.ftm.json",
    )
    payload = b"".join(
        encoded(record)
        for record in [
            {
                "id": "fictional-a",
                "schema": "Organization",
                "properties": {
                    "name": ["Fictional Lumen Orchard"],
                    "alias": ["Fictional Silver Lantern"],
                    "topics": ["sanction"],
                },
            },
            {
                "id": "fictional-b",
                "schema": "Organization",
                "properties": {
                    "name": ["Fictional Velvet Compass"],
                    "alias": ["Fictional Silver Lantern"],
                    "topics": ["sanction"],
                },
            },
            {
                "id": "fictional-excluded",
                "schema": "Person",
                "properties": {
                    "name": ["Invented Excluded Person"],
                    "topics": ["sanction"],
                },
            },
        ]
    )
    output = tmp_path / "fictional-full"
    result = acquire_fictional(
        prepare_full_profile((policy,)),
        output,
        transport=FictionalTransport({policy.url: (FictionalReply(payload),)}),
        ledger=BudgetLedger(),
        guard=ResourceGuard(
            probe=lambda: ResourceSnapshot(30_000_000, 20 * GIB, 0, 0.5, False)
        ),
    )
    assert result["status"] == "passed"
    selection_path = output / f"{source_id}.jsonl"
    receipt_path = output / "receipt.json"
    records = [json.loads(line) for line in selection_path.read_bytes().splitlines()]
    fixture = FictionalInputs(tmp_path / "fixture-purpose", records)
    fixture.permission["receipt_sha256"] = digest(receipt_path.read_bytes())
    fixture.permission["selection_sha256"] = digest(selection_path.read_bytes())
    fixture.permission_path.write_bytes(encoded(fixture.permission))
    kwargs = {
        "expected_receipt_sha256": digest(receipt_path.read_bytes()),
        "expected_permission_sha256": digest(fixture.permission_path.read_bytes()),
        "evidence_root": fixture.root,
    }
    return selection_path, receipt_path, fixture, kwargs


def test_fictional_full_acquisition_to_candidate_to_unfilled_review_packet(
    fictional_full_inputs, tmp_path
):
    from watchlist.ingest.link_records import LinkName, LinkRecord, nominate_links
    from watchlist.label_review import (
        ReviewCase,
        ReviewEvidence,
        export_review_packet,
        freeze_review_packet,
        load_review_packet,
        review_summary,
    )

    selection_path, receipt_path, fixture, kwargs = fictional_full_inputs
    # The legacy admission path retains its complete acquisition-grant contract.
    with pytest.raises(CorpusValidationError, match="receipt fields"):
        prepare_source(selection_path, receipt_path, fixture.permission_path, **kwargs)
    prepared = corpus.prepare_fictional_full_source(
        selection_path, receipt_path, fixture.permission_path, **kwargs
    )
    assert prepared.receipt_bytes == receipt_path.read_bytes()
    assert prepared.receipt_format == "fictional-full-acquisition-v1"
    release = build_candidate_release(
        tmp_path / "fictional-candidate.sqlite",
        [prepared],
        release_id="fictional-full-workflow",
    )
    loaded = load_candidate_release(
        tmp_path / "fictional-candidate.sqlite",
        expected_artifact_sha256=release.artifact_sha256,
    )
    assert loaded == release
    assert (
        release.summary()["sources"][0]["selection_scope"]
        == "fictional_development_only"
    )
    assert len(release.observations) == 2
    records = tuple(
        LinkRecord(
            observation_id=item.observation_id,
            source_record_key=item.source_record_key,
            snapshot_id=item.snapshot_id,
            aliases=tuple(
                LinkName(alias.evidence_id, alias.value) for alias in item.aliases
            ),
            exposure_events=("development_only",),
        )
        for item in release.observations
    )
    nominations = nominate_links(records, candidate_release_hash=release.logical_sha256)
    assert len(nominations) == 1 and nominations[0].method == "exact_name"
    nominated = nominations[0]
    evidence = tuple(
        ReviewEvidence(
            alias.evidence_id,
            item.observation_id,
            item.source_record_key,
            item.snapshot_id,
            alias.value,
            alias.field,
            language=alias.language,
            attributes=alias.attributes,
        )
        for item in release.observations
        for alias in item.aliases
    )
    packet = freeze_review_packet(
        (
            ReviewCase(
                nominated.candidate_id,
                "link_candidate",
                (nominated.left, nominated.right),
                evidence,
                ("fictional_latin",),
            ),
        ),
        candidate_release_hash=release.logical_sha256,
        sampling_rule="authored integration fixture",
    )
    export_review_packet(packet, tmp_path / "fictional-review-packet.json")
    assert load_review_packet(tmp_path / "fictional-review-packet.json") == packet
    summary = review_summary(packet)
    assert summary["prepared_cases"] == 1
    assert summary["human_reviews_performed"] == summary["usable_real_labels"] == 0
    assert not summary["real_review_minimum_met"]
    assert release.summary()["accepted_independent_families"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "live",
        "custody",
        "status",
        "scope",
        "selection_scope",
        "resume",
        "selection_hash",
        "selection_bytes",
        "input_bytes",
        "container",
        "counts",
        "retained_count",
        "duplicate_source",
        "active_source",
        "active_request",
        "stop_reason",
        "ledger_total",
        "ledger_source_bytes",
        "ledger_transactions",
    ],
)
def test_fictional_full_receipts_fail_closed(fictional_full_inputs, mutation):
    selection_path, receipt_path, fixture, kwargs = fictional_full_inputs
    receipt = json.loads(receipt_path.read_bytes())
    if mutation in {"live", "custody", "resume"}:
        receipt[
            {
                "live": "live_authorized",
                "custody": "independent_custody_verified",
                "resume": "resume_supported",
            }[mutation]
        ] = True
    elif mutation == "status":
        receipt["status"] = "failed"
    elif mutation == "scope":
        receipt["scope"] = "live"
    elif mutation == "selection_scope":
        receipt["selection_use"] = "fresh_final"
    elif mutation == "selection_hash":
        receipt["sources"][0]["selection_sha256"] = "0" * 64
    elif mutation == "selection_bytes":
        receipt["sources"][0]["selection_bytes"] += 1
    elif mutation == "input_bytes":
        receipt["sources"][0]["input_bytes"] += 1
    elif mutation == "container":
        receipt["sources"][0]["container_format"] = "zip"
    elif mutation == "counts":
        receipt["sources"][0]["input_records"] += 1
    elif mutation == "retained_count":
        receipt["sources"][0]["retained_groups"] += 1
        receipt["sources"][0]["input_records"] += 1
    elif mutation == "duplicate_source":
        receipt["sources"] *= 2
    elif mutation in {"active_source", "active_request", "stop_reason"}:
        receipt["ledger"][mutation] = "unfinished"
    elif mutation == "ledger_total":
        receipt["ledger"]["total_received_bytes"] += 1
    elif mutation == "ledger_source_bytes":
        receipt["ledger"]["received_bytes"]["opensanctions_un"] = 0
        receipt["ledger"]["total_received_bytes"] = 0
    elif mutation == "ledger_transactions":
        receipt["ledger"]["transactions"]["opensanctions_un"] = 0
    receipt_path.write_bytes(encoded(receipt))
    kwargs["expected_receipt_sha256"] = digest(receipt_path.read_bytes())
    fixture.permission["receipt_sha256"] = kwargs["expected_receipt_sha256"]
    fixture.permission_path.write_bytes(encoded(fixture.permission))
    kwargs["expected_permission_sha256"] = digest(fixture.permission_path.read_bytes())
    with pytest.raises(CorpusValidationError):
        corpus.prepare_fictional_full_source(
            selection_path, receipt_path, fixture.permission_path, **kwargs
        )


def test_exact_v1_candidate_remains_readable_without_rewrite(inputs, tmp_path):
    # This digest pins the SQL bytes frozen by the approved 08B-0 implementation.
    assert (
        digest(corpus._schema(1).encode())
        == "0238b04682d4352b241dd57db7643e57094e4366f5f6e45b943297cd29102710"
    )
    source = inputs.prepare()
    manifest = corpus._manifest("historical-fictional-v1", (source,), schema_version=1)
    path = tmp_path / "historical-v1.sqlite"
    path.write_bytes(corpus._database_bytes(manifest, (source,), schema_version=1))
    before = path.read_bytes()
    loaded = load_candidate_release(path)
    assert loaded.summary()["schema_version"] == 1
    assert loaded.observations == source.observations
    assert path.read_bytes() == before


class RealFullInputs:
    """Authored names and grants shaped like real receipts; never a dataset GET."""

    def __init__(self, root):
        from watchlist.ingest.resource_guard import (
            BudgetLedger,
            BULK_SOURCES,
            STEP08_LIMITS,
        )

        self.root = root.resolve()
        self.root.mkdir()
        self.approval = {
            "path": "approval.json",
            "sha256": digest(encoded({"fixture": "fictional authorization only"})),
        }
        (self.root / "approval.json").write_bytes(
            encoded({"fixture": "fictional authorization only"})
        )
        terms = b"Authored fictional terms; not a grant from any real publisher."
        (self.root / "terms.txt").write_bytes(terms)
        self.grants = {}
        self.policy = {
            "schema_version": 1,
            "scope": "08B-2-three-source-candidate-acquisition",
            "approval": self.approval,
            "limits": {
                "max_attempts": 1,
                "max_redirects": 0,
                "session_seconds": 3600,
                "total_bytes": 39_000_000,
                "transactions": 3,
            },
            "session_claim_path": "fictional-claim.json",
            "sources": [],
        }
        self.receipt = {
            "schema_version": 1,
            "scope": "08B-2-development-candidate-acquisition-v1",
            "policy_sha256": "filled",
            "execution_policy_path": "execution-policy.json",
            "execution_policy_sha256": "filled",
            "approval": self.approval,
            "permission_grants": [],
            "status": "passed",
            "live_authorized": True,
            "independent_custody_verified": False,
            "selection_use": "shared-development-candidates-only",
            "resume_supported": False,
            "sources": [],
            "total_body_limit": 39_000_000,
            "started_at": "2026-10-25T10:00:00+00:00",
            "finished_at": "2026-10-25T10:00:01+00:00",
            "peak_sampled_rss_bytes": 30_000_000,
            "rss_enforcement": "fictional resource observation only",
        }
        ledger = BudgetLedger(
            total_received_limit=39_000_000,
            bulk_transaction_limit=3,
            allowed_buckets=BULK_SOURCES,
        )
        urls = {
            "opensanctions_un": "https://data.opensanctions.org/artifacts/un_sc_sanctions/20261025000000-fictional/entities.ftm.json",
            "ofac_sdn": "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_XML.ZIP",
            "eu_consolidated": "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content",
        }
        for index, source_id in enumerate(
            ("opensanctions_un", "ofac_sdn", "eu_consolidated")
        ):
            record = fictional_record(source_id, name=f"Fictional Moon Orchard {index}")
            base = FictionalInputs(self.root / source_id, [record], source_id)
            selection = encoded(record)
            (self.root / f"{source_id}.jsonl").write_bytes(selection)
            grant = {
                key: value
                for key, value in base.permission.items()
                if key not in {"receipt_sha256", "selection_sha256"}
            }
            grant.update(
                {
                    "grant_kind": "prospective_source_purpose",
                    "review_id": f"fictional-purpose-{source_id}",
                    "exact_url": urls[source_id],
                    "license": "fictional authored fixture only",
                    "source_role": "sanctions_reference",
                    "approval": self.approval,
                    "development_exposed_only": True,
                    "final_eligible": False,
                    "raw_retention_allowed": False,
                    "transient_full_response_allowed": True,
                    "evidence": [
                        self.approval,
                        {"path": "terms.txt", "sha256": digest(terms)},
                    ],
                    "prospective_binding": {
                        "same_policy": True,
                        "require_complete_direct_200": True,
                        "bind_receipt_sha256": True,
                        "bind_selection_sha256": True,
                        "one_derived_grant_per_source": True,
                        "preserve_review_metadata": True,
                        "new_human_review_asserted": False,
                    },
                }
            )
            self.grants[source_id] = grant
            body = f"fictional source body {index}".encode()
            decoded = b"fictional OFAC XML fixture" if source_id == "ofac_sdn" else None
            source_limit = STEP08_LIMITS[source_id]
            pins = {
                "expected_sha256": None,
                "expected_sha1": hashlib.sha1(body).hexdigest()
                if source_id == "opensanctions_un"
                else None,
                "expected_bytes": len(body) if source_id != "eu_consolidated" else None,
                "expected_decoded_sha256": digest(decoded)
                if decoded is not None
                else None,
                "verified": source_id != "eu_consolidated",
                "hash_verified": source_id != "eu_consolidated",
            }
            self.policy["sources"].append(
                {
                    "source_id": source_id,
                    "url": urls[source_id],
                    "allowed_redirects": [],
                    **{
                        key: value
                        for key, value in pins.items()
                        if key.startswith("expected_")
                    },
                    "max_groups": {
                        "opensanctions_un": 300,
                        "ofac_sdn": 12000,
                        "eu_consolidated": 2000,
                    }[source_id],
                    "received_bytes": source_limit.received_bytes,
                    "request_seconds": source_limit.request_seconds,
                    "source_seconds": source_limit.source_seconds,
                    "decoded_bytes": 32_000_000 if decoded else None,
                    "permission_path": f"{source_id}.grant.json",
                    "permission_sha256": "filled",
                }
            )
            self.receipt["sources"].append(
                {
                    "source_id": source_id,
                    "input_sha256": digest(body),
                    "input_bytes": len(body),
                    "body_sha256": digest(body),
                    "bytes_received": len(body),
                    "input_records": 2,
                    "retained_groups": 1,
                    "excluded_person_records": 1,
                    "excluded_other_records": 0,
                    "excluded_by_limit": 0,
                    "parser_version": "fictional-live-shape-fixture-v1",
                    "source_version": None,
                    "source_version_field": None,
                    "version_fallback": "input_sha256",
                    "container_format": "zip" if decoded else None,
                    "container_member": "SDN.XML" if decoded else None,
                    "decoded_bytes": len(decoded) if decoded else None,
                    "decoded_sha256": digest(decoded) if decoded else None,
                    "selection_sha256": digest(selection),
                    "selection_bytes": len(selection),
                    "requested_url": urls[source_id],
                    "final_url": urls[source_id],
                    "status": "passed",
                    "complete": True,
                    "http_status": 200,
                    "selection_path": f"{source_id}.jsonl",
                    "transaction_count": 1,
                    "permission": {},
                    "publisher_checksum": pins,
                }
            )
            ledger.begin_source(source_id)
            ticket = ledger.begin_request(source_id)
            ledger.charge_received(ticket, len(body))
            ledger.finish_request(ticket)
            ledger.finish_source(source_id)
        self.receipt["ledger"] = ledger.receipt()
        self.save()

    def save(self):
        self.receipt["permission_grants"] = []
        for row in self.policy["sources"]:
            source_id = row["source_id"]
            payload = encoded(self.grants[source_id])
            (self.root / row["permission_path"]).write_bytes(payload)
            row["permission_sha256"] = digest(payload)
            reference = {"path": row["permission_path"], "sha256": digest(payload)}
            self.receipt["permission_grants"].append(
                {"source_id": source_id, **reference}
            )
            for source in self.receipt["sources"]:
                if source["source_id"] == source_id:
                    source["permission"] = reference
        policy_bytes = encoded(self.policy)
        (self.root / "execution-policy.json").write_bytes(policy_bytes)
        self.receipt["policy_sha256"] = self.receipt["execution_policy_sha256"] = (
            digest(policy_bytes)
        )
        references = {"execution-policy.json": digest(policy_bytes)}
        for source_id, grant in self.grants.items():
            references[f"{source_id}.grant.json"] = digest(encoded(grant))
            for item in grant["evidence"]:
                references[item["path"]] = item["sha256"]
        self.receipt["evidence"] = [
            {"path": path, "sha256": value}
            for path, value in sorted(references.items())
        ]
        (self.root / "receipt.json").write_bytes(encoded(self.receipt))

    def bind(self, source_id="opensanctions_un"):
        return corpus.bind_real_full_permission(
            self.root / f"{source_id}.jsonl",
            self.root / "receipt.json",
            source_id=source_id,
            expected_receipt_sha256=digest((self.root / "receipt.json").read_bytes()),
            expected_grant_sha256=digest(
                (self.root / f"{source_id}.grant.json").read_bytes()
            ),
            evidence_root=self.root,
        )

    def prepare(self, source_id="opensanctions_un"):
        permission = self.bind(source_id)
        permission_path = self.root / f"{source_id}.purpose.json"
        permission_path.write_bytes(encoded(permission))
        return corpus.prepare_real_full_source(
            self.root / f"{source_id}.jsonl",
            self.root / "receipt.json",
            permission_path,
            expected_receipt_sha256=digest((self.root / "receipt.json").read_bytes()),
            expected_permission_sha256=digest(permission_path.read_bytes()),
            evidence_root=self.root,
        )


@pytest.fixture
def real_full_inputs(tmp_path):
    return RealFullInputs(tmp_path / "authored-real-receipt-shape")


def test_real_full_binding_preserves_grant_review_and_development_only_status(
    real_full_inputs, tmp_path
):
    fixture = real_full_inputs
    bound = fixture.bind()
    grant = fixture.grants["opensanctions_un"]
    for field in ("review_id", "reviewer", "reviewed_at", "conditions", "name_fields"):
        assert bound[field] == grant[field]
    assert {item["path"] for item in bound["evidence"]} == {
        "approval.json",
        "terms.txt",
        "execution-policy.json",
        "opensanctions_un.grant.json",
    }
    assert not list(
        fixture.root.glob("*.purpose.json")
    )  # Binding helper does not write.
    prepared = tuple(fixture.prepare(source) for source in fixture.grants)
    result = build_candidate_release(
        tmp_path / "fictional-live-shape.sqlite",
        prepared,
        release_id="fictional-live-shape",
    )
    loaded = load_candidate_release(tmp_path / "fictional-live-shape.sqlite")
    assert loaded == result
    summary = loaded.summary()
    assert summary["schema_version"] == 2 and summary["source_observations"] == 3
    assert all(
        item["selection_scope"] == "shared_development_candidates_only"
        for item in summary["sources"]
    )
    assert (
        not summary["training_eligible"]
        and not summary["evaluation_eligible"]
        and not summary["fresh_final_eligible"]
    )
    assert (
        summary["human_review_count"] == summary["accepted_independent_families"] == 0
    )


def test_acquisition_written_fictional_receipt_admits_all_three_sources(
    tmp_path, monkeypatch
):
    """Exercise the live-shaped writer with invented responses and no sockets."""
    import socket

    from test_full_acquisition import live_fixture, run_live_fixture

    def denied(*args, **kwargs):
        raise AssertionError("Corpus integration test must never use the network")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    policy_file, data, transport, _ = live_fixture(tmp_path)
    receipt = run_live_fixture(tmp_path, policy_file, data, transport)
    assert receipt["status"] == "passed"
    output = tmp_path / "live-run"
    receipt_path = output / "receipt.json"
    receipt_hash = digest(receipt_path.read_bytes())
    prepared = []
    for row in receipt["sources"]:
        source_id = row["source_id"]
        selection_path = output / row["selection_path"]
        permission_path = output / f"{source_id}.purpose.json"
        bound = corpus.bind_real_full_permission(
            selection_path,
            receipt_path,
            source_id=source_id,
            expected_receipt_sha256=receipt_hash,
            expected_grant_sha256=row["permission"]["sha256"],
            evidence_root=tmp_path,
        )
        assert bound == json.loads(permission_path.read_bytes())
        prepared.append(
            corpus.prepare_real_full_source(
                selection_path,
                receipt_path,
                permission_path,
                expected_receipt_sha256=receipt_hash,
                expected_permission_sha256=digest(permission_path.read_bytes()),
                evidence_root=tmp_path,
            )
        )
    path = tmp_path / "fictional-integration.sqlite"
    release = build_candidate_release(
        path, prepared, release_id="fictional-integration"
    )
    assert load_candidate_release(path) == release
    summary = release.summary()
    assert summary["schema_version"] == 2 and summary["source_observations"] == 3
    assert (
        summary["human_review_count"] == summary["accepted_independent_families"] == 0
    )
    assert not any(
        summary[field]
        for field in (
            "training_eligible",
            "evaluation_eligible",
            "fresh_final_eligible",
        )
    )


@pytest.mark.parametrize(
    "mutation", ["missing_policy", "missing_grant", "duplicate", "changed_other_grant"]
)
def test_real_receipt_evidence_is_complete_and_hash_bound(real_full_inputs, mutation):
    fixture = real_full_inputs
    references = fixture.receipt["evidence"]
    if mutation in {"missing_policy", "missing_grant"}:
        path = (
            "execution-policy.json"
            if mutation == "missing_policy"
            else "ofac_sdn.grant.json"
        )
        fixture.receipt["evidence"] = [
            item for item in references if item["path"] != path
        ]
    elif mutation == "duplicate":
        references.append(references[0])
    else:
        (fixture.root / "ofac_sdn.grant.json").write_bytes(b"changed fictional grant")
    (fixture.root / "receipt.json").write_bytes(encoded(fixture.receipt))
    with pytest.raises(CorpusValidationError):
        fixture.bind()
    assert not list(fixture.root.glob("*.purpose.json"))


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "missing_source",
        "incomplete",
        "redirect",
        "http206",
        "two_transactions",
        "body_bytes",
        "body_hash",
        "selection_hash",
        "missing_fallback",
        "publisher_pin",
        "publisher_verified",
        "ledger59mb",
        "ledger9requests",
        "metadata_bucket",
        "unfinished",
        "missing_permission",
        "wrong_purpose",
        "final_eligible",
        "new_review",
        "changed_route",
        "expanded_policy",
        "missing_approval_evidence",
        "changed_terms",
    ],
)
def test_real_full_partial_tampered_or_unpermitted_inputs_fail_without_binding(
    real_full_inputs, mutation
):
    fixture = real_full_inputs
    receipt = fixture.receipt
    row = receipt["sources"][0]
    grant = fixture.grants["opensanctions_un"]
    if mutation == "partial":
        receipt["status"] = "partial"
    elif mutation == "missing_source":
        receipt["sources"].pop()
    elif mutation == "incomplete":
        row["complete"] = False
    elif mutation == "redirect":
        row["final_url"] += "?redirected=1"
    elif mutation == "http206":
        row["http_status"] = 206
    elif mutation == "two_transactions":
        row["transaction_count"] = 2
    elif mutation == "body_bytes":
        row["input_bytes"] -= 1
    elif mutation == "body_hash":
        row["body_sha256"] = "0" * 64
    elif mutation == "selection_hash":
        row["selection_sha256"] = "0" * 64
    elif mutation == "missing_fallback":
        row["version_fallback"] = None
    elif mutation == "publisher_pin":
        row["publisher_checksum"]["expected_sha1"] = "0" * 40
    elif mutation == "publisher_verified":
        row["publisher_checksum"]["hash_verified"] = False
    elif mutation == "ledger59mb":
        receipt["ledger"]["tranche_bytes"] = 59_000_000
    elif mutation == "ledger9requests":
        receipt["ledger"]["bulk_transaction_limit"] = 9
    elif mutation == "metadata_bucket":
        receipt["ledger"]["allowed_buckets"].append("metadata")
    elif mutation == "unfinished":
        receipt["ledger"]["active_source"] = "eu_consolidated"
    elif mutation == "missing_permission":
        grant["status"] = "pending"
    elif mutation == "wrong_purpose":
        grant["purpose"] = "feasibility_download_only"
    elif mutation == "final_eligible":
        grant["final_eligible"] = True
    elif mutation == "new_review":
        grant["prospective_binding"]["new_human_review_asserted"] = True
    elif mutation == "changed_route":
        fixture.policy["sources"][0]["url"] += "?different=1"
    elif mutation == "expanded_policy":
        fixture.policy["limits"]["transactions"] = 9
    elif mutation == "missing_approval_evidence":
        grant["evidence"] = [
            item for item in grant["evidence"] if item["path"] != "approval.json"
        ]
    elif mutation == "changed_terms":
        (fixture.root / "terms.txt").write_bytes(b"changed fictional evidence")
    fixture.save()
    with pytest.raises(CorpusValidationError):
        fixture.bind()
    assert not list(fixture.root.glob("*.purpose.json"))
    assert not list(fixture.root.glob("*.sqlite"))


def test_bound_real_permission_cannot_expand_fields_or_claim_another_review(
    real_full_inputs,
):
    fixture = real_full_inputs
    valid = fixture.bind()
    for field, value in (
        ("reviewer", "different fictional reviewer"),
        ("name_fields", {**valid["name_fields"], "properties.unapproved": []}),
    ):
        changed = deepcopy(valid)
        changed[field] = value
        path = fixture.root / "changed-purpose.json"
        path.write_bytes(encoded(changed))
        with pytest.raises(CorpusValidationError, match="prospective grant"):
            corpus.prepare_real_full_source(
                fixture.root / "opensanctions_un.jsonl",
                fixture.root / "receipt.json",
                path,
                expected_receipt_sha256=digest(
                    (fixture.root / "receipt.json").read_bytes()
                ),
                expected_permission_sha256=digest(path.read_bytes()),
                evidence_root=fixture.root,
            )


def test_real_receipt_is_rejected_by_legacy_and_fictional_entry_points(
    real_full_inputs,
):
    fixture = real_full_inputs
    permission_path = fixture.root / "purpose.json"
    permission_path.write_bytes(encoded(fixture.bind()))
    for function in (prepare_source, corpus.prepare_fictional_full_source):
        with pytest.raises(CorpusValidationError):
            function(
                fixture.root / "opensanctions_un.jsonl",
                fixture.root / "receipt.json",
                permission_path,
                expected_receipt_sha256=digest(
                    (fixture.root / "receipt.json").read_bytes()
                ),
                expected_permission_sha256=digest(permission_path.read_bytes()),
                evidence_root=fixture.root,
            )


class RecoveryInputs:
    """Acquisition-written fictional recovery artifacts, never publisher data."""

    def __init__(self, root):
        from test_full_acquisition import recovery_fixture, run_recovery_fixture

        self.root = root.resolve()
        self.root.mkdir()
        policy_path, policy, transport, _ = recovery_fixture(self.root)
        self.original_paths = tuple((self.root / "live-run").iterdir()) + (
            self.root / "session.claim.json",
        )
        self.before = {path: path.read_bytes() for path in self.original_paths}
        self.receipt = run_recovery_fixture(self.root, policy_path, policy, transport)
        assert self.receipt["status"] == "passed"
        assert len(transport.calls) == 3
        assert not any("opensanctions.org" in url for url, _ in transport.calls)
        self.output = self.root / "recovery-run"
        self.receipt_path = self.output / "receipt.json"

    def bind(self, source_id="opensanctions_un"):
        row = next(
            row for row in self.receipt["sources"] if row["source_id"] == source_id
        )
        return corpus.bind_recovered_full_permission(
            self.output / row["selection_path"],
            self.receipt_path,
            source_id=source_id,
            expected_receipt_sha256=digest(self.receipt_path.read_bytes()),
            expected_grant_sha256=row["permission"]["sha256"],
            evidence_root=self.root,
        )

    def prepare(self, source_id):
        purpose = self.output / f"{source_id}.purpose.json"
        return corpus.prepare_recovered_full_source(
            self.output / f"{source_id}.jsonl",
            self.receipt_path,
            purpose,
            expected_receipt_sha256=digest(self.receipt_path.read_bytes()),
            expected_permission_sha256=digest(purpose.read_bytes()),
            evidence_root=self.root,
        )


@pytest.fixture
def recovered_inputs(tmp_path, monkeypatch):
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("Recovery corpus tests must never use the network")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    return RecoveryInputs(tmp_path / "fictional-recovery")


def test_recovery_writer_admission_release_preserves_interrupted_history(
    recovered_inputs, tmp_path
):
    fixture = recovered_inputs
    sources = []
    for source_id in ("opensanctions_un", "ofac_sdn", "eu_consolidated"):
        assert fixture.bind(source_id) == json.loads(
            (fixture.output / f"{source_id}.purpose.json").read_bytes()
        )
        sources.append(fixture.prepare(source_id))
    path = tmp_path / "recovered-fictional-candidates.sqlite"
    release = build_candidate_release(path, sources, release_id="fictional-recovery")
    assert load_candidate_release(path) == release
    summary = release.summary()
    assert summary["schema_version"] == 2
    assert summary["assembly"]["kind"] == "separately_completed_source_sessions"
    assert summary["assembly"]["historical_source_statuses"] == {
        "opensanctions_un": "passed",
        "ofac_sdn": "failed",
        "eu_consolidated": "running",
    }
    assert summary["assembly"]["original_session_status"] == "interrupted"
    assert summary["assembly"]["original_final_receipt_available"] is False
    assert (
        summary["human_review_count"] == summary["accepted_independent_families"] == 0
    )
    assert not any(
        summary[name]
        for name in ("training_eligible", "evaluation_eligible", "fresh_final_eligible")
    )
    assert {
        path: path.read_bytes() for path in fixture.original_paths
    } == fixture.before
    assert not (fixture.root / "live-run" / "receipt.json").exists()
    assert {
        item["source_id"]: item["acquisition_origin"] for item in summary["sources"]
    } == {
        "opensanctions_un": "reused_completed_source",
        "ofac_sdn": "recovery_session",
        "eu_consolidated": "recovery_session",
    }
    saved = path.read_bytes()
    with pytest.raises(FileExistsError):
        build_candidate_release(path, sources, release_id="fictional-recovery")
    assert path.read_bytes() == saved


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "failed_source",
        "running_source",
        "missing_source",
        "changed_origin",
        "historical_transaction_reset",
        "new_os_request",
        "missing_length",
        "length_mismatch",
        "partial_http",
        "unapproved_redirect",
        "stored_signing_values",
        "uncharged_redirect_body",
        "prior_budget_reset",
        "consumed_claim_reused",
        "wrong_claim_policy",
        "cross_session_journal",
    ],
)
def test_recovery_incomplete_or_cross_session_evidence_is_rejected(
    recovered_inputs, mutation
):
    fixture = recovered_inputs
    receipt = fixture.receipt
    os_row, ofac, eu = receipt["sources"]
    if mutation == "partial":
        receipt["status"] = "partial"
    elif mutation == "failed_source":
        ofac["status"] = "failed"
    elif mutation == "running_source":
        eu["status"] = "running"
    elif mutation == "missing_source":
        receipt["sources"].pop()
    elif mutation == "changed_origin":
        os_row["origin_evidence"]["source_row"]["input_records"] += 1
    elif mutation == "historical_transaction_reset":
        os_row["transaction_count"] = 0
    elif mutation == "new_os_request":
        receipt["ledger"]["transactions"]["opensanctions_un"] = 1
    elif mutation == "missing_length":
        eu["transactions"][0]["declared_body_bytes"] = None
    elif mutation == "length_mismatch":
        eu["transactions"][0]["declared_body_bytes"] += 1
    elif mutation == "partial_http":
        ofac["transactions"][-1]["http_status"] = 206
    elif mutation == "unapproved_redirect":
        ofac["transactions"][0]["redirect_target"] += "/other-object"
    elif mutation == "stored_signing_values":
        ofac["transactions"][-1]["requested_url"] += "?X-Amz-Signature=fictional"
    elif mutation == "uncharged_redirect_body":
        ofac["bytes_received"] = ofac["input_bytes"]
    elif mutation == "prior_budget_reset":
        receipt["prior_session_accounting"]["received_plus_reservation_bytes"] = 0
    elif mutation == "consumed_claim_reused":
        receipt["session_claim"] = receipt["prior_session"]["claim"]
    elif mutation == "wrong_claim_policy":
        reference = receipt["session_claim"]
        path = fixture.root / reference["path"]
        claim = json.loads(path.read_bytes())
        claim["policy_sha256"] = "0" * 64
        path.write_bytes(encoded(claim))
        reference["sha256"] = digest(path.read_bytes())
        for item in receipt["evidence"]:
            if item["path"] == reference["path"]:
                item["sha256"] = reference["sha256"]
    else:
        receipt["prior_session"]["completed_journal"] = receipt["prior_session"][
            "policy"
        ]
    fixture.receipt_path.write_bytes(encoded(receipt))
    with pytest.raises(CorpusValidationError):
        fixture.bind()
    assert not list(fixture.root.glob("*.sqlite"))


def test_recovery_requires_explicit_entry_point_and_one_complete_assembly(
    recovered_inputs, tmp_path
):
    fixture = recovered_inputs
    purpose = fixture.output / "opensanctions_un.purpose.json"
    with pytest.raises(CorpusValidationError, match="explicit admission"):
        corpus.prepare_real_full_source(
            fixture.output / "opensanctions_un.jsonl",
            fixture.receipt_path,
            purpose,
            expected_receipt_sha256=digest(fixture.receipt_path.read_bytes()),
            expected_permission_sha256=digest(purpose.read_bytes()),
            evidence_root=fixture.root,
        )
    sources = [
        fixture.prepare(name)
        for name in ("opensanctions_un", "ofac_sdn", "eu_consolidated")
    ]
    with pytest.raises(CorpusValidationError, match="all three"):
        build_candidate_release(
            tmp_path / "incomplete.sqlite",
            sources[:1],
            release_id="fictional-incomplete",
        )
    second = RecoveryInputs(tmp_path / "separate-fictional-recovery")
    sources[1] = second.prepare("ofac_sdn")
    with pytest.raises(CorpusValidationError, match="one new receipt"):
        build_candidate_release(
            tmp_path / "mixed.sqlite", sources, release_id="fictional-mixed"
        )
    assert not (tmp_path / "incomplete.sqlite").exists()
    assert not (tmp_path / "mixed.sqlite").exists()


class ContinuationInputs:
    """Three fictional selections with actual interrupted/failed session lineage."""

    def __init__(self, root):
        from test_full_acquisition import continuation_fixture, run_continuation_fixture

        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        policy_path, policy, transport, _ = continuation_fixture(self.root)
        self.receipt = run_continuation_fixture(
            self.root, policy_path, policy, transport
        )
        assert self.receipt["status"] == "passed"
        self.output = self.root / "continuation-run"
        self.receipt_path = self.output / "receipt.json"

    def prepare(self, *, checkpoint=None):
        return corpus.prepare_continuation_sources(
            self.receipt_path,
            expected_receipt_sha256=digest(self.receipt_path.read_bytes()),
            evidence_root=self.root,
            checkpoint=checkpoint,
        )


@pytest.fixture
def continuation_inputs(tmp_path, monkeypatch):
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("Continuation corpus tests must never use the network")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    return ContinuationInputs(tmp_path / "fictional-continuation")


def test_continuation_readonly_admission_retains_all_inputs_and_no_release(
    continuation_inputs, tmp_path
):
    fixture = continuation_inputs
    before = {
        path: path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()
    }
    checkpoints = []
    prepared = fixture.prepare(checkpoint=lambda: checkpoints.append(True))
    assert checkpoints
    assert type(prepared) is corpus.ContinuationPreparation
    assert len(prepared.sources) == 3
    assert {source.source_id for source in prepared.sources} == {
        "opensanctions_un",
        "ofac_sdn",
        "eu_consolidated",
    }
    assert len(prepared.observations) == sum(
        row["retained_groups"] for row in fixture.receipt["sources"]
    )
    assert prepared.observations == tuple(
        item for source in prepared.sources for item in source.observations
    )
    manifest = prepared.input_manifest()
    assert manifest["artifact_kind"] == corpus.CONTINUATION_PREPARATION
    assert manifest["receipt_scope"] == corpus.CONTINUATION_SCOPE
    assert manifest["release_authorized"] is False
    assert (
        manifest["human_review_count"] == manifest["accepted_independent_families"] == 0
    )
    assert not any(
        manifest[key]
        for key in ("training_eligible", "evaluation_eligible", "fresh_final_eligible")
    )
    assert (
        manifest["lineage"]["recovery_accounting"][
            "carried_prior_recovery_reservation_bytes"
        ]
        == 4_000_000
    )
    manifest["release_authorized"] = True
    assert prepared.input_manifest()["release_authorized"] is False
    assert prepared == fixture.prepare()
    for source in prepared.sources:
        assert type(source) is corpus.ContinuationSource
        assert (
            source.permission_bytes
            == (fixture.root / source.permission_path).read_bytes()
        )
        assert (
            source.selection_bytes
            == (fixture.root / source.selection_path).read_bytes()
        )
    with pytest.raises(CorpusValidationError, match="count/type"):
        build_candidate_release(
            tmp_path / "forbidden.sqlite", prepared.sources, release_id="forbidden"
        )
    assert not (tmp_path / "forbidden.sqlite").exists()
    assert {
        path: path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "missing_source",
        "duplicate_source",
        "failed_source",
        "incomplete_source",
        "carried_reservation",
        "prior_transactions",
        "cumulative_limit",
        "recovery_ceiling",
        "release_authorized",
        "new_body_cap",
        "ledger_cap",
        "new_os_request",
        "cumulative_bytes",
        "cumulative_transactions",
        "combined_accounting",
        "old_accounting",
        "reused_origin",
        "reused_transactions",
        "reused_source_row",
        "new_source_origin",
        "incomplete_body",
        "missing_length",
        "transaction_sequence",
        "redirect_body_accounting",
        "receipt_selection_hash",
        "selection_path",
        "duplicate_evidence",
        "evidence_path",
        "selection_bytes",
        "evidence_bytes",
        "missing_grant",
        "claim_reused",
        "tls_unverified",
        "tls_extra_field",
        "boolean_counter",
    ],
)
def test_continuation_incomplete_accounting_permissions_and_provenance_fail(
    continuation_inputs, mutation
):
    fixture = continuation_inputs
    receipt = fixture.receipt
    reused, ofac, eu = receipt["sources"]
    if mutation == "partial":
        receipt["status"] = "partial"
    elif mutation == "missing_source":
        receipt["sources"].pop()
    elif mutation == "duplicate_source":
        receipt["sources"][2] = deepcopy(ofac)
    elif mutation == "failed_source":
        eu["status"] = "failed"
    elif mutation == "incomplete_source":
        eu["complete"] = False
    elif mutation in {
        "carried_reservation",
        "prior_transactions",
        "cumulative_limit",
        "recovery_ceiling",
        "release_authorized",
    }:
        key = {
            "carried_reservation": "carried_reservation_bytes",
            "prior_transactions": "prior_charged_transactions",
            "cumulative_limit": "cumulative_transaction_limit",
            "recovery_ceiling": "recovery_ceiling_bytes",
            "release_authorized": "release_authorized",
        }[mutation]
        receipt["continuation"][key] = True if mutation == "release_authorized" else 0
    elif mutation == "new_body_cap":
        receipt["total_body_limit"] = 34_000_000
    elif mutation == "ledger_cap":
        receipt["ledger"]["tranche_bytes"] = 34_000_000
    elif mutation == "new_os_request":
        receipt["ledger"]["transactions"]["opensanctions_un"] = 1
    elif mutation in {
        "cumulative_bytes",
        "cumulative_transactions",
        "combined_accounting",
    }:
        key = {
            "cumulative_bytes": "cumulative_recovery_accounted_bytes",
            "cumulative_transactions": "cumulative_recovery_transactions",
            "combined_accounting": "combined_prior_plus_recovery_reserved_bytes",
        }[mutation]
        receipt["recovery_accounting"][key] -= 1
    elif mutation == "old_accounting":
        receipt["prior_session_accounting"]["received_plus_reservation_bytes"] = 0
    elif mutation == "reused_origin":
        reused["acquisition_origin"] = "recovery_session"
    elif mutation == "reused_transactions":
        reused["transaction_count"] = 0
    elif mutation == "reused_source_row":
        reused["origin_evidence"]["source_row"]["input_records"] += 1
    elif mutation == "new_source_origin":
        ofac["acquisition_origin"] = "reused_completed_source"
    elif mutation == "incomplete_body":
        eu["transactions"][0]["http_status"] = 206
    elif mutation == "missing_length":
        eu["transactions"][0]["declared_body_bytes"] = None
    elif mutation == "transaction_sequence":
        eu["transactions"][0]["sequence"] = 1
    elif mutation == "redirect_body_accounting":
        ofac["transactions"][0]["bytes_received"] += 1
    elif mutation == "receipt_selection_hash":
        eu["selection_sha256"] = "0" * 64
    elif mutation == "selection_path":
        eu["selection_path"] = "../eu_consolidated.jsonl"
    elif mutation == "duplicate_evidence":
        receipt["evidence"].append(deepcopy(receipt["evidence"][0]))
    elif mutation == "evidence_path":
        receipt["evidence"][0]["path"] = "../outside"
    elif mutation == "selection_bytes":
        (fixture.output / "eu_consolidated.jsonl").write_bytes(b"{}\n")
    elif mutation == "evidence_bytes":
        (fixture.root / receipt["evidence"][0]["path"]).write_bytes(b"changed evidence")
    elif mutation == "missing_grant":
        receipt["permission_grants"].pop()
    elif mutation == "claim_reused":
        receipt["session_claim"] = receipt["continuation"]["claim"]
    elif mutation in {"tls_unverified", "tls_extra_field"}:
        receipt["tls_preflight"] = {
            "ca_bundle": receipt["continuation"]["tls_ca_bundle"],
            "ca_count": 1,
            "certificate_chain_verification": mutation != "tls_unverified",
            "client": "HTTPTransport/_PinnedHTTPSConnection/ssl.create_default_context",
            "hostname_verification": True,
        }
        if mutation == "tls_extra_field":
            receipt["tls_preflight"]["unchecked"] = True
    elif mutation == "boolean_counter":
        receipt["continuation"]["prior_charged_transactions"] = True
    fixture.receipt_path.write_bytes(encoded(receipt))
    before = {
        path: path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()
    }
    with pytest.raises(CorpusValidationError):
        fixture.prepare()
    assert {
        path: path.read_bytes() for path in fixture.root.rglob("*") if path.is_file()
    } == before


@pytest.mark.parametrize(
    "mutation",
    [
        "old_status",
        "old_received",
        "old_reservation",
        "old_transaction",
        "old_reused",
        "old_reused_boolean",
        "old_ledger",
        "old_grants",
        "old_total",
        "old_byte_limit_float",
        "old_prior_accounting_float",
        "claim_scope",
        "claim_budget",
        "claim_policy",
        "claim_output",
        "claim_time",
        "new_review_metadata",
        "new_rights",
    ],
)
def test_continuation_semantics_reject_rehashed_failed_history(
    continuation_inputs, mutation
):
    """Fresh hashes must not turn a failed session into successful source evidence."""
    prepared = continuation_inputs.prepare()
    receipt = json.loads(prepared.receipt_bytes)
    evidence = {item.path: item for item in prepared.evidence}
    policy = json.loads(evidence[receipt["execution_policy_path"]].payload)
    claim = json.loads(evidence[receipt["session_claim"]["path"]].payload)
    binding = receipt["continuation"]

    def replace_document(reference, document):
        payload = encoded(document)
        reference["sha256"] = digest(payload)
        evidence[reference["path"]] = corpus.SavedEvidence(
            reference["path"], digest(payload), payload
        )

    previous = json.loads(evidence[binding["receipt"]["path"]].payload)
    old_claim = json.loads(evidence[binding["claim"]["path"]].payload)
    if mutation == "old_status":
        previous["status"] = "passed"
    elif mutation == "old_received":
        previous["sources"][1]["bytes_received"] = 1
    elif mutation == "old_reservation":
        previous["sources"][1]["transactions"][0][
            "unresolved_response_reservation_bytes"
        ] = 0
    elif mutation == "old_transaction":
        previous["sources"][1]["transactions"][0]["sequence"] = 2
    elif mutation == "old_reused":
        previous["sources"][0]["transaction_count"] = 0
    elif mutation == "old_reused_boolean":
        previous["sources"][0]["transaction_count"] = True
    elif mutation == "old_ledger":
        previous["ledger"]["transactions"]["ofac_sdn"] = 0
    elif mutation == "old_grants":
        previous["permission_grants"].pop()
    elif mutation == "old_total":
        previous["recovery_accounting"]["received_plus_reservation_bytes"] = 0
    elif mutation == "old_byte_limit_float":
        previous["total_body_limit"] = 34_000_000.0
    elif mutation == "old_prior_accounting_float":
        previous["prior_session_accounting"]["received_plus_reservation_bytes"] = float(
            previous["prior_session_accounting"]["received_plus_reservation_bytes"]
        )
    elif mutation == "claim_scope":
        old_claim["scope"] = corpus.CONTINUATION_SCOPE
    elif mutation == "claim_budget":
        old_claim["maximum_reserved_body_bytes"] = 0
    elif mutation == "claim_policy":
        old_claim["policy_sha256"] = "0" * 64
    elif mutation == "claim_output":
        old_claim["output"] = claim["output"]
    elif mutation == "claim_time":
        old_claim["claimed_at"] = claim["claimed_at"]
    else:
        reference = receipt["sources"][1]["permission"]
        grant = json.loads(evidence[reference["path"]].payload)
        if mutation == "new_review_metadata":
            grant["reviewer"] = "invented new reviewer"
        else:
            grant["final_eligible"] = True
        replace_document(reference, grant)
    if mutation.startswith("claim_"):
        replace_document(binding["claim"], old_claim)
        previous["session_claim"] = deepcopy(binding["claim"])
        for reference in previous["evidence"]:
            if reference["path"] == binding["claim"]["path"]:
                reference["sha256"] = binding["claim"]["sha256"]
    replace_document(binding["receipt"], previous)
    policy["continuation"] = deepcopy(binding)
    claim["continuation"] = deepcopy(binding)
    with pytest.raises(CorpusValidationError):
        corpus._continuation_lineage(receipt, policy, claim, tuple(evidence.values()))


@pytest.mark.parametrize(
    "kind", ["receipt", "selection", "evidence", "evidence_parent", "root", "fifo"]
)
def test_continuation_unsafe_paths_fail_closed(continuation_inputs, tmp_path, kind):
    fixture = continuation_inputs
    kwargs = {
        "expected_receipt_sha256": digest(fixture.receipt_path.read_bytes()),
        "evidence_root": fixture.root,
    }
    receipt_path = fixture.receipt_path
    if kind == "root":
        alias = tmp_path / "root-link"
        alias.symlink_to(fixture.root, target_is_directory=True)
        kwargs["evidence_root"] = alias
        receipt_path = alias / "continuation-run/receipt.json"
    elif kind == "evidence_parent":
        original = fixture.root / "live-run"
        moved = fixture.root / "moved-live-run"
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    else:
        path = (
            receipt_path
            if kind == "receipt"
            else fixture.output / "eu_consolidated.jsonl"
            if kind == "selection"
            else fixture.root / fixture.receipt["evidence"][0]["path"]
        )
        contents = path.read_bytes()
        path.unlink()
        if kind == "fifo":
            os.mkfifo(path)
        else:
            replacement = tmp_path / "replacement"
            replacement.write_bytes(contents)
            path.symlink_to(replacement)
    with pytest.raises(CorpusValidationError):
        corpus.prepare_continuation_sources(receipt_path, **kwargs)


def test_continuation_requires_external_receipt_pin_and_original_absence(
    continuation_inputs,
):
    fixture = continuation_inputs
    with pytest.raises(CorpusValidationError, match="trusted digest"):
        corpus.prepare_continuation_sources(
            fixture.receipt_path,
            expected_receipt_sha256="0" * 64,
            evidence_root=fixture.root,
        )
    (fixture.root / "live-run/receipt.json").write_bytes(b"{}")
    with pytest.raises(CorpusValidationError, match="unexpectedly has a final receipt"):
        fixture.prepare()


def test_continuation_original_admission_apis_remain_strict(continuation_inputs):
    fixture = continuation_inputs
    prepared = fixture.prepare()
    source = prepared.sources[0]
    selection_path = fixture.root / source.selection_path
    purpose_path = fixture.root / "fictional-purpose-for-rejection.json"
    purpose_path.write_bytes(source.bound_permission_bytes)
    for admit in (
        corpus.prepare_source,
        corpus.prepare_fictional_full_source,
        corpus.prepare_real_full_source,
        corpus.prepare_recovered_full_source,
    ):
        with pytest.raises(CorpusValidationError):
            admit(
                selection_path,
                fixture.receipt_path,
                purpose_path,
                expected_receipt_sha256=prepared.receipt_sha256,
                expected_permission_sha256=digest(source.bound_permission_bytes),
                evidence_root=fixture.root,
            )
    for bind in (
        corpus.bind_real_full_permission,
        corpus.bind_recovered_full_permission,
    ):
        with pytest.raises(CorpusValidationError):
            bind(
                selection_path,
                fixture.receipt_path,
                source_id=source.source_id,
                expected_receipt_sha256=prepared.receipt_sha256,
                expected_grant_sha256=source.permission_sha256,
                evidence_root=fixture.root,
            )
