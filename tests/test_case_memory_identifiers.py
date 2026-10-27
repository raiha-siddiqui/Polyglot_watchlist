"""Exact identifiers, registry ambiguity checks and memory config validation."""

import pytest

from test_case_memory_support import (
    SNAP_ALT,
    SNAP_MAIN,
    SNAP_REFRESH,
    SNAP_UNREGISTERED,
    memory_config,
    memory_config_dict,
)
from watchlist.case_memory.identifiers import RecordRef, record_key, validate_identifier
from watchlist.case_memory.state import (
    MemoryConfig,
    MemoryInputError,
    MemoryVersionError,
    UnregisteredIdentifierError,
)


def test_record_keys_depend_only_on_namespace_and_exact_id():
    assert record_key("fictional-synthetic-v1", "syn-001") != record_key(
        "fictional-synthetic-v1", "syn-012"
    )
    # The same ID in another namespace is another record.
    assert record_key("fictional-synthetic-v1", "syn-001") != record_key(
        "fictional-alt-v1", "syn-001"
    )
    assert record_key("fictional-synthetic-v1", "syn-001") == record_key(
        "fictional-synthetic-v1", "syn-001"
    )


@pytest.mark.parametrize(
    "value",
    ["Luma Vesper", " syn-001", "syn-001 ", "", "syn\n001", "лума", "x" * 129, 7, None],
)
def test_names_and_malformed_values_are_never_identifiers(value):
    with pytest.raises(MemoryInputError):
        validate_identifier(value, "id")


def test_record_ref_requires_exact_snapshot_digest():
    with pytest.raises(MemoryInputError):
        RecordRef("fictional-synthetic-v1", "syn-001", "not-a-digest")
    ref = RecordRef("fictional-synthetic-v1", "syn-001", SNAP_MAIN)
    assert ref.to_dict()["record_key"] == record_key(
        "fictional-synthetic-v1", "syn-001"
    )


def test_registry_maps_snapshots_to_one_namespace():
    registry = memory_config().registry()
    assert (
        registry.namespace_for_snapshot(SNAP_MAIN).namespace_id
        == "fictional-synthetic-v1"
    )
    assert (
        registry.namespace_for_snapshot(SNAP_REFRESH).namespace_id
        == "fictional-synthetic-v1"
    )
    assert registry.namespace_for_snapshot(SNAP_ALT).namespace_id == "fictional-alt-v1"
    assert registry.namespace_for_snapshot(SNAP_UNREGISTERED) is None
    with pytest.raises(UnregisteredIdentifierError):
        registry.refs(SNAP_UNREGISTERED, ["syn-001"])
    refs = registry.refs(SNAP_ALT, ["syn-001"])
    assert refs[0].namespace_id == "fictional-alt-v1"
    with pytest.raises(UnregisteredIdentifierError):
        registry.check_source(registry.require_namespace(SNAP_MAIN), "fictional-alt-v1")


def test_ambiguous_registrations_are_rejected():
    data = memory_config_dict()
    data["namespaces"].append(dict(data["namespaces"][0], namespace_id="other-ns"))
    with pytest.raises(MemoryInputError, match="two namespaces"):
        MemoryConfig.from_dict(data)
    data = memory_config_dict()
    data["snapshots"].append(
        dict(data["snapshots"][0], namespace_id="fictional-alt-v1")
    )
    with pytest.raises(MemoryInputError, match="registered twice"):
        MemoryConfig.from_dict(data)
    data = memory_config_dict()
    data["snapshots"][0]["namespace_id"] = "nobody"
    with pytest.raises(MemoryInputError, match="unregistered namespace"):
        MemoryConfig.from_dict(data)


def test_config_versions_and_policy_bounds():
    data = memory_config_dict()
    data["config_version"] = "case-memory-config-v0"
    with pytest.raises(MemoryVersionError):
        MemoryConfig.from_dict(data)
    data = memory_config_dict()
    data["policy"]["display_origins"]["human"] = ["human", "unknown"]
    with pytest.raises(MemoryInputError, match="unknown-origin"):
        MemoryConfig.from_dict(data)


def test_project_config_registers_the_current_packaged_snapshot():
    config = MemoryConfig.load("config/memory.json")
    assert [s.snapshot_sha256 for s in config.snapshots] == [SNAP_MAIN]
    assert config.policy.cards_per_candidate == 3
    assert config.policy.visibility_days == 90
    assert config.policy.display_origins["human"] == ("human",)
    assert config.policy.display_origins["unknown"] == ()
    assert all(n.fictional for n in config.namespaces)


def test_namespace_must_have_exactly_one_source_label():
    """Regression: record_key omits the source, so two sources in one namespace
    could reuse an entity ID and collide."""
    data = memory_config_dict()
    data["namespaces"][0]["sources"] = ["fictional-synthetic-v1", "fictional-other-v1"]
    with pytest.raises(MemoryInputError, match="exactly one"):
        MemoryConfig.from_dict(data)
    data["namespaces"][0]["sources"] = []
    with pytest.raises(MemoryInputError, match="exactly one"):
        MemoryConfig.from_dict(data)
