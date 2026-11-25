"""AAP 0.14.1 criterion 2: a suite is a Foundry suite with no configuration file at all.

The observation this module makes is the activation proof AAP 0.4.2 publishes. In a directory that
holds one test file and nothing else -- no ``foundry.toml``, no ``pyproject.toml`` and no
``pytest.ini``, with no plugin-enabling argument on the command line -- pytest exits 0, the terminal
summary carries ``foundry: profile=default``, ``foundry_config`` reports the field defaults of
AAP 0.4.5, and the three report files of AAP 0.4.6 appear beside the test. Activation is the
installed ``pytest11`` entry point and nothing else: each inner run is driven through ``pytester``,
so the plugin is exercised through pytest's own machinery rather than by calling one of its hooks by
hand, and every assertion reads a file the run produced or the banner itself.

Two facts about the harness shape the module. The ``pytest_plugins`` declaration that enables
``pytester`` lives in ``tests/conftest.py`` alone, because pytest aborts a run whose nested conftest
repeats it -- which is why this package has no ``conftest.py`` of its own. And ``clean_foundry_env``
removes any ``FOUNDRY_*`` variable an operator's shell exports, so a host environment can never turn
one of the asserted defaults into an override.

The subprocess half of the same criterion -- an installed console script driven in a child
interpreter -- belongs to ``tests/e2e/test_zero_config_subprocess.py``; the wheel, install and
example-project evidence belongs to ``tests/e2e/test_install.py`` and
``tests/e2e/test_example_project.py``. Nothing here duplicates either.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
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
"""The public surfaces this module drives through a real pytest session (AAP 0.14.1, criterion 13).

``tests/plugin/test_reachability.py`` reads this declaration, so each entry names a surface an inner
run above actually requests or a file it actually writes, never a name this docstring merely
mentions.
"""

_RUN_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")
"""A run id as AAP 0.4.3 publishes it: a UTC stamp and six lowercase hexadecimal digits."""

_NODEID_MODULE: Final[str] = "test_zero_config_defaults"
"""File name of the inner module whose one test asserts the field defaults."""

_NODEID_FUNCTION: Final[str] = "test_zero_config_reports_defaults"
"""Name of that inner test, which is also the node id ``results.json`` must record it under."""

_NODEID: Final[str] = f"{_NODEID_MODULE}.py::{_NODEID_FUNCTION}"
"""The inner test's node id as pytest writes it, built from the names ``makepyfile`` is given."""

_HIDDEN_NODEID_CHARACTERS: Final[tuple[str, ...]] = ("/", "::", "[")
"""What a node id may not leak into a directory name, because each would change the layout."""

_ARTIFACT_MODULE: Final[str] = "test_zero_config_artifacts"
"""File name of the inner module that reports the run identity and one artifact directory."""

_ARTIFACT_RECORD: Final[str] = "artifacts.json"
"""What that inner module writes for the outer assertions to read.

A run id, the run directory it named and the per-test directory ``dir_for`` answered with.
"""

_EVENTS_MODULE: Final[str] = "test_zero_config_events"
"""File name of the inner module run under a conftest that records the Foundry log."""

_EVENT_RECORD: Final[str] = "foundry-events.log"
"""The file that conftest writes, one ``LEVELNAME|message`` line per record."""

_NO_CONFIG_LINE: Final[str] = "DEBUG|foundry: no config file found"
"""The whole line AAP 0.4.7 pins for the zero-configuration state, with its level in front."""

_OPTION_SPELLINGS: Final[tuple[str, ...]] = (
    "--foundry-config",
    "--foundry-json-report",
    "--foundry-profile",
    "--foundry-record",
    "--foundry-replay",
    "--foundry-retries",
    "--foundry-timeout",
)
"""The named options AAP 0.4.4 publishes, in sorted order; ``--help`` must list every one."""

_REPORT_FILES: Final[tuple[str, ...]] = ("junit.xml", "report.html", "results.json")
"""The three report files AAP 0.4.6 places in the invocation directory, sorted."""

_NO_MATCH_KEYWORD: Final[str] = "no_such_test"
"""A ``-k`` expression no inner test matches, which empties a run of AAP 0.4.7 row 12's kind."""

_NO_TESTS_EXIT: Final[int] = 5
"""pytest 8.4.2's own no-tests-collected code, which is the value row 12's ``4`` is read as.

The row's number is not a code Foundry forces: AAP 0.4.4 forbids rewriting pytest's 3, 4 and 5, and
pytest answers a run emptied by a filter with 5, so the preserved code is the one asserted here.
"""

_DEFAULTS_MODULE: Final[str] = '''"""Assert the field defaults with nothing configured."""

import pathlib


def _plain(value: object) -> object:
    """Return an enum member's value, leaving any other value as it stands."""
    return getattr(value, "value", value)


def test_zero_config_reports_defaults(foundry_config, foundry_profile) -> None:
    """One passing test is the proof that every default asserted here held."""
    assert _plain(foundry_profile) == "default"
    assert _plain(foundry_config.profile) == "default"
    assert foundry_config.http.timeout == 10.0
    assert foundry_config.http.base_url is None
    assert foundry_config.execution.retries == 0
    assert foundry_config.execution.timeout == 60.0
    assert _plain(foundry_config.execution.quarantine_mode) == "report"
    assert _plain(foundry_config.cassette.mode) == "off"
    assert foundry_config.reporting.html is True
    assert foundry_config.reporting.json is True
    assert foundry_config.logging.capture_lines == 200
    assert _plain(foundry_config.logging.level) == "INFO"
    assert foundry_config.data.dir == pathlib.Path("testdata")
    assert foundry_config.artifacts.dir == pathlib.Path("artifacts")
    assert foundry_config.artifacts.keep_passed is False
    assert list(foundry_config.secrets.redact_keys) == [
        "authorization",
        "set-cookie",
        "cookie",
        "x-api-key",
    ]
'''
"""The inner test of criterion 2's default values, one assertion per documented key."""

_ARTIFACT_MODULE_SOURCE: Final[str] = '''"""Report the run id and one artifact directory."""

import json
import pathlib
import re


def test_zero_config_artifacts(foundry_artifacts, foundry_run_id, request) -> None:
    """The run id has its shape and dir_for answers inside artifacts/<run_id>/."""
    assert re.fullmatch(r"\\d{8}T\\d{6}-[0-9a-f]{6}", foundry_run_id)
    directory = foundry_artifacts.dir_for(request.node.nodeid)
    assert directory.is_dir()
    assert directory.parent.name == foundry_run_id
    assert directory.parent.parent.name == "artifacts"
    pathlib.Path(__file__).with_name("artifacts.json").write_text(
        json.dumps(
            {"dir": str(directory), "run_id": foundry_run_id, "nodeid": request.node.nodeid},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
'''
"""The inner test of the run identity and the per-test artifact directory (AAP 0.4.6)."""

_EVENTS_MODULE_SOURCE: Final[str] = '''"""A plain passing test that requests nothing."""


def test_zero_config_events() -> None:
    """Nothing is requested, so the record the outer test asserts on came from the bootstrap."""
    assert True
'''
"""The inner test of the no-configuration log line, deliberately fixture-free."""

_EVENTS_CONFTEST: Final[str] = '''"""Record Foundry records before the plugin resolves."""

import logging
import pathlib

import pytest

_EVENTS = pathlib.Path(__file__).with_name("foundry-events.log")
_HANDLER = None
_PRIOR_LEVEL = None


class _LineHandler(logging.Handler):
    """Append one ``LEVELNAME|message`` line per record, so the file is readable by a test."""

    def emit(self, record):
        """Write the record's level and rendered message."""
        with _EVENTS.open("a", encoding="utf-8") as stream:
            stream.write(f"{record.levelname}|{record.getMessage()}\\n")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Attach the handler and allow DEBUG through, before Foundry resolves anything.

    The ``foundry`` logger inherits the root logger's level, which is ``WARNING`` unless the host
    project says otherwise, so without this line the DEBUG record AAP 0.4.7 pins is dropped before
    it reaches any handler.
    """
    global _HANDLER, _PRIOR_LEVEL
    logger = logging.getLogger("foundry")
    _PRIOR_LEVEL = logger.level
    logger.setLevel(logging.DEBUG)
    _HANDLER = _LineHandler()
    logger.addHandler(_HANDLER)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    """Detach the handler and restore the level, so an in-process run leaks no logging state."""
    logger = logging.getLogger("foundry")
    if _HANDLER is not None:
        logger.removeHandler(_HANDLER)
    if _PRIOR_LEVEL is not None:
        logger.setLevel(_PRIOR_LEVEL)
'''
"""The conftest the logging test writes, which is a consuming project's own logging setup."""


def _read_json(path: Path) -> object:
    """Return the document ``path`` holds, decoded by the standard library's own reader.

    Read with :mod:`json` rather than through Foundry's reporting code on purpose: what is under
    assertion is a file a run produced, so the assertion must not travel back through the code that
    produced it.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def test_zero_config_activation_reports_default_profile(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A directory holding one test file and nothing else runs green and reports the defaults."""
    pytester.makepyfile(**{_NODEID_MODULE: _DEFAULTS_MODULE})
    # Checked once the test module exists, so these two lines rule out a configuration file the
    # harness wrote rather than one this test merely forgot to write itself.
    assert not (pytester.path / "foundry.toml").exists()
    assert not (pytester.path / "pyproject.toml").exists()
    # No argument is passed that loads the plugin by name: the installed entry point is the whole
    # activation path AAP 0.4.2 promises, and an option-free run is what proves it.
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*foundry: profile=default*"])


def test_run_id_and_artifact_root(pytester: pytest.Pytester, clean_foundry_env: None) -> None:
    """The session id has its published shape and names the directory its artifacts live under."""
    pytester.makepyfile(**{_ARTIFACT_MODULE: _ARTIFACT_MODULE_SOURCE})
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    recorded = _read_json(pytester.path / _ARTIFACT_RECORD)
    assert isinstance(recorded, dict)
    run_id = recorded["run_id"]
    assert isinstance(run_id, str)
    assert _RUN_ID_PATTERN.fullmatch(run_id) is not None
    assert recorded["nodeid"] == f"{_ARTIFACT_MODULE}.py::test_zero_config_artifacts"
    run_dir = pytester.path / "artifacts" / run_id
    assert run_dir.is_dir()
    directory = Path(recorded["dir"])
    assert directory.is_dir()
    assert directory.parent == run_dir
    names = sorted(entry.name for entry in run_dir.iterdir())
    assert directory.name in names
    for name in names:
        for forbidden in _HIDDEN_NODEID_CHARACTERS:
            assert forbidden not in name, f"{name!r} carries a node id separator {forbidden!r}"


def test_three_report_files_are_written_by_default(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """An unconfigured run writes results.json, junit.xml and report.html beside the test."""
    pytester.makepyfile(**{_NODEID_MODULE: _DEFAULTS_MODULE})
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    for name in _REPORT_FILES:
        assert (pytester.path / name).is_file(), f"{name} is missing from {pytester.path}"
    records = _read_json(pytester.path / "results.json")
    assert isinstance(records, list)
    assert len(records) == 1
    record = records[0]
    assert isinstance(record, dict)
    assert record["record_type"] == "result"
    assert record["nodeid"] == _NODEID
    assert record["profile"] == "default"
    assert record["outcome"] == "passed"
    assert record["attempts"] == 1
    run_id = record["run_id"]
    assert isinstance(run_id, str)
    assert _RUN_ID_PATTERN.fullmatch(run_id) is not None
    suite = ElementTree.parse(pytester.path / "junit.xml").getroot()
    assert suite.tag == "testsuite"
    assert len(suite.findall("testcase")) == 1
    report = (pytester.path / "report.html").read_text(encoding="utf-8")
    assert report.strip()
    assert run_id in report


def test_all_tests_deselected_writes_no_report(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A run whose only test is deselected writes no report file at all (AAP 0.4.7 row 12).

    The code the run ends with is pytest's own: AAP 0.4.4 forbids rewriting pytest's 3, 4 and 5, and
    pytest 8.4.2 answers a whole-run deselection with its no-tests-collected code 5, so row 12's "4"
    is read as pytest's own code rather than a number Foundry forces. AAP 0.4.6 reserves the empty
    document for a run that collected nothing, which is why no file of the three appears here.
    """
    pytester.makepyfile(**{_NODEID_MODULE: _DEFAULTS_MODULE})
    result = pytester.runpytest("-k", _NO_MATCH_KEYWORD)
    assert result.ret == _NO_TESTS_EXIT
    result.assert_outcomes(deselected=1)
    result.stdout.fnmatch_lines(["*1 deselected*"])
    written = sorted(name for name in _REPORT_FILES if (pytester.path / name).exists())
    assert not written, f"an all-deselected run writes no report, but {written} exist"


def test_matching_keyword_still_writes_every_report(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A ``-k`` filter that keeps the test still writes all three report files (AAP 0.4.6).

    The control for the case above: a guard that suppressed every filtered run would satisfy that
    case vacuously and fails this one.
    """
    pytester.makepyfile(**{_NODEID_MODULE: _DEFAULTS_MODULE})
    result = pytester.runpytest("-k", _NODEID_FUNCTION)
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    missing = sorted(name for name in _REPORT_FILES if not (pytester.path / name).is_file())
    assert not missing, f"{missing} are missing from {pytester.path}"


def test_collection_only_run_writes_no_report(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """A ``--collect-only`` session runs no test and writes no report file (AAP 0.4.6).

    The other report-less state of the session-finish guard: the run has no result to record, and
    pytest's own 0 for a collection-only session is preserved as AAP 0.4.4 requires.
    """
    pytester.makepyfile(**{_NODEID_MODULE: _DEFAULTS_MODULE})
    result = pytester.runpytest("--collect-only", "-q")
    assert result.ret == 0
    result.stdout.fnmatch_lines([f"*{_NODEID}*"])
    written = sorted(name for name in _REPORT_FILES if (pytester.path / name).exists())
    assert not written, f"a collection-only run writes no report, but {written} exist"


def test_option_group_is_registered(pytester: pytest.Pytester, clean_foundry_env: None) -> None:
    """``--help`` lists every named option of AAP 0.4.4, with no configuration file present."""
    result = pytester.runpytest("--help")
    assert result.ret == 0
    assert not (pytester.path / "foundry.toml").exists()
    listed = result.stdout.str()
    missing = [spelling for spelling in _OPTION_SPELLINGS if spelling not in listed]
    assert missing == [], f"--help does not list {missing}"


def test_no_config_file_logs_a_debug_line(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """The zero-configuration state is recorded at DEBUG on the Foundry logger (AAP 0.4.7)."""
    pytester.makeconftest(_EVENTS_CONFTEST)
    pytester.makepyfile(**{_EVENTS_MODULE: _EVENTS_MODULE_SOURCE})
    result = pytester.runpytest()
    assert result.ret == 0
    result.assert_outcomes(passed=1)
    lines = (pytester.path / _EVENT_RECORD).read_text(encoding="utf-8").splitlines()
    assert _NO_CONFIG_LINE in [line.strip() for line in lines]
