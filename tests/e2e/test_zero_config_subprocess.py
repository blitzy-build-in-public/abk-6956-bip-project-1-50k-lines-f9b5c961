"""End-to-end proof that the installed plugin activates with no Foundry configuration at all.

Acceptance criterion 2 (AAP 0.14.1, "Zero-config activation"): a real ``pytest`` child in a
directory holding one test file and nothing else exits 0, prints ``foundry: profile=default`` and
writes the three report files AAP 0.4.6 places in the invocation directory, so activation can only
come from the installed ``pytest11`` entry point (AAP 0.4.2). Nothing sleeps, no wall clock is read
and no socket is opened: the child's one test asks for a filesystem-only fixture.
"""

from __future__ import annotations

import importlib.metadata
import json
import pathlib
import xml.etree.ElementTree
from collections.abc import Callable
from typing import Any, Final

import pytest

# The banner AAP 0.4.2 publishes, the two defaults AAP 0.4.5 publishes, the child's node id, and the
# two ambient sentinels whose leaks are observable, so a passing child proves the stripping worked.
EXPECTED_BANNER: Final[str] = "foundry: profile=default"
EXPECTED_PROFILE: Final[str] = "default"
EXPECTED_TIMEOUT: Final[float] = 10.0
CHILD_TEST_NAME: Final[str] = "test_defaults.py"
CHILD_NODEID: Final[str] = f"tests/{CHILD_TEST_NAME}::test_defaults"
PROBE_ADDOPTS: Final[str] = "-k __foundry_hygiene_probe_that_matches_nothing__"
PROBE_TIMEOUT: Final[str] = "99.5"

DOCUMENTED_KEYS: Final[tuple[str, ...]] = (
    "record_type",
    "run_id",
    "started_at",
    "finished_at",
    "profile",
    "nodeid",
    "outcome",
    "attempts",
    "duration_ms",
    "markers",
    "failures",
    "artifacts",
    "quarantine",
    "artifacts_disabled",
)
"""Every field AAP 0.4.6 fixes for a result record, each part of the compatibility promise, plus
the additive ``artifacts_disabled`` flag of AAP 0.4.7 (§0.4.9 permits added record keys)."""

CONFIG_FREE_PATHS: Final[tuple[str, ...]] = (
    "foundry.toml",
    "pyproject.toml",
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "tests/conftest.py",
    "cassettes",
    "testdata",
)
"""Every path whose presence would let the child resolve something the claim excludes."""

_CHILD_TEST_SOURCE: Final[str] = f'''"""The child project's only test."""


def test_defaults(foundry_config):
    assert foundry_config.profile == "{EXPECTED_PROFILE}"
    assert foundry_config.http.timeout == {EXPECTED_TIMEOUT}
'''
"""The whole child test module, rendered from the two constants so one name owns each value."""


def _write_project(root: pathlib.Path) -> pathlib.Path:
    """Write the configuration-free project into ``root`` and return ``root``.

    One test file is everything a run needs. The absence of every other path is asserted, not
    assumed: a stray ``conftest.py`` would make this an ordinary configured run (AAP 0.4.2).
    """
    test_file = root / "tests" / CHILD_TEST_NAME
    test_file.parent.mkdir(parents=True)
    test_file.write_text(_CHILD_TEST_SOURCE, encoding="utf-8", newline="\n")
    present = [relative for relative in CONFIG_FREE_PATHS if (root / relative).exists()]
    assert present == [], (
        f"the zero-configuration project at {root} must hold nothing but {CHILD_TEST_NAME}; it "
        f"also holds {present}, which pytest or Foundry would resolve, so the run would no longer "
        "prove activation with nothing configured (AAP 0.4.2)"
    )
    return root


def _load_results(project: pathlib.Path) -> list[dict[str, Any]]:
    """Return the ``results.json`` elements this project's run wrote, parsed and shape-checked."""
    path = project / "results.json"
    assert path.is_file(), (
        f"the zero-configuration run wrote no results.json in its invocation directory {project}: "
        "AAP 0.4.6 writes it there unless --foundry-json-report moves it (assumption A5)"
    )
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), (
        f"{path} holds a JSON {type(document).__name__} rather than the array of result elements "
        "AAP 0.4.6 specifies"
    )
    elements: list[dict[str, Any]] = []
    for element in document:
        assert isinstance(element, dict), (
            f"{path} holds the element {element!r}, which is not a JSON object with a record_type"
        )
        elements.append(element)
    return elements


def test_zero_config_run_reports_default_profile(
    tmp_path: pathlib.Path,
    installed_distribution: importlib.metadata.Distribution,
    repo_root: pathlib.Path,
    run_pytest: Callable[..., Any],
) -> None:
    """A plain ``pytest`` run in an unconfigured directory exits 0 and reports the defaults.

    The child holds only ``tests/test_defaults.py``, so the fixtures, the banner and the three
    report files can only come from the installed plugin, resolving the two AAP 0.4.5 defaults.
    """
    # A versionless distribution is a failure, not a skip: without it the child has no entry point.
    assert installed_distribution.version, (
        "the 'pytest-foundry' distribution is not installed in the interpreter running this suite, "
        "so the child has no pytest11 entry point to activate (AAP 0.4.2): install it with "
        "`uv sync --extra dev` and rerun"
    )

    project = _write_project(tmp_path / "proj")
    result = run_pytest("-q", cwd=project)
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, (
        f"a run in a configuration-free directory must exit 0 (AAP 0.14.1):\n{output}"
    )
    assert EXPECTED_BANNER in result.stdout, (
        f"the terminal summary printed no {EXPECTED_BANNER!r}, so the plugin did not activate: "
        f"the pytest11 entry point is missing or the banner is not written (AAP 0.4.2):\n{output}"
    )
    assert "1 passed" in output, (
        f"the child collected or ran something other than the project's one test:\n{output}"
    )

    elements = _load_results(project)
    records = [element for element in elements if element.get("record_type") == "result"]
    run_errors = [element for element in elements if element.get("record_type") == "run_error"]
    assert run_errors == [], (
        f"the run reported itself broken with {len(run_errors)} run_error element(s), which one "
        f"passing test cannot produce (AAP 0.4.6): {run_errors}"
    )
    assert len(records) == 1, (
        f"expected one result record for the child's one test and found {len(records)}: {elements}"
    )
    record = records[0]
    missing = [key for key in DOCUMENTED_KEYS if key not in record]
    assert missing == [], (
        f"the result record is missing the documented keys {missing}, which AAP 0.4.6 fixes as "
        f"part of the published contract: {record}"
    )
    assert record["nodeid"] == CHILD_NODEID, (
        f"the record names {record['nodeid']!r} rather than {CHILD_NODEID!r}: {record}"
    )
    assert record["profile"] == EXPECTED_PROFILE, (
        f"the run resolved profile {record['profile']!r} with nothing configured rather than "
        f"{EXPECTED_PROFILE!r} (AAP 0.4.5)"
    )
    assert record["outcome"] == "passed", (
        f"the record's outcome is {record['outcome']!r} for a test that passed: {record}"
    )
    assert record["attempts"] == 1, (
        f"the record reports {record['attempts']!r} attempts although the test never failed and "
        "no retry was asked for (assumption A2)"
    )

    junit_path = project / "junit.xml"
    assert junit_path.is_file(), (
        f"the run wrote no junit.xml beside results.json in {project} (AAP 0.4.6)"
    )
    suite = xml.etree.ElementTree.parse(junit_path).getroot()
    cases = suite.findall("testcase")
    assert len(cases) == 1, (
        f"{junit_path} holds {len(cases)} <testcase> elements for the child's one test: "
        f"{xml.etree.ElementTree.tostring(suite, encoding='unicode')}"
    )
    assert cases[0].get("name") == "test_defaults", (
        f"the single <testcase> is named {cases[0].get('name')!r}, not after the test that ran"
    )

    report_path = project / "report.html"
    assert report_path.is_file(), (
        f"the run wrote no report.html beside results.json in {project}: reporting.html defaults "
        "to true (AAP 0.4.6)"
    )
    report = report_path.read_text(encoding="utf-8")
    for scheme in ("http://", "https://"):
        assert scheme not in report, (
            f"{report_path} references {scheme}, so it cannot be opened offline (AAP 0.4.6)"
        )
    assert "test_defaults" in report, (
        f"{report_path} does not list the test the run executed: its table carries every result"
    )

    run_id = record["run_id"]
    run_dir = project / "artifacts" / run_id
    assert run_dir.is_dir(), (
        f"the run wrote no artifact directory {run_dir}: AAP 0.4.6 roots capture at "
        f"artifacts/<run_id>/ inside the invocation directory:\n{output}"
    )
    assert not (repo_root / "artifacts" / run_id).is_dir(), (
        f"run {run_id} rooted its artifacts at the repository root {repo_root} rather than the "
        f"child's invocation directory {project} (assumption A5)"
    )
    artifacts_root = run_dir.resolve().as_posix()
    assert f"foundry: run {run_id} artifacts at {artifacts_root}" in result.stdout, (
        f"the terminal summary did not name {artifacts_root} as the artifact root, so the run "
        f"captured outside its invocation directory (assumption A5):\n{output}"
    )
    # A passing test keeps only its manifest, so assert the shape written rather than a count.
    for entry in sorted(run_dir.iterdir()):
        assert entry.is_dir(), f"{entry} is not a per-test artifact directory (AAP 0.4.6)"
        assert "/" not in entry.name and "::" not in entry.name, (
            f"the per-test directory name {entry.name!r} carries a raw separator: node ids are "
            "percent-encoded before they become paths (AAP 0.4.6)"
        )
        assert (entry / "manifest.json").is_file(), (
            f"the per-test artifact directory {entry} holds no manifest.json, the one file a "
            "passing test keeps (AAP 0.4.5)"
        )


def test_ambient_environment_does_not_leak(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    run_pytest: Callable[..., Any],
) -> None:
    """Export the sentinels a real CI image carries, and prove the child never sees them.

    An inherited ``PYTEST_ADDOPTS`` would deselect the child's only test (exit 5) and an inherited
    ``FOUNDRY_HTTP__TIMEOUT`` would override the default it asserts, so the unconfigured project
    must keep passing with both exported: ``subprocess_env`` in ``tests/conftest.py`` strips them.
    """
    monkeypatch.setenv("PYTEST_ADDOPTS", PROBE_ADDOPTS)
    monkeypatch.setenv("FOUNDRY_HTTP__TIMEOUT", PROBE_TIMEOUT)

    project = _write_project(tmp_path / "proj_leak")
    result = run_pytest("-q", cwd=project)
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 0, (
        "the child inherited the outer process's ambient environment: PYTEST_ADDOPTS="
        f"{PROBE_ADDOPTS!r} deselects every test and FOUNDRY_HTTP__TIMEOUT={PROBE_TIMEOUT} "
        "overrides the 10.0 default this project asserts. The environment builder in "
        f"tests/conftest.py must strip both before spawning the child.\n{output}"
    )
    assert "1 passed" in output, (
        f"the child did not run the project's one test with the sentinels exported:\n{output}"
    )
    assert "deselected" not in output, (
        f"the child applied PYTEST_ADDOPTS={PROBE_ADDOPTS!r} inherited from the outer process, so "
        f"the outer run's options reached it:\n{output}"
    )
    assert EXPECTED_BANNER in result.stdout, (
        f"the child printed no {EXPECTED_BANNER!r} under the sentinels, although isolation alone "
        f"may not change the defaults a run resolves (AAP 0.4.2):\n{output}"
    )
