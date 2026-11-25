"""Zero-config activation through ``pytester``: exit 0, the ``foundry: profile=default`` banner,
every published field default, the run tree and the three report files, with no plugin flag passed.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import pytest

REACHES: tuple[str, ...] = (
    "fixture:foundry_config",
    "fixture:foundry_profile",
    "fixture:foundry_run_id",
    "fixture:foundry_artifacts",
    "format:results.json",
    "format:junit.xml",
    "format:report.html",
)

_MODULE: Final[str] = "test_activation"
_RUN_ID: Final[re.Pattern[str]] = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")
_REPORTS: Final[tuple[str, ...]] = ("junit.xml", "report.html", "results.json")
_OPTIONS: Final[tuple[str, ...]] = tuple(
    f"--foundry-{name}"
    for name in ("config", "json-report", "profile", "record", "replay", "retries", "timeout")
)

_PROJECT: Final[str] = """import json
import pathlib
import re


def test_zero_config_reports_defaults(foundry_config, foundry_profile):
    c = foundry_config
    assert foundry_profile == "default" and c.profile == "default"
    assert c.http.timeout == 10.0 and c.http.base_url is None
    assert c.execution.retries == 0 and c.execution.timeout == 60.0
    assert c.execution.quarantine_mode == "report" and c.cassette.mode == "off"
    assert c.reporting.html is True and c.reporting.json is True
    assert c.logging.capture_lines == 200 and c.logging.level == "INFO"
    assert c.data.dir == pathlib.Path("testdata")
    assert c.artifacts.dir == pathlib.Path("artifacts")
    assert c.artifacts.keep_passed is False
    assert list(c.secrets.redact_keys) == ["authorization", "set-cookie", "cookie", "x-api-key"]

def test_zero_config_artifacts(foundry_artifacts, foundry_run_id, request):
    assert re.fullmatch(r"\\d{8}T\\d{6}-[0-9a-f]{6}", foundry_run_id)
    directory = foundry_artifacts.dir_for(request.node.nodeid)
    assert directory.is_dir()
    assert directory.parent.name == foundry_run_id
    assert directory.parent.parent.name == "artifacts"
    facts = {"dir": str(directory), "run_id": foundry_run_id, "nodeid": request.node.nodeid}
    out = pathlib.Path(__file__).with_name("artifacts.json")
    out.write_text(json.dumps(facts, sort_keys=True), encoding="utf-8")
"""

_EVENTS_CONFTEST: Final[str] = """import logging
import pathlib

import pytest

EVENTS = pathlib.Path(__file__).with_name("foundry-events.log")
LOG = logging.getLogger("foundry")
PRIOR = LOG.level

class _Lines(logging.Handler):
    def emit(self, record):
        with EVENTS.open("a", encoding="utf-8") as stream:
            stream.write(f"{record.levelname}|{record.getMessage()}\\n")

HANDLER = _Lines()
LOG.setLevel(logging.DEBUG)
LOG.addHandler(HANDLER)

@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    LOG.removeHandler(HANDLER)
    LOG.setLevel(PRIOR)
"""

_NO_MATCH: Final[str] = "no_such_test"
_NO_TESTS_EXIT: Final[int] = 5
_HIDDEN: Final[tuple[str, ...]] = ("/", "::", "[")

_PUBLISHED: Final[str] = (
    "import json, pathlib\n\n\n"
    "def test_zero_config_published_write(foundry_artifacts):\n"
    '    reference = foundry_artifacts.write("stdout", None, "published write")\n'
    '    assert reference is not None and reference.kind == "stdout", reference\n'
    '    assert pathlib.PurePosixPath(reference.path).name == "stdout.log", reference\n'
    '    found = sorted(pathlib.Path("artifacts").glob(f"*/{reference.path}"))\n'
    '    assert [item.read_text(encoding="utf-8") for item in found] == ["published write"]\n'
    "    manifest = foundry_artifacts.manifest\n"
    "    assert reference in manifest, manifest\n"
    '    facts = {"path": reference.path, "directory": found[0].parent.name,\n'
    '             "manifest": [entry.path for entry in manifest]}\n'
    '    pathlib.Path(__file__).with_name("published-write.json").write_text(\n'
    '        json.dumps(facts, sort_keys=True), encoding="utf-8")\n'
)


def test_activation_runs_green_and_reports_the_defaults(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makeconftest(_EVENTS_CONFTEST)
    pytester.makepyfile(**{_MODULE: _PROJECT})
    # Checked once the test module exists, so neither line can pass by finding an empty directory.
    assert not (pytester.path / "foundry.toml").exists()
    assert not (pytester.path / "pyproject.toml").exists()
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["*foundry: profile=default*"])
    for name in _REPORTS:
        assert (pytester.path / name).is_file(), name
    records = json.loads((pytester.path / "results.json").read_text(encoding="utf-8"))
    assert isinstance(records, list) and len(records) == 2
    record = next(item for item in records if item["nodeid"].endswith("reports_defaults"))
    assert record["record_type"] == "result"
    assert record["nodeid"] == f"{_MODULE}.py::test_zero_config_reports_defaults"
    assert record["profile"] == "default"
    assert record["outcome"] == "passed" and record["attempts"] == 1
    run_id = record["run_id"]
    assert isinstance(run_id, str) and _RUN_ID.fullmatch(run_id) is not None
    suite = ElementTree.parse(pytester.path / "junit.xml").getroot()
    assert suite.tag == "testsuite" and len(suite.findall("testcase")) == 2
    report = (pytester.path / "report.html").read_text(encoding="utf-8")
    assert report.strip() and run_id in report
    facts = json.loads((pytester.path / "artifacts.json").read_text(encoding="utf-8"))
    assert (
        isinstance(facts, dict) and facts["nodeid"] == f"{_MODULE}.py::test_zero_config_artifacts"
    )
    artifact_run_id = facts["run_id"]
    assert isinstance(artifact_run_id, str) and _RUN_ID.fullmatch(artifact_run_id) is not None
    run_dir = pytester.path / "artifacts" / artifact_run_id
    directory = Path(facts["dir"])
    assert run_dir.is_dir() and directory.parent == run_dir
    names = sorted(entry.name for entry in run_dir.iterdir())
    assert directory.name in names
    # A node id separator would change the run tree's layout, so none may reach a directory name.
    assert all(not any(char in name for char in ("/", "::", "[")) for name in names)
    # The conftest is the consuming project's own logging setup; the pinned DEBUG line needs it.
    lines = (pytester.path / "foundry-events.log").read_text(encoding="utf-8").splitlines()
    assert "DEBUG|foundry: no config file found" in [line.strip() for line in lines]


def test_option_group_is_registered(pytester: pytest.Pytester, clean_foundry_env: None) -> None:
    result = pytester.runpytest("--help")
    assert result.ret == 0
    assert not (pytester.path / "foundry.toml").exists()
    listed = result.stdout.str()
    missing = [name for name in _OPTIONS if name not in listed]
    assert missing == [], f"--help does not list {missing}"


@pytest.mark.parametrize(
    ("argv", "code", "outcomes", "line", "reports"),
    [
        (("-k", _NO_MATCH), _NO_TESTS_EXIT, {"deselected": 2}, "*2 deselected*", False),
        (("-k", "reports_defaults"), 0, {"passed": 1, "deselected": 1}, None, True),
        (("--collect-only", "-q"), 0, {"tests": 2}, "*::test_zero_config_reports_defaults*", False),
    ],
    ids=["all-deselected", "keyword", "collect-only"],
)
def test_only_a_run_that_runs_a_test_writes_a_report(
    pytester: pytest.Pytester,
    clean_foundry_env: None,
    argv: tuple[str, ...],
    code: int,
    outcomes: Mapping[str, int],
    line: str | None,
    reports: bool,
) -> None:
    # AAP 0.4.4 forbids rewriting pytest's own 3, 4 and 5, so a run emptied by a filter keeps 5.
    pytester.makepyfile(**{_MODULE: _PROJECT})
    result = pytester.runpytest(*argv)
    assert result.ret == code and result.parseoutcomes() == outcomes
    if line is not None:
        result.stdout.fnmatch_lines([line])
    written = sorted(name for name in _REPORTS if (pytester.path / name).exists())
    assert bool(written) is reports, written


def test_the_published_write_and_manifest_members_work(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_published=_PUBLISHED)
    result = pytester.runpytest("--foundry-artifacts.keep_passed", "true")
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    recorded = json.loads((pytester.path / "published-write.json").read_text(encoding="utf-8"))
    relative = recorded["path"]
    assert Path(relative).name == "stdout.log" and recorded["manifest"] == [relative]
    record = json.loads((pytester.path / "results.json").read_text(encoding="utf-8"))[0]
    written = pytester.path / "artifacts" / record["run_id"] / relative
    assert written.is_file(), f"the write member recorded {relative!r} but wrote no such file"
    assert written.parent.name == recorded["directory"] and "published" in written.parent.name
    assert not [char for char in _HIDDEN if char in written.parent.name]
    listed = [{"kind": item["kind"], "path": item["path"]} for item in record["artifacts"]]
    assert {"kind": "stdout", "path": relative} in listed, listed
