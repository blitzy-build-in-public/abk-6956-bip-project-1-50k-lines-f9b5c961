"""End-to-end proof that the installed plugin activates with no Foundry configuration at all.

AAP 0.14.1 criterion 2: a real ``pytest`` child holding one test file and nothing else exits 0,
prints ``foundry: profile=default`` and writes the three report files in its invocation directory.
"""

from __future__ import annotations

import importlib.metadata
import json
import pathlib
import xml.etree.ElementTree
from collections.abc import Callable
from typing import Any, Final

import pytest

BANNER: Final[str] = "foundry: profile=default"
SCHEMES: Final[tuple[str, ...]] = ("http://", "https://")
CHILD_TEST: Final[str] = "tests/test_defaults.py"
CHILD_NODEID: Final[str] = f"{CHILD_TEST}::test_defaults"
PROBE_ADDOPTS: Final[str] = "-k __foundry_hygiene_probe_that_matches_nothing__"
PROBE_TIMEOUT: Final[str] = "99.5"
RECORD_KEYS: Final[frozenset[str]] = frozenset(
    {
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
    }
)
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
CHILD_SOURCE: Final[str] = (
    "def test_defaults(foundry_config):\n"
    '    assert foundry_config.profile == "default"\n'
    "    assert foundry_config.http.timeout == 10.0\n"
)


def _write_project(root: pathlib.Path) -> pathlib.Path:
    test_file = root / CHILD_TEST
    test_file.parent.mkdir(parents=True)
    test_file.write_text(CHILD_SOURCE, encoding="utf-8", newline="\n")
    present = [relative for relative in CONFIG_FREE_PATHS if (root / relative).exists()]
    assert present == [], f"{root} also holds {present}, which a pytest run would resolve"
    return root


def _results(project: pathlib.Path) -> list[dict[str, Any]]:
    path = project / "results.json"
    assert path.is_file(), f"the run wrote no results.json in its invocation directory {project}"
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), f"{path} holds {type(document).__name__}, not an array"
    assert all(isinstance(element, dict) for element in document), f"{path} holds a non-object"
    return document


def test_zero_config_run_reports_default_profile(
    tmp_path: pathlib.Path,
    installed_distribution: importlib.metadata.Distribution,
    repo_root: pathlib.Path,
    run_pytest: Callable[..., Any],
) -> None:
    assert installed_distribution.version, "the pytest-foundry distribution is not installed"
    project = _write_project(tmp_path / "proj")
    result = run_pytest("-q", cwd=project)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"a configuration-free run must exit 0:\n{output}"
    assert BANNER in result.stdout, f"no {BANNER!r}, so the plugin did not activate:\n{output}"
    assert "1 passed" in output, output
    document = _results(project)
    records = [element for element in document if element.get("record_type") == "result"]
    assert (len(document), len(records)) == (1, 1), f"{len(document)} element(s): {document}"
    record = records[0]
    missing = [key for key in sorted(RECORD_KEYS) if key not in record]
    assert missing == [], f"the record omits the AAP 0.4.6 keys {missing}: {record}"
    assert record["nodeid"] == CHILD_NODEID, f"the record names {record['nodeid']!r}"
    assert record["profile"] == "default", f"the record resolved {record['profile']!r}"
    assert record["outcome"] == "passed", f"the record's outcome is {record['outcome']!r}"
    assert record["attempts"] == 1, f"{record['attempts']!r} attempts with no retry asked for"
    for name in ("junit.xml", "report.html"):
        assert (project / name).is_file(), f"the run wrote no {name} beside results.json"
    cases = xml.etree.ElementTree.parse(project / "junit.xml").getroot().findall("testcase")
    assert [case.get("name") for case in cases] == ["test_defaults"], f"junit.xml holds {cases}"
    report = (project / "report.html").read_text(encoding="utf-8")
    assert [scheme for scheme in SCHEMES if scheme in report] == [], "report.html needs a network"
    assert "test_defaults" in report, "report.html does not list the test the run executed"
    run_id = str(record["run_id"])
    run_dir = project / "artifacts" / run_id
    assert run_dir.is_dir(), f"the run wrote no artifact directory {run_dir}"
    assert not (repo_root / "artifacts" / run_id).is_dir(), f"{run_id} captured at the repo root"
    root_line = f"foundry: run {run_id} artifacts at {run_dir.resolve().as_posix()}"
    assert root_line in result.stdout, f"the summary did not name {run_dir} as the artifact root"
    for entry in sorted(run_dir.iterdir()):
        assert entry.is_dir(), f"{entry} is not a per-test artifact directory"
        assert "/" not in entry.name and "::" not in entry.name, f"{entry.name!r} is unsanitised"
        assert (entry / "manifest.json").is_file(), f"{entry} holds no manifest.json"


def test_ambient_environment_does_not_leak(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, run_pytest: Callable[..., Any]
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", PROBE_ADDOPTS)
    monkeypatch.setenv("FOUNDRY_HTTP__TIMEOUT", PROBE_TIMEOUT)
    project = _write_project(tmp_path / "proj_leak")
    result = run_pytest("-q", cwd=project)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, f"the child inherited the outer process's environment:\n{output}"
    assert "1 passed" in output, output
    assert "deselected" not in output, f"the child applied PYTEST_ADDOPTS={PROBE_ADDOPTS!r}"
    assert BANNER in result.stdout, f"the child printed no {BANNER!r} under the sentinels"
