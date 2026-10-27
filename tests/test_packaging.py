"""Build real distributions and install into a separate, empty environment.

Use UV to select the reviewed uv executable and UV_CACHE_DIR for its local
dependency cache. Missing tools or compatible wheels fail explicitly. Optional
WATCHLIST_PACKAGING_EVIDENCE names a new directory for artifacts and receipts.
"""

from __future__ import annotations

import base64
import csv
from email.parser import BytesParser
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tomllib
from urllib.parse import urlsplit
import zipfile

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version
import pytest


ROOT = Path(__file__).resolve().parents[1]
RESOURCES = {
    "watchlist/data/synthetic_entities.json",
    "watchlist/ingest/schema.sql",
    "watchlist/corpus_schema.sql",
    "watchlist/agent/schema.sql",
    "watchlist/review/schema.sql",
    "watchlist/case_memory/schema.sql",
}
PACKAGE_ENTRY_POINTS = {
    f"watchlist/{name}/__init__.py"
    for name in ("review", "api", "ui", "mcp_server", "case_memory", "llm")
}
PACKAGE_PREFIXES = ("watchlist/",)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def uv_executable() -> str:
    command = os.environ.get("UV", "uv")
    resolved = shutil.which(command)
    if resolved is None:
        pytest.fail(
            f"Required uv executable {command!r} is unavailable; set UV explicitly. No tool download is attempted."
        )
    return str(Path(resolved).resolve())


def offline_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
        "RAPIDFUZZ_IMPLEMENTATION",
    ):
        environment.pop(name, None)
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        UV_OFFLINE="1",
        UV_PYTHON_DOWNLOADS="never",
        SOURCE_DATE_EPOCH="1704067200",
    )
    return environment


def run_checked(
    command: list[str],
    *,
    cwd: Path,
    commands: list[dict],
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    environment = {**offline_environment(), **(extra_environment or {})}
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    commands.append(
        {
            "command": command,
            "cwd": str(cwd),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "environment_overrides": extra_environment or {},
        }
    )
    assert result.returncode == 0, (
        f"Offline command failed ({result.returncode}): {command!r}\n"
        f"{result.stdout}\n{result.stderr}\nNo missing tool or package is skipped."
    )
    return result


def source_files() -> dict[str, Path]:
    return {
        str(path.relative_to(ROOT / "src")): path
        for package in PACKAGE_PREFIXES
        for path in (ROOT / "src" / package).rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


@pytest.fixture(scope="module")
def distributions(tmp_path_factory):
    """Build once per test run; every output lives outside the source checkout."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    expected_backend = Requirement(project["build-system"]["requires"][0])
    assert expected_backend.name == "hatchling"
    assert (
        Version(importlib.metadata.version("hatchling")) in expected_backend.specifier
    )
    uv = uv_executable()
    directory = tmp_path_factory.mktemp("distribution-check")
    evidence_setting = os.environ.get("WATCHLIST_PACKAGING_EVIDENCE")
    evidence = Path(evidence_setting).resolve() if evidence_setting else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=False)
    inputs = {
        **source_files(),
        ".gitignore": ROOT / ".gitignore",
        "pyproject.toml": ROOT / "pyproject.toml",
        "README.md": ROOT / "README.md",
        "uv.lock": ROOT / "uv.lock",
    }
    before = {name: sha256(path) for name, path in inputs.items()}
    receipt = {
        "python": sys.version,
        "build_backend": str(expected_backend),
        "declared_python": project["project"]["requires-python"],
        "validated_python_series": f"{sys.version_info.major}.{sys.version_info.minor}",
        "commands": [],
        "input_sha256": before,
        "network_allowed": False,
        "original_environment_modified": False,
    }
    state = {"root": directory, "uv": uv, "project": project, "receipt": receipt}
    try:
        run_checked(
            [
                uv,
                "build",
                "--offline",
                "--no-build-isolation",
                "--python",
                sys.executable,
                "--out-dir",
                str(directory / "dist"),
            ],
            cwd=ROOT,
            commands=receipt["commands"],
        )
        entries = list((directory / "dist").iterdir())
        artifacts = [path for path in entries if path.name != ".gitignore"]
        if (directory / "dist/.gitignore").exists():
            assert (directory / "dist/.gitignore").read_text() == "*"
        wheels = [path for path in artifacts if path.suffix == ".whl"]
        sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
        assert len(artifacts) == 2 and len(wheels) == len(sdists) == 1
        state.update(wheel=wheels[0], sdist=sdists[0])
        receipt["artifacts"] = {
            path.name: {"sha256": sha256(path), "bytes": path.stat().st_size}
            for path in artifacts
        }
        yield state
    finally:
        after = {name: sha256(path) for name, path in inputs.items()}
        receipt["source_inputs_unchanged"] = before == after
        if evidence:
            for path in (directory / "dist").glob("*"):
                if path.is_file() and path.name != ".gitignore":
                    shutil.copyfile(path, evidence / path.name)
            with (evidence / "receipt.json").open("x") as stream:
                json.dump(receipt, stream, indent=2)
                stream.write("\n")
        assert before == after, "Building or testing changed package source inputs."


def test_running_python_matches_project_and_lock_metadata():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    supported = SpecifierSet(project["project"]["requires-python"])
    locked_support = SpecifierSet(lock["requires-python"])
    running = Version(".".join(map(str, sys.version_info[:3])))
    assert running in supported and running in locked_support
    # uv may canonicalize >=3.14,<3.15 as ==3.14.*. Compare stable releases
    # around the supported series; the separate uv check validates the lock.
    major, minor = sys.version_info[:2]
    boundary_versions = {
        Version(f"{major}.{minor + offset}.{patch}")
        for offset in (-1, 0, 1)
        for patch in (0, 1, 99)
    }
    boundary_versions.add(Version(f"{major + 1}.0.0"))
    for version in boundary_versions:
        assert (version in supported) == (version in locked_support), version


def test_uv_validates_the_lock_offline():
    commands = []
    run_checked(
        [uv_executable(), "lock", "--check", "--offline"], cwd=ROOT, commands=commands
    )


def test_wheel_includes_exact_package_sources_and_runtime_resources(distributions):
    with zipfile.ZipFile(distributions["wheel"]) as archive:
        members = archive.namelist()
        assert len(members) == len(set(members))
        actual = {name for name in members if name.startswith(PACKAGE_PREFIXES)}
        expected = source_files()
        assert actual == set(expected)
        assert RESOURCES | PACKAGE_ENTRY_POINTS <= actual
        for name, source in expected.items():
            assert archive.read(name) == source.read_bytes()
        assert all(
            name.startswith(PACKAGE_PREFIXES) or ".dist-info/" in name
            for name in members
        )
        assert all(
            ".." not in PurePosixPath(name).parts and not name.startswith("/")
            for name in members
        )
        distributions["receipt"]["wheel_members"] = sorted(members)


def test_wheel_metadata_and_record_hashes_are_complete(distributions):
    with zipfile.ZipFile(distributions["wheel"]) as archive:
        names = archive.namelist()
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        project = distributions["project"]["project"]
        assert metadata["License-Expression"] == project["license"] == "MIT"
        assert (
            metadata["Name"] == project["name"]
            and metadata["Version"] == project["version"]
        )
        assert SpecifierSet(metadata["Requires-Python"]) == SpecifierSet(
            project["requires-python"]
        )
        actual = [Requirement(value) for value in metadata.get_all("Requires-Dist", [])]
        for extra in ("", *project.get("optional-dependencies", {})):
            expected = [Requirement(value) for value in project["dependencies"]]
            expected.extend(
                Requirement(value)
                for value in project.get("optional-dependencies", {}).get(extra, [])
            )
            included = [
                requirement
                for requirement in actual
                if requirement.marker is None
                or requirement.marker.evaluate({"extra": extra})
            ]
            assert {
                (canonicalize_name(requirement.name), str(requirement.specifier))
                for requirement in included
            } == {
                (canonicalize_name(requirement.name), str(requirement.specifier))
                for requirement in expected
            }
        record_name = next(name for name in names if name.endswith(".dist-info/RECORD"))
        records = list(csv.reader(io.StringIO(archive.read(record_name).decode())))
        assert {row[0] for row in records} == set(names)
        for name, digest, size in records:
            if name == record_name:
                assert digest == size == ""
                continue
            payload = archive.read(name)
            expected_hash = (
                base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
                .decode()
                .rstrip("=")
            )
            assert digest == "sha256=" + expected_hash and size == str(len(payload))


def test_source_distribution_excludes_local_data_models_and_history(distributions):
    with tarfile.open(distributions["sdist"]) as archive:
        members = archive.getmembers()
        assert all(member.isfile() or member.isdir() for member in members)
        roots = {PurePosixPath(member.name).parts[0] for member in members}
        assert len(roots) == 1
        files = {
            str(PurePosixPath(member.name).relative_to(next(iter(roots)))): member
            for member in members
            if member.isfile()
        }
        expected = {"src/" + name: path for name, path in source_files().items()}
        expected.update(
            {
                name: ROOT / name
                for name in (
                    ".gitignore",
                    "pyproject.toml",
                    "README.md",
                    "uv.lock",
                    "LICENSE",
                )
            }
        )
        assert set(files) == set(expected) | {"PKG-INFO"}
        assert {
            "src/" + name for name in RESOURCES | PACKAGE_ENTRY_POINTS
        } <= files.keys()
        for name, path in expected.items():
            assert archive.extractfile(files[name]).read() == path.read_bytes()
        distributions["receipt"]["sdist_members"] = sorted(files)


def test_wheel_rebuild_from_sdist_is_identical_outside_repository(distributions):
    extracted = distributions["root"] / "unpacked-sdist"
    extracted.mkdir()
    with tarfile.open(distributions["sdist"]) as archive:
        archive.extractall(extracted, filter="data")
    (project,) = extracted.iterdir()
    assert not project.is_relative_to(ROOT)
    rebuilt = distributions["root"] / "rebuilt"
    run_checked(
        [
            distributions["uv"],
            "build",
            "--offline",
            "--no-build-isolation",
            "--python",
            sys.executable,
            "--wheel",
            "--out-dir",
            str(rebuilt),
        ],
        cwd=project,
        commands=distributions["receipt"]["commands"],
    )
    (wheel,) = rebuilt.glob("*.whl")
    assert wheel.read_bytes() == distributions["wheel"].read_bytes()
    distributions["receipt"]["sdist_wheel_rebuild_identical"] = True


def test_clean_install_and_cli_run_outside_repository(distributions):
    uv, root, receipt = (
        distributions["uv"],
        distributions["root"],
        distributions["receipt"],
    )
    environment = root / "clean-environment"
    working = root / "outside-working-directory"
    working.mkdir()
    run_checked(
        [uv, "venv", "--offline", "--python", sys.executable, str(environment)],
        cwd=working,
        commands=receipt["commands"],
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert (
        "include-system-site-packages = false"
        in (environment / "pyvenv.cfg").read_text().lower()
    )
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    dependency = next(
        package for package in lock["package"] if package["name"] == "rapidfuzz"
    )
    locked_rapidfuzz = dependency["version"]
    requirements = root / "core-requirements.txt"
    wheel = distributions["wheel"]
    requirement_lines = [
        f"polyglot-watchlist @ {wheel.as_uri()} --hash=sha256:{sha256(wheel)}"
    ]
    tags = set(sys_tags())
    compatible_hashes = [
        artifact["hash"]
        for artifact in dependency["wheels"]
        if parse_wheel_filename(Path(urlsplit(artifact["url"]).path).name)[3] & tags
    ]
    assert compatible_hashes, (
        "The lock has no compatible RapidFuzz wheel for this runtime."
    )
    local_options = []
    installed_dependency = None
    if os.environ.get("WATCHLIST_WHEELHOUSE"):
        wheelhouse = Path(os.environ["WATCHLIST_WHEELHOUSE"]).resolve()
        assert wheelhouse.is_dir(), (
            "WATCHLIST_WHEELHOUSE must name a directory of approved wheels."
        )
        compatible = []
        for artifact in dependency["wheels"]:
            name = Path(urlsplit(artifact["url"]).path).name
            path = wheelhouse / name
            if path.is_file() and parse_wheel_filename(name)[3] & tags:
                assert "sha256:" + sha256(path) == artifact["hash"]
                compatible.append(
                    {
                        "filename": name,
                        "sha256": sha256(path),
                        "bytes": path.stat().st_size,
                    }
                )
        assert compatible, (
            "No compatible lock-verified RapidFuzz wheel exists in WATCHLIST_WHEELHOUSE."
        )
        receipt["verified_local_dependency_wheels"] = compatible
        installed_dependency = wheelhouse / compatible[0]["filename"]
        requirement_lines.append(
            f"rapidfuzz @ {installed_dependency.as_uri()} --hash=sha256:{sha256(installed_dependency)}"
        )
        local_options = ["--no-index"]
    else:
        requirement_lines.append(
            f"rapidfuzz=={locked_rapidfuzz} "
            + " ".join("--hash=" + value for value in compatible_hashes)
        )
    requirements.write_text("\n".join(requirement_lines) + "\n")
    receipt["installation_requirements"] = requirement_lines
    run_checked(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--python",
            str(python),
            *local_options,
            "--require-hashes",
            "--requirements",
            str(requirements),
        ],
        cwd=working,
        commands=receipt["commands"],
    )
    inspect = """
from importlib import import_module
from importlib.machinery import EXTENSION_SUFFIXES, ExtensionFileLoader
import importlib.metadata as metadata
from importlib.resources import files
import json
from pathlib import Path
import sys
def no_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:
        raise AssertionError('network forbidden during installed package smoke')
sys.addaudithook(no_network)
import watchlist
from watchlist.entities import load_entities
from rapidfuzz import fuzz
scorer = import_module(fuzz.WRatio.__module__)
scorer_path = Path(scorer.__file__).resolve()
native_scorer = isinstance(scorer.__spec__.loader, ExtensionFileLoader) and any(
    scorer_path.name == scorer.__name__.rsplit('.', 1)[-1] + suffix
    for suffix in EXTENSION_SUFFIXES
)
resources = {name: len(files('watchlist').joinpath(name).read_bytes())
             for name in json.loads(sys.argv[1])}
print(json.dumps({'package_path': watchlist.__file__, 'prefix': sys.prefix,
                  'sys_path': sys.path, 'resources': resources,
                  'entities': len(load_entities()), 'scorer_module': fuzz.WRatio.__module__,
                  'scorer_file': str(scorer_path), 'scorer_is_native': native_scorer,
                  'scorer_export_matches': scorer.WRatio is fuzz.WRatio,
                  'rapidfuzz_package_path': str(Path(metadata.distribution('rapidfuzz').locate_file('rapidfuzz')).resolve()),
                  'direct_urls': {name: json.loads(metadata.distribution(name).read_text('direct_url.json') or 'null')
                                  for name in ('polyglot-watchlist', 'rapidfuzz')},
                  'installed': {item.metadata['Name'].lower(): item.version
                                for item in metadata.distributions()}}))
"""
    inspection = run_checked(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            inspect,
            json.dumps(sorted(name.removeprefix("watchlist/") for name in RESOURCES)),
        ],
        cwd=working,
        commands=receipt["commands"],
    )
    observed = json.loads(inspection.stdout)
    assert Path(observed["package_path"]).is_relative_to(environment)
    assert Path(observed["prefix"]) == environment
    assert not any(Path(path).is_relative_to(ROOT) for path in observed["sys_path"])
    assert set(observed["installed"]) == {"polyglot-watchlist", "rapidfuzz"}
    assert observed["installed"]["rapidfuzz"] == locked_rapidfuzz
    assert observed["entities"] == 12 and all(observed["resources"].values())
    # RapidFuzz selects a native backend for the available CPU features.
    assert observed["scorer_module"] in {
        "rapidfuzz.fuzz_cpp",
        "rapidfuzz.fuzz_cpp_avx2",
        "rapidfuzz.fuzz_cpp_sse2",
    }
    assert observed["scorer_is_native"] is True
    assert observed["scorer_export_matches"] is True
    rapidfuzz_package = Path(observed["rapidfuzz_package_path"])
    assert rapidfuzz_package.is_relative_to(environment)
    assert Path(observed["scorer_file"]).parent == rapidfuzz_package
    assert observed["direct_urls"]["polyglot-watchlist"]["url"] == wheel.as_uri()
    if installed_dependency:
        assert (
            observed["direct_urls"]["rapidfuzz"]["url"] == installed_dependency.as_uri()
        )
    result = run_checked(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "watchlist",
            "--name",
            "Luma Vesper",
            "--top-k",
            "1",
        ],
        cwd=working,
        commands=receipt["commands"],
    )
    output = json.loads(result.stdout)
    assert output["candidates"][0]["entity_id"] == "syn-001"
    assert output["candidates"][0]["similarity_score"] == 100.0
    assert output["candidates"][0]["synthetic"] is True
    assert "not identity confirmation" in output["notice"]
    receipt["clean_install"] = observed
    receipt["installed_cli_result"] = output


def test_qa_lock_group_recreates_in_an_empty_offline_environment(distributions):
    """Exercise QA lock resolution without depending on the installed QA env."""
    uv, root, receipt = (
        distributions["uv"],
        distributions["root"],
        distributions["receipt"],
    )
    environment = root / "clean-qa-environment"
    working = root / "outside-qa-working-directory"
    working.mkdir()
    run_checked(
        [uv, "venv", "--offline", "--python", sys.executable, str(environment)],
        cwd=working,
        commands=receipt["commands"],
    )
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    inspect = """
from importlib import metadata
import json
from pathlib import Path
import re
import site
import sys
print(json.dumps({
    'prefix': sys.prefix, 'base_prefix': sys.base_prefix,
    'user_site_enabled': site.ENABLE_USER_SITE, 'sys_path': sys.path,
    'installed': {re.sub(r'[-_.]+', '-', item.metadata['Name']).lower(): item.version
                  for item in metadata.distributions()},
}))
"""
    initial = run_checked(
        [str(python), "-I", "-B", "-c", inspect],
        cwd=working,
        commands=receipt["commands"],
    )
    empty = json.loads(initial.stdout)
    assert empty["installed"] == {}, (
        "The QA reproduction must start with no installed packages."
    )
    receipt["qa_empty_before_install"] = empty
    run_checked(
        [
            uv,
            "sync",
            "--locked",
            "--offline",
            "--only-group",
            "qa",
            "--no-install-project",
            "--python",
            str(python),
        ],
        cwd=ROOT,
        commands=receipt["commands"],
        extra_environment={"UV_PROJECT_ENVIRONMENT": str(environment)},
    )
    inspection = run_checked(
        [str(python), "-I", "-B", "-c", inspect],
        cwd=working,
        commands=receipt["commands"],
    )
    observed = json.loads(inspection.stdout)
    assert Path(observed["prefix"]) == environment
    assert (
        observed["prefix"] != observed["base_prefix"]
        and observed["user_site_enabled"] is False
    )
    assert (
        "include-system-site-packages = false"
        in (environment / "pyvenv.cfg").read_text().lower()
    )
    assert not any(Path(path).is_relative_to(ROOT) for path in observed["sys_path"])
    expected = dict(
        item.split("==", 1)
        for item in distributions["project"]["dependency-groups"]["qa"]
    )
    expected = {canonicalize_name(name): version for name, version in expected.items()}
    assert set(expected) == {"ruff", "pre-commit", "detect-secrets"}
    assert all(
        observed["installed"].get(name) == version for name, version in expected.items()
    )
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {
        canonicalize_name(item["name"]): item["version"] for item in lock["package"]
    }
    assert all(
        locked.get(name) == version for name, version in observed["installed"].items()
    )
    assert not {
        "polyglot-watchlist",
        "rapidfuzz",
        "pytest",
        "hatchling",
        "torch",
    } & set(observed["installed"])
    versions = {}
    for name, module in (
        ("ruff", "ruff"),
        ("pre-commit", "pre_commit"),
        ("detect-secrets", "detect_secrets"),
    ):
        process = run_checked(
            [str(python), "-I", "-B", "-m", module, "--version"],
            cwd=working,
            commands=receipt["commands"],
        )
        versions[name] = process.stdout.strip()
        assert versions[name].endswith(expected[name])
    receipt["qa_clean_install"] = observed
    receipt["qa_tool_cli_versions"] = versions
