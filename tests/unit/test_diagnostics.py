"""Unit tests for ``foundry.diagnostics``: the six doctor probes and the report they build.

AAP 0.4.4 fixes what ``foundry doctor`` answers -- ``0`` when every required probe passes, ``2``
when a required probe fails, and no change at all for a warning -- and AAP 0.7.1/0.7.3 fix the six
rows and their words. What is pinned here is that mechanism, probe by probe and then through
:func:`~foundry.diagnostics.checks.run_checks`:

* the six rows appear in the published order ``interpreter``, ``plugin``, ``configuration``,
  ``artifacts``, ``cassettes``, ``socket_guard``, each with one of the three status words and a
  non-empty detail;
* only a ``FAIL`` on a required row clears the report's ``ok``, which is the value that maps to the
  exit code, so a ``WARN`` -- an unsupported platform, a project with no cassette directory and no
  socket guard -- never fails the command;
* ``check_interpreter`` distinguishes an unsupported interpreter, an unsupported platform and a
  supported pair; ``check_plugin_registered`` reports the ``pytest11`` registration and the module
  it must be able to import, both halves checked; ``check_configuration`` reports the resolved
  profile or the configuration fault's own message; the two directory probes create nothing they
  were not asked to create, count what a cassette directory holds and report an absent one as a
  warning; and the socket-guard probe reads the convention ``tests/conftest.py`` publishes.

Every probe is handed a path under ``tmp_path``, and the artifact probe is the only one that creates
a directory, so no test here writes inside the repository. Nothing opens a socket, and nothing
sleeps.
"""

from __future__ import annotations

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


# --- The report (AAP 0.4.4, AAP 0.7.3) -----------------------------------


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
    tmp_path: Path, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that raises becomes one ``FAIL`` row instead of a traceback (AAP 0.1.2)."""

    def explode(*_args: object, **_kwargs: object) -> ProbeResult:
        """Stand in for a probe that cannot do its work."""
        raise FoundryError("the probe could not run")

    monkeypatch.setattr("foundry.diagnostics.checks.check_cassette_dir", explode)
    report = run_checks(cwd=tmp_path)
    cassettes = next(row for row in report.checks if row.name == PROBE_CASSETTES)
    assert cassettes.status is ProbeStatus.FAIL
    assert cassettes.detail == "probe failed: FoundryError"
    assert len(report.checks) == len(_PROBE_ORDER)


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

    Read from :mod:`importlib.metadata`, which is the source the probe itself consults when it is
    handed no pairs, so the expectation follows the environment rather than this tree: a source
    checkout publishes nothing, and an environment that installed ``pytest-foundry`` publishes
    ``foundry.plugin``.
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
    """The plugin row follows the ``pytest11`` metadata the environment actually publishes.

    Three states are possible: nothing registered (a source checkout), registered with an
    importable module (an installed distribution), and registered with no importable module
    (``src/foundry/plugin.py`` absent). The expectation is observed here -- the targets the
    metadata publishes and whether the module they name resolves -- so the probe is held to one
    rule in all of them: ``FAIL`` naming what the metadata says, ``PASS`` naming the registration
    it found, and ``report.ok`` never disagreeing with ``report.failed`` (AAP 0.4.2, AAP 0.4.4).
    """
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
    """The installed world's verdict, from the pairs ``run_checks`` already accepts (AAP 0.4.2).

    Both halves of the registration are supplied so the probe's ``PASS`` branch runs here, where
    no distribution is installed: the pair is the entry-point metadata an installed
    ``pytest-foundry`` publishes, and the module half stands in for the ``src/foundry/plugin.py``
    that installation ships. Nothing is installed and no module is created, so the report is the
    one that world produces without this tree having to become it.
    """
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


def test_a_guard_publishing_no_count_reads_as_installed_and_unmeasured() -> None:
    """The two halves of the convention are separate: announcing is not measuring."""
    state = detect_socket_guard({"guard": SimpleNamespace(FOUNDRY_SOCKET_GUARD=object())})
    assert state == SocketGuardState(installed=True, non_loopback_attempts=None, source="guard")
    assert check_socket_guard(state).status is ProbeStatus.WARN


# --- A document a caller can serialise ---------------------------------


def test_the_report_document_holds_the_published_members(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    """``ok`` and the three documented ``checks`` keys are what a reader of ``--json`` sees."""
    report = run_checks(config=resolve(environ={}, cwd=tmp_path), cwd=tmp_path)
    document: Mapping[str, object] = report.as_document()
    assert set(document) == {"interpreter", "checks", "ok"}
    checks = document["checks"]
    assert isinstance(checks, list)
    assert [entry["name"] for entry in checks] == list(_PROBE_ORDER)
    for entry in checks:
        assert set(entry) == {"name", "status", "detail"}
        assert entry["status"] in _STATUS_WORDS
    assert document["ok"] is report.ok
