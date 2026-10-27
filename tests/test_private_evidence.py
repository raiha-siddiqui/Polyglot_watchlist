"""Exercise private-evidence skips through real pytest setup/call/teardown reports."""

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


@pytest.mark.parametrize("require_private", [False, True])
def test_private_evidence_hook_preserves_unrelated_failures(tmp_path, require_private):
    project = tmp_path / "project"
    tests = project / "tests"
    tests.mkdir(parents=True)
    (tests / "conftest.py").write_bytes(
        Path(__file__).with_name("conftest.py").read_bytes()
    )
    (tests / "test_cases.py").write_text(
        """import os
from pathlib import Path
import pytest

PRIVATE = Path("data/eval-comparisons/missing-grant.json")

def test_private_absolute():
    PRIVATE.absolute().read_bytes()

def test_private_relative():
    PRIVATE.read_bytes()

def test_private_bytes():
    open(os.fsencode(PRIVATE), "rb")

@pytest.fixture
def private_setup():
    PRIVATE.read_bytes()

def test_private_setup(private_setup):
    pass

def test_public_missing():
    Path("config/required.json").read_bytes()

def test_public_bytes():
    open(b"config/required.json", "rb")

def test_sibling_prefix():
    Path("data/eval-comparisons-public/missing.json").read_bytes()

def test_parent_escape():
    PRIVATE.parent.mkdir(parents=True, exist_ok=True)
    (PRIVATE.parent / "../public.json").read_bytes()

def test_symlink_escape():
    PRIVATE.parent.mkdir(parents=True, exist_ok=True)
    (PRIVATE.parent / "link").symlink_to(Path("config").absolute(), target_is_directory=True)
    (PRIVATE.parent / "link/missing.json").read_bytes()

def test_existing_file():
    PRIVATE.parent.mkdir(parents=True, exist_ok=True)
    existing = PRIVATE.with_name("exists.json")
    existing.write_text("{}")
    raise FileNotFoundError(2, "not genuinely missing", str(existing))

def test_no_filename():
    raise FileNotFoundError("no filename")

def test_invalid_filename():
    raise FileNotFoundError(2, "not a filesystem path", 42)

def test_other_error():
    raise PermissionError(13, "not a missing file", str(PRIVATE))

def test_wrapped_error():
    try:
        PRIVATE.read_bytes()
    except FileNotFoundError as exc:
        raise ValueError("invalid evidence") from exc

def test_prior_failure():
    try:
        raise AssertionError("unrelated earlier failure")
    except AssertionError:
        PRIVATE.read_bytes()

@pytest.fixture
def private_teardown():
    yield
    PRIVATE.read_bytes()

def test_teardown(private_teardown):
    pass
""",
        encoding="utf-8",
    )
    xml = tmp_path / "results.xml"
    environment = dict(os.environ)
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        WATCHLIST_REQUIRE_PRIVATE_EVIDENCE="1" if require_private else "0",
    )
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={xml}",
            str(tests),
        ],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    cases = {case.attrib["name"]: case for case in ET.parse(xml).iter("testcase")}
    assert len(cases) == 16
    private = {
        "test_private_absolute",
        "test_private_relative",
        "test_private_bytes",
        "test_private_setup",
    }
    skipped = {name for name, case in cases.items() if case.find("skipped") is not None}
    assert skipped == (set() if require_private else private)
    for name, case in cases.items():
        if name not in skipped:
            assert case.find("failure") is not None or case.find("error") is not None, (
                name
            )
    for name in skipped:
        assert (
            "private Step 08 evidence" in cases[name].find("skipped").attrib["message"]
        )
