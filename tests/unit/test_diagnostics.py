"""Unit tests for ``foundry.diagnostics``: the six doctor probes and the report they build.

AAP 0.4.4 fixes what ``foundry doctor`` answers -- ``0`` when every required probe passes, ``2``
when a required probe fails, and no change at all for a warning -- and AAP 0.7.1/0.7.3 fix the rows
and their words. Pinned here, probe by probe and then through
:func:`~foundry.diagnostics.checks.run_checks`:

* the six rows appear in the published order ``interpreter``, ``plugin``, ``configuration``,
  ``artifacts``, ``cassettes``, ``socket_guard``, each with one of the three status words and a
  non-empty, specific detail;
* only a ``FAIL`` on a required row clears the report's ``ok`` -- the value that maps to the exit
  code -- so a ``WARN`` never fails the command;
* each probe is held to its own rule: the interpreter's platform and version policy, both halves of
  the ``pytest11`` registration, the resolved profile or the configuration fault's own message, the
  two directory states, and the socket-guard row.

Every probe is handed a path under ``tmp_path``, so no test writes inside the repository, and
nothing opens a socket or sleeps. Two contracts are judged through the seam the package really
reads: the interpreter policy through the ``sys.platform``, ``sys.version_info`` and
``platform.python_implementation()`` that
:meth:`~foundry.diagnostics.report.InterpreterInfo.current` reads, rather than through
:func:`platform.system`, which the probe never calls; and the socket guard through the
``FOUNDRY_SOCKET_GUARD`` and ``FOUNDRY_NON_LOOPBACK_ATTEMPTS`` names ``tests/conftest.py``'s guard
publishes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from importlib.metadata import entry_points
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

from foundry.config import resolve
from foundry.core.exceptions import FoundryError
from foundry.diagnostics.checks import (
    PROBE_ARTIFACTS,
    PROBE_CASSETTES,
    PROBE_CONFIGURATION,
    PROBE_INTERPRETER,
    PROBE_PLUGIN,
    REQUIRED_PROBES,
    check_artifacts_dir,
    check_cassette_dir,
    check_configuration,
    check_interpreter,
    check_plugin_registered,
    run_checks,
)
from foundry.diagnostics.report import (
    DoctorReport,
    InterpreterInfo,
    ProbeResult,
    ProbeStatus,
)
from foundry.diagnostics.socket_guard import (
    PROBE_NAME,
    SocketGuardState,
    check_socket_guard,
    detect_socket_guard,
)

_STATUS_WORDS: Final[frozenset[str]] = frozenset({"pass", "warn", "fail"})
"""The three words AAP 0.7.4 prints in the ``Status`` column and AAP 0.7.3 serialises."""

_PROBE_ORDER: Final[tuple[str, ...]] = (
    PROBE_INTERPRETER,
    PROBE_PLUGIN,
    PROBE_CONFIGURATION,
    PROBE_ARTIFACTS,
    PROBE_CASSETTES,
    PROBE_NAME,
)
"""The fixed row order of one doctor report, which the table and the JSON array both follow."""

_ENTRY_POINT_DETAIL: Final[str] = 'pytest11 entry point foundry = "foundry.plugin"'
"""The registration every plugin-probe detail names (AAP 0.4.2)."""

_ENTRY_POINT_NAME: Final[str] = "foundry"
_ENTRY_POINT_TARGET: Final[str] = "foundry.plugin"
"""The registration AAP 0.4.2 publishes, as the entry-point name and the module it must import."""

_SUPPORTED: Final[InterpreterInfo] = InterpreterInfo(
    implementation="CPython", version="3.12.4", platform="linux", supported=True
)
_UNSUPPORTED: Final[InterpreterInfo] = InterpreterInfo(
    implementation="CPython", version="3.9.18", platform="linux", supported=False
)
_WINDOWS: Final[InterpreterInfo] = InterpreterInfo(
    implementation="CPython", version="3.12.4", platform="win32", supported=True
)


# --- Builders -------------------------------------------------------------


def _report(*checks: ProbeResult) -> DoctorReport:
    """Return a report holding ``checks`` in order, over a supported interpreter."""
    return DoctorReport(interpreter=_SUPPORTED, checks=checks)


def _row(
    name: str, status: ProbeStatus, *, required: bool = True, detail: str = "d"
) -> ProbeResult:
    """Return one probe row with a placeholder detail, for the verdict arithmetic."""
    return ProbeResult(name=name, status=status, detail=detail, required=required)


def _version_info(major: int, minor: int, micro: int) -> SimpleNamespace:
    """Return a stand-in for :data:`sys.version_info` carrying the three fields the probe reads."""
    return SimpleNamespace(major=major, minor=minor, micro=micro)


# --- The report (AAP 0.4.4, AAP 0.7.3) -----------------------------------


def test_the_status_vocabulary_is_the_published_three_words() -> None:
    """The three words are the whole vocabulary, asserted from both sides (AAP 0.7.3/0.7.4)."""
    assert {status.value for status in ProbeStatus} == set(_STATUS_WORDS)
    assert ProbeStatus("pass") is ProbeStatus.PASS
    assert ProbeStatus("warn") is ProbeStatus.WARN
    assert ProbeStatus("fail") is ProbeStatus.FAIL


def test_run_checks_reports_the_six_probes_in_the_published_order(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """Six rows, in order, each with a published status word and a detail, directory work in tmp."""
    report = run_checks(cwd=tmp_path)
    assert report.interpreter == InterpreterInfo.current()
    assert [row.name for row in report.checks] == list(_PROBE_ORDER)
    assert {row.status.value for row in report.checks} <= _STATUS_WORDS
    assert all(row.detail.strip() for row in report.checks)
    assert [row.required for row in report.checks] == [True] * 5 + [False]
    assert _PROBE_ORDER[:5] == REQUIRED_PROBES
    # The artifact probe creates the directory it inspects; nothing else appears beside it.
    assert (tmp_path / "artifacts").is_dir()
    assert sorted(path.name for path in tmp_path.iterdir() if path.is_dir()) == ["artifacts"]
    # AAP 0.4.4: the exit code is cleared by a required failure and by nothing else.
    assert report.ok is (report.failed == ())


def test_run_checks_reports_the_configuration_it_was_given(tmp_path: Path) -> None:
    """A supplied configuration is reported on as given, and its directories are the ones probed."""
    config = resolve(environ={}, cwd=tmp_path)
    report = run_checks(config=config, cwd=tmp_path)
    configuration = next(row for row in report.checks if row.name == PROBE_CONFIGURATION)
    assert configuration.status is ProbeStatus.PASS
    assert configuration.detail == "configuration valid (profile=default)"
    artifacts = next(row for row in report.checks if row.name == PROBE_ARTIFACTS)
    assert artifacts.status is ProbeStatus.PASS
    assert artifacts.detail == f"{(tmp_path / 'artifacts').resolve()} writable"


def test_run_checks_isolates_a_probe_that_raises(
    tmp_path: Path,
    clean_foundry_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A probe that raises becomes one ``FAIL`` row instead of a traceback (AAP 0.1.2)."""

    def explode(*_args: object, **_kwargs: object) -> ProbeResult:
        """Stand in for a probe that cannot do its work."""
        raise FoundryError("the probe could not run")

    caplog.set_level(logging.WARNING, logger="foundry")
    monkeypatch.setattr("foundry.diagnostics.checks.check_cassette_dir", explode)
    report = run_checks(cwd=tmp_path)
    cassettes = next(row for row in report.checks if row.name == PROBE_CASSETTES)
    assert cassettes.status is ProbeStatus.FAIL
    assert cassettes.detail == "probe failed: FoundryError"
    assert len(report.checks) == len(_PROBE_ORDER)
    # AAP 0.1.2: the fault is reported once, on the foundry logger, and it is the only record the
    # probe adds -- a diagnostic run must not turn one broken probe into a wall of warnings.
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].name == "foundry"
    assert PROBE_CASSETTES in warnings[0].getMessage()
    assert "FoundryError" in warnings[0].getMessage()
    assert report.ok is False


def test_a_warning_never_clears_the_verdict_and_a_required_failure_does() -> None:
    """Only a ``FAIL`` on a required row makes the report not-ok (AAP 0.4.4)."""
    assert _report(_row("a", ProbeStatus.WARN), _row("b", ProbeStatus.FAIL, required=False)).ok
    assert not _report(_row("a", ProbeStatus.PASS), _row("b", ProbeStatus.FAIL)).ok
    mixed = _report(
        _row("a", ProbeStatus.WARN),
        _row("b", ProbeStatus.FAIL, required=False),
        _row("c", ProbeStatus.PASS),
    )
    assert mixed.ok is True
    assert mixed.failed == ()
    assert mixed.as_document()["ok"] is True


# --- The plugin row against the metadata this environment publishes (AAP 0.4.2) ---


def _published_entry_point_targets() -> tuple[str, ...]:
    """Return the ``pytest11`` targets this environment publishes under the name ``foundry``.

    Read from :mod:`importlib.metadata`, the source the probe consults when handed no pairs, so the
    expectation follows the environment: a source checkout publishes nothing, an installed
    ``pytest-foundry`` publishes ``foundry.plugin``.
    """
    return tuple(
        sorted(
            str(point.value)
            for point in entry_points(group="pytest11")
            if point.name == _ENTRY_POINT_NAME
        )
    )


def _entry_point_registered() -> bool:
    """Return whether the published metadata carries the registration AAP 0.4.2 names."""
    return any(
        value.split(":", 1)[0].strip() == _ENTRY_POINT_TARGET
        for value in _published_entry_point_targets()
    )


def _entry_point_module_available() -> bool:
    """Return whether ``foundry.plugin`` resolves here, without importing it."""
    try:
        return find_spec(_ENTRY_POINT_TARGET) is not None
    except (ImportError, ValueError):
        return False


def test_run_checks_reports_the_pytest_entry_point_this_environment_publishes(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """The plugin row follows the ``pytest11`` metadata this environment actually publishes."""
    registered = _entry_point_registered()
    available = _entry_point_module_available()
    report = run_checks(cwd=tmp_path)
    plugin_row = next(row for row in report.checks if row.name == PROBE_PLUGIN)
    if registered and available:
        assert plugin_row.status is ProbeStatus.PASS
        assert plugin_row.detail == f"{_ENTRY_POINT_DETAIL} registered"
    elif registered:
        assert plugin_row.status is ProbeStatus.FAIL
        assert plugin_row.detail == (
            f"{_ENTRY_POINT_DETAIL} is registered but {_ENTRY_POINT_TARGET} is not importable"
        )
    else:
        assert plugin_row.status is ProbeStatus.FAIL
        assert plugin_row.detail.startswith(f"{_ENTRY_POINT_DETAIL} is not registered")
    assert (PROBE_PLUGIN in [row.name for row in report.failed]) is not (registered and available)
    assert report.ok is (report.failed == ())


def test_run_checks_reports_a_registered_entry_point_as_passed(
    tmp_path: Path, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installed world's verdict, from the pairs ``run_checks`` already accepts (AAP 0.4.2)."""
    monkeypatch.setattr("foundry.diagnostics.checks._module_importable", lambda _target: True)
    report = run_checks(
        config=resolve(environ={}, cwd=tmp_path),
        cwd=tmp_path,
        discovered=[(_ENTRY_POINT_NAME, _ENTRY_POINT_TARGET)],
    )
    plugin_row = next(row for row in report.checks if row.name == PROBE_PLUGIN)
    assert plugin_row.status is ProbeStatus.PASS
    assert plugin_row.detail == f"{_ENTRY_POINT_DETAIL} registered"
    assert plugin_row.required is True
    assert report.failed == ()
    assert report.ok is (report.failed == ())


# --- check_interpreter (AAP 0.4.1) --------------------------------------


@pytest.mark.parametrize(
    ("info", "status"),
    [
        (_SUPPORTED, ProbeStatus.PASS),
        (_UNSUPPORTED, ProbeStatus.FAIL),
        (_WINDOWS, ProbeStatus.WARN),
    ],
)
def test_check_interpreter_judges_the_version_and_the_platform(
    info: InterpreterInfo, status: ProbeStatus
) -> None:
    """A supported pair passes, an unsupported interpreter fails and another platform warns."""
    row = check_interpreter(info)
    assert row.name == PROBE_INTERPRETER
    assert row.status is status
    assert row.required is True
    assert f"{info.implementation} {info.version} on {info.platform}" in row.detail


def test_check_interpreter_describes_the_running_interpreter() -> None:
    """With no argument the probe reports the interpreter it is running on, and passes here."""
    row = check_interpreter()
    current = InterpreterInfo.current()
    assert row.status is ProbeStatus.PASS
    assert row.detail == f"{current.implementation} {current.version} on {current.platform}"
    assert "CPython 3.11-3.13" not in row.detail


@pytest.mark.parametrize(
    ("version_info", "platform_name", "status", "fragment"),
    [
        (_version_info(3, 12, 4), "win32", ProbeStatus.WARN, "on win32 is unsupported"),
        (_version_info(3, 9, 18), "linux", ProbeStatus.FAIL, "requires CPython 3.11-3.13"),
        (_version_info(3, 14, 0), "darwin", ProbeStatus.FAIL, "requires CPython 3.11-3.13"),
    ],
)
def test_check_interpreter_judges_the_platform_and_range_it_reads(
    monkeypatch: pytest.MonkeyPatch,
    version_info: SimpleNamespace,
    platform_name: str,
    status: ProbeStatus,
    fragment: str,
) -> None:
    """A Windows-like platform warns and an unsupported version fails (AAP 0.4.1)."""
    monkeypatch.setattr(
        "foundry.diagnostics.report.sys",
        SimpleNamespace(platform=platform_name, version_info=version_info),
    )
    row = check_interpreter()
    assert row.name == PROBE_INTERPRETER
    assert row.status is status
    assert fragment in row.detail
    assert f"on {platform_name}" in row.detail
    assert row.required is True


def test_check_interpreter_fails_on_an_implementation_other_than_cpython(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An implementation outside the range is a failure naming the one that is supported."""
    monkeypatch.setattr(
        "foundry.diagnostics.report.platform",
        SimpleNamespace(python_implementation=lambda: "PyPy"),
    )
    row = check_interpreter()
    assert row.status is ProbeStatus.FAIL
    assert "PyPy" in row.detail
    assert "requires CPython 3.11-3.13" in row.detail


# --- check_plugin_registered (AAP 0.4.2) --------------------------------


def test_check_plugin_registered_passes_when_the_entry_point_and_the_module_are_present() -> None:
    """A registered ``foundry = foundry.plugin`` entry point with an importable module passes."""
    row = check_plugin_registered(discovered=[("foundry", "foundry.plugin")], module_available=True)
    assert row.status is ProbeStatus.PASS
    assert row.detail == f"{_ENTRY_POINT_DETAIL} registered"


def test_check_plugin_registered_accepts_an_entry_point_naming_an_attribute() -> None:
    """A ``module:attribute`` target still names the module the probe has to import."""
    row = check_plugin_registered(
        discovered=[("foundry", "foundry.plugin:configure")], module_available=True
    )
    assert row.status is ProbeStatus.PASS


def test_check_plugin_registered_fails_when_the_module_cannot_be_imported() -> None:
    """A declared entry point whose module is missing would activate nothing, so it fails."""
    row = check_plugin_registered(
        discovered=[("foundry", "foundry.plugin")], module_available=False
    )
    assert row.status is ProbeStatus.FAIL
    assert row.detail == f"{_ENTRY_POINT_DETAIL} is registered but foundry.plugin is not importable"


@pytest.mark.parametrize(
    ("discovered", "found"),
    [((), "none"), ([("pytest_cov", "pytest_cov.plugin")], "pytest_cov")],
)
def test_check_plugin_registered_fails_naming_what_it_did_find(
    discovered: list[tuple[str, str]], found: str
) -> None:
    """An unregistered entry point names the group, the target and the names that do exist."""
    row = check_plugin_registered(discovered=discovered, module_available=True)
    assert row.status is ProbeStatus.FAIL
    assert row.detail == f"{_ENTRY_POINT_DETAIL} is not registered; names found: {found}"


# --- check_configuration (AAP 0.4.5, AAP 0.7.1) -------------------------


def test_check_configuration_reports_the_resolved_profile(tmp_path: Path) -> None:
    """A resolved configuration passes with the message ``config validate`` also prints."""
    config = resolve(environ={}, cwd=tmp_path)
    assert config.profile.value == "default"
    row = check_configuration(config)
    assert row.name == PROBE_CONFIGURATION
    assert row.status is ProbeStatus.PASS
    assert row.detail == "configuration valid (profile=default)"
    assert row.required is True


def test_check_configuration_reports_the_fault_it_cannot_resolve(
    tmp_path: Path, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable value reaches the row as the configuration error's own message."""
    monkeypatch.setenv("FOUNDRY_HTTP__TIMEOUT", "-1")
    monkeypatch.chdir(tmp_path)
    row = check_configuration()
    assert row.status is ProbeStatus.FAIL
    assert "http.timeout" in row.detail
    # AAP 0.4.5: the message names the accepted range, not merely the rejected value.
    assert "0 < t <= 300" in row.detail
    assert row.required is True


def test_check_configuration_names_an_unknown_key(
    tmp_path: Path, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key no layer may set is reported by name, which is what makes it fixable."""
    monkeypatch.setenv("FOUNDRY_HTTP__TIMEOUTT", "1.0")
    monkeypatch.chdir(tmp_path)
    row = check_configuration()
    assert row.status is ProbeStatus.FAIL
    assert "http.timeoutt" in row.detail
    assert row.required is True


# --- check_artifacts_dir (AAP 0.4.7, AAP 0.7.1) ------------------------


def test_check_artifacts_dir_creates_and_proves_the_directory_writable(tmp_path: Path) -> None:
    """The probe creates the directory, writes one file into it and removes that file again."""
    target = tmp_path / "artifacts"
    row = check_artifacts_dir(directory=target)
    assert row.status is ProbeStatus.PASS
    assert row.detail == f"{target.resolve()} writable"
    assert target.is_dir()
    assert list(target.iterdir()) == []


def test_check_artifacts_dir_resolves_a_relative_path_against_the_given_cwd(tmp_path: Path) -> None:
    """A relative ``artifacts.dir`` resolves against the given cwd, not the process's."""
    row = check_artifacts_dir(directory=Path("artifacts"), cwd=tmp_path)
    assert row.status is ProbeStatus.PASS
    assert row.detail == f"{(tmp_path / 'artifacts').resolve()} writable"


def test_check_artifacts_dir_fails_when_the_path_cannot_be_a_directory(tmp_path: Path) -> None:
    """A path already taken by a file is reported as the operating system's own refusal."""
    target = tmp_path / "artifacts"
    target.write_text("not a directory", encoding="utf-8")
    row = check_artifacts_dir(directory=target)
    assert row.status is ProbeStatus.FAIL
    assert row.detail.startswith(f"{target.resolve()}: ")


def test_check_artifacts_dir_warns_when_no_directory_can_be_determined() -> None:
    """Without a configuration and without an explicit path the probe could not run."""
    row = check_artifacts_dir()
    assert row.status is ProbeStatus.WARN
    assert row.detail == "not checked: configuration unavailable"


def test_check_artifacts_dir_creates_a_missing_parent_chain(tmp_path: Path) -> None:
    """A configured path whose parents do not exist yet is created, and the row reports it."""
    target = tmp_path / "one" / "two" / "artifacts"
    assert not target.parent.exists()
    row = check_artifacts_dir(directory=target)
    assert row.status is ProbeStatus.PASS
    assert row.detail == f"{target.resolve()} writable"
    assert target.is_dir()


@pytest.mark.skipif(
    os.geteuid() == 0, reason="a privileged user writes into a directory whose mode forbids it"
)
def test_check_artifacts_dir_fails_on_a_directory_it_may_not_write(tmp_path: Path) -> None:
    """A directory the process may not write into is a ``FAIL`` naming the path, not a traceback."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        row = check_artifacts_dir(directory=locked)
    finally:
        locked.chmod(0o700)
    assert row.status is ProbeStatus.FAIL
    assert row.detail.startswith(f"{locked.resolve()}: ")


# --- check_cassette_dir (assumption A8, AAP 0.7.1) ---------------------


def test_check_cassette_dir_warns_when_no_cassette_directory_exists(tmp_path: Path) -> None:
    """A project that never records owns no cassette directory, and that is a warning."""
    row = check_cassette_dir(directory=tmp_path / "cassettes")
    assert row.status is ProbeStatus.WARN
    assert row.detail == f"{(tmp_path / 'cassettes').resolve()} not created yet"
    assert not (tmp_path / "cassettes").exists()


def test_check_cassette_dir_counts_the_documents_it_holds(tmp_path: Path) -> None:
    """An existing directory passes and reports how many cassette files it holds."""
    directory = tmp_path / "cassettes"
    directory.mkdir()
    empty = check_cassette_dir(directory=directory)
    assert empty.status is ProbeStatus.PASS
    assert re.search(r"\(0 cassette file\(s\)\)$", empty.detail) is not None
    (directory / "one.json").write_text("[]", encoding="utf-8")
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")
    one = check_cassette_dir(directory=directory)
    assert one.status is ProbeStatus.PASS
    assert re.search(r"\(1 cassette file\(s\)\)$", one.detail) is not None


def test_check_cassette_dir_fails_when_the_path_is_not_a_directory(tmp_path: Path) -> None:
    """A path taken by a file could never hold a recording, so recording there is a failure."""
    target = tmp_path / "cassettes"
    target.write_text("{}", encoding="utf-8")
    row = check_cassette_dir(directory=target)
    assert row.status is ProbeStatus.FAIL
    assert row.detail == f"{target.resolve()} is not a directory"


def test_check_cassette_dir_warns_when_no_directory_can_be_determined() -> None:
    """Without a configuration and without an explicit path the probe could not run."""
    row = check_cassette_dir()
    assert row.status is ProbeStatus.WARN
    assert row.detail == "not checked: configuration unavailable"


# --- The socket-guard probe (AAP 0.14.3) -------------------------------


def test_the_socket_guard_probe_reads_the_state_the_suite_publishes(
    socket_guard: object,
) -> None:
    """The guard ``tests/conftest.py`` installs is found, and its live count decides the row."""
    # The fixture's own guard type lives in tests/conftest.py, which a test module cannot import by
    # name under every import mode, so the two members it publishes are read off the object.
    count: int = socket_guard.non_loopback_attempts
    state = detect_socket_guard()
    assert state.installed is True
    assert state.source == "conftest"
    assert state.non_loopback_attempts == count
    row = check_socket_guard(state)
    assert row.name == PROBE_NAME
    assert row.required is False
    assert row.status is (ProbeStatus.PASS if count == 0 else ProbeStatus.FAIL)
    assert row.detail == state.describe()


def test_no_guard_installed_is_a_warning_that_cannot_fail_the_command() -> None:
    """A project without a guard is an ordinary project: the row warns and stays optional."""
    assert detect_socket_guard({}) == SocketGuardState.absent()
    row = check_socket_guard(SocketGuardState.absent())
    assert row.status is ProbeStatus.WARN
    assert row.detail == "no socket guard installed"
    assert row.required is False


def test_detect_socket_guard_reads_a_published_module_and_its_count() -> None:
    """A module publishing the two names is found, and its count is read from either holder."""
    on_module = SimpleNamespace(FOUNDRY_SOCKET_GUARD=True, FOUNDRY_NON_LOOPBACK_ATTEMPTS=2)
    state = detect_socket_guard({"a_guard": on_module})
    assert state == SocketGuardState(installed=True, non_loopback_attempts=2, source="a_guard")
    guard = SimpleNamespace(FOUNDRY_NON_LOOPBACK_ATTEMPTS=4)
    on_object = detect_socket_guard({"b_guard": SimpleNamespace(FOUNDRY_SOCKET_GUARD=guard)})
    assert on_object.non_loopback_attempts == 4
    assert on_object.source == "b_guard"


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (
            SocketGuardState(installed=True, non_loopback_attempts=None, source="m"),
            ProbeStatus.WARN,
        ),
        (SocketGuardState(installed=True, non_loopback_attempts=0, source="m"), ProbeStatus.PASS),
        (SocketGuardState(installed=True, non_loopback_attempts=1, source="m"), ProbeStatus.FAIL),
    ],
)
def test_the_socket_guard_row_reports_what_the_count_says(
    state: SocketGuardState, status: ProbeStatus
) -> None:
    """An unmeasured guard warns, a zero count passes and any attempt fails, all optional."""
    row = check_socket_guard(state)
    assert row.status is status
    assert row.detail == state.describe()
    assert row.required is False
    if status is ProbeStatus.FAIL:
        assert f"{state.non_loopback_attempts} non-loopback connection attempt(s)" in row.detail


def test_a_guard_publishing_no_count_reads_as_installed_and_unmeasured() -> None:
    """The two halves of the convention are separate: announcing is not measuring."""
    state = detect_socket_guard({"guard": SimpleNamespace(FOUNDRY_SOCKET_GUARD=object())})
    assert state == SocketGuardState(installed=True, non_loopback_attempts=None, source="guard")
    assert check_socket_guard(state).status is ProbeStatus.WARN


def test_run_checks_reports_the_guard_state_it_was_given(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """The optional row carries the supplied state and stays out of the verdict (AAP 0.4.4)."""
    state = SocketGuardState(installed=True, non_loopback_attempts=3, source="a_guard")
    report = run_checks(config=resolve(environ={}, cwd=tmp_path), cwd=tmp_path, guard_state=state)
    row = next(check for check in report.checks if check.name == PROBE_NAME)
    assert row.status is ProbeStatus.FAIL
    assert row.detail == state.describe()
    assert row.required is False
    assert PROBE_NAME not in [check.name for check in report.failed]
    assert report.ok is (report.failed == ())


# --- A document a caller can serialise ---------------------------------


def test_the_report_document_holds_the_published_members(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """``ok`` and the three documented ``checks`` keys are what a reader of ``--json`` sees."""
    report = run_checks(config=resolve(environ={}, cwd=tmp_path), cwd=tmp_path)
    document: Mapping[str, object] = report.as_document()
    assert set(document) == {"interpreter", "checks", "ok"}
    interpreter = document["interpreter"]
    assert isinstance(interpreter, Mapping)
    assert set(interpreter) == {"implementation", "version", "platform", "supported"}
    assert interpreter["supported"] is True
    checks = document["checks"]
    assert isinstance(checks, list)
    assert [entry["name"] for entry in checks] == list(_PROBE_ORDER)
    for entry in checks:
        assert set(entry) == {"name", "status", "detail"}
        assert entry["status"] in _STATUS_WORDS
    assert document["ok"] is report.ok
    # A document a caller serialises: it round-trips through JSON unchanged (AAP 0.7.3).
    assert json.loads(json.dumps(document)) == document


def test_the_report_document_renders_identically_twice(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """Two invocations in one directory produce one document, so a diff between them is empty."""
    config = resolve(environ={}, cwd=tmp_path)
    first = run_checks(config=config, cwd=tmp_path).as_document()
    second = run_checks(config=config, cwd=tmp_path).as_document()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
