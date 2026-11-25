"""Unit tests for ``foundry.diagnostics``: the six doctor probes and the report they build.
AAP 0.4.4 fixes the exit mapping (0 when every required probe passes, 2 when one fails, unchanged
for a warning), AAP 0.7.1/0.7.3 fix the rows and their words, and the socket-guard row is optional;
the live plugin row is proven through ``tests/plugin/test_cli_surface.py`` and the profile row
through ``tests/unit/test_doctor_profile_row.py``."""

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import pytest

from foundry.config import resolve
from foundry.core.exceptions import FoundryError
from foundry.diagnostics.checks import (
    PROBE_CASSETTES,
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
from foundry.diagnostics.report import DoctorReport, InterpreterInfo, ProbeResult, ProbeStatus
from foundry.diagnostics.socket_guard import (
    PROBE_NAME,
    SocketGuardState,
    check_socket_guard,
    detect_socket_guard,
)

_STATUS_WORDS: Final[frozenset[str]] = frozenset({"pass", "warn", "fail"})
_ENTRY_POINT_DETAIL: Final[str] = 'pytest11 entry point foundry = "foundry.plugin"'
_REGISTERED: Final[str] = f"{_ENTRY_POINT_DETAIL} registered"
_PAIR: Final[tuple[str, str]] = ("foundry", "foundry.plugin")
_PAIR_ATTRIBUTE: Final[tuple[str, str]] = (_PAIR[0], f"{_PAIR[1]}:configure")
_UNCHECKED: Final[str] = "not checked: configuration unavailable"
_SUPPORTED: Final[InterpreterInfo] = InterpreterInfo(
    implementation="CPython", version="3.12.4", platform="linux", supported=True
)
_WINDOWS: Final[InterpreterInfo] = InterpreterInfo(
    implementation="CPython", version="3.12.4", platform="win32", supported=True
)
_WIN32_SYS = SimpleNamespace(
    platform="win32", version_info=SimpleNamespace(major=3, minor=12, micro=4)
)
_PY39_SYS = SimpleNamespace(
    platform="linux", version_info=SimpleNamespace(major=3, minor=9, micro=18)
)
_PYPY_PLATFORM = SimpleNamespace(python_implementation=lambda: "PyPy")


def _row(
    name: str, status: ProbeStatus, *, required: bool = True, detail: str = "d"
) -> ProbeResult:
    return ProbeResult(name=name, status=status, detail=detail, required=required)


def test_run_checks_reports_the_published_order(tmp_path: Path, clean_foundry_env: None) -> None:
    report = run_checks(cwd=tmp_path)
    assert report.interpreter == InterpreterInfo.current()
    assert REQUIRED_PROBES == ("interpreter", "plugin", "configuration", "artifacts", "cassettes")
    assert [row.name for row in report.checks] == [*REQUIRED_PROBES, PROBE_NAME]
    assert [row.required for row in report.checks] == [True] * 5 + [False]
    assert {row.status.value for row in report.checks} <= _STATUS_WORDS
    assert all(row.detail.strip() for row in report.checks)
    plugin = next(row for row in report.checks if row.name == PROBE_PLUGIN)
    assert (plugin.status, plugin.detail, plugin.required) == (ProbeStatus.PASS, _REGISTERED, True)
    # The artifact probe creates the directory it inspects; nothing else appears beside it.
    assert sorted(path.name for path in tmp_path.iterdir() if path.is_dir()) == ["artifacts"]
    assert report.ok is (report.failed == ())


def test_run_checks_isolates_a_probe_that_raises(
    tmp_path: Path,
    clean_foundry_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> ProbeResult:
        raise FoundryError("the probe could not run")

    caplog.set_level(logging.WARNING, logger="foundry")
    monkeypatch.setattr("foundry.diagnostics.checks.check_cassette_dir", explode)
    report = run_checks(cwd=tmp_path)
    cassettes = next(row for row in report.checks if row.name == PROBE_CASSETTES)
    assert (cassettes.status, cassettes.detail) == (ProbeStatus.FAIL, "probe failed: FoundryError")
    assert len(report.checks) == len(REQUIRED_PROBES) + 1
    # AAP 0.1.2: the fault is reported once, on the foundry logger, and nowhere else.
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert [record.name for record in warnings] == ["foundry"]
    message = warnings[0].getMessage()
    assert PROBE_CASSETTES in message and "FoundryError" in message and report.ok is False


def test_a_warning_never_clears_the_verdict_and_a_required_failure_does() -> None:
    def report(*checks: ProbeResult) -> DoctorReport:
        return DoctorReport(interpreter=_SUPPORTED, checks=checks)

    rows = (_row("a", ProbeStatus.WARN), _row("b", ProbeStatus.FAIL, required=False))
    mixed = report(*rows, _row("c", ProbeStatus.PASS))
    assert (mixed.ok, mixed.failed, mixed.as_document()["ok"]) == (True, (), True)
    assert not report(_row("a", ProbeStatus.PASS), _row("b", ProbeStatus.FAIL)).ok


@pytest.mark.parametrize(
    ("attribute", "value", "status", "fragments"),
    [
        (None, None, ProbeStatus.PASS, ("CPython", " on ")),
        (None, _SUPPORTED, ProbeStatus.PASS, ("CPython 3.12.4 on linux",)),
        (None, _WINDOWS, ProbeStatus.WARN, ("CPython 3.12.4 on win32", "is unsupported")),
        ("sys", _WIN32_SYS, ProbeStatus.WARN, ("on win32 is unsupported",)),
        ("sys", _PY39_SYS, ProbeStatus.FAIL, ("requires CPython 3.11-3.13", "on linux")),
        ("platform", _PYPY_PLATFORM, ProbeStatus.FAIL, ("PyPy", "requires CPython 3.11-3.13")),
    ],
    ids=["live", "supported", "windows", "win32", "py39", "pypy"],
)
def test_check_interpreter_judges_the_version_and_the_platform_it_reads(
    monkeypatch: pytest.MonkeyPatch,
    attribute: str | None,
    value: object,
    status: ProbeStatus,
    fragments: tuple[str, ...],
) -> None:
    if attribute is not None:
        monkeypatch.setattr(f"foundry.diagnostics.report.{attribute}", value)
    row = check_interpreter(value if isinstance(value, InterpreterInfo) else None)
    assert (row.name, row.status, row.required) == (PROBE_INTERPRETER, status, True)
    assert all(fragment in row.detail for fragment in fragments)
    if status is ProbeStatus.PASS:
        assert "CPython 3.11-3.13" not in row.detail


@pytest.mark.parametrize(
    ("discovered", "available", "status", "suffix"),
    [
        ([_PAIR], True, ProbeStatus.PASS, "registered"),
        ([_PAIR_ATTRIBUTE], True, ProbeStatus.PASS, "registered"),
        ([_PAIR], False, ProbeStatus.FAIL, "is registered but foundry.plugin is not importable"),
        ((), True, ProbeStatus.FAIL, "is not registered; names found: none"),
        (
            [("pytest_cov", "pytest_cov.plugin")],
            True,
            ProbeStatus.FAIL,
            "is not registered; names found: pytest_cov",
        ),
    ],
    ids=["module", "attribute", "not-importable", "nothing", "other-plugin"],
)
def test_check_plugin_registered_judges_what_collection_found(
    discovered: list[tuple[str, str]], available: bool, status: ProbeStatus, suffix: str
) -> None:
    row = check_plugin_registered(discovered=discovered, module_available=available)
    detail = f"{_ENTRY_POINT_DETAIL} {suffix}"
    assert (row.name, row.status, row.detail, row.required) == (PROBE_PLUGIN, status, detail, True)


@pytest.mark.parametrize(
    ("variable", "value", "expected"),
    [
        ("FOUNDRY_HTTP__TIMEOUT", "-1", ("http.timeout", "0 < t <= 300")),
        ("FOUNDRY_HTTP__TIMEOUTT", "1.0", ("http.timeoutt", "")),
    ],
    ids=["out-of-range", "unknown-key"],
)
def test_check_configuration_reports_the_fault_it_cannot_resolve(
    tmp_path: Path,
    clean_foundry_env: None,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    expected: tuple[str, str],
) -> None:
    monkeypatch.setenv(variable, value)
    monkeypatch.chdir(tmp_path)
    row = check_configuration()
    # AAP 0.4.5: an out-of-range value names the accepted range, not merely the rejected value.
    assert (row.status, row.required) == (ProbeStatus.FAIL, True)
    assert all(part in row.detail for part in expected)


def test_check_artifacts_dir_creates_and_proves_the_directory_writable(tmp_path: Path) -> None:
    target = tmp_path / "one" / "two" / "artifacts"
    row = check_artifacts_dir(directory=target)
    assert (row.status, row.detail) == (ProbeStatus.PASS, f"{target.resolve()} writable")
    assert target.is_dir() and list(target.iterdir()) == []


def test_check_artifacts_dir_resolves_and_refuses_a_path_it_cannot_use(tmp_path: Path) -> None:
    relative = check_artifacts_dir(directory=Path("artifacts"), cwd=tmp_path)
    expected = f"{(tmp_path / 'artifacts').resolve()} writable"
    assert (relative.status, relative.detail) == (ProbeStatus.PASS, expected)
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    row = check_artifacts_dir(directory=blocked)
    assert (row.status, row.detail.startswith(f"{blocked.resolve()}: ")) == (ProbeStatus.FAIL, True)


@pytest.mark.skipif(
    os.geteuid() == 0, reason="a privileged user writes into a directory whose mode forbids it"
)
def test_check_artifacts_dir_fails_on_a_directory_it_may_not_write(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        row = check_artifacts_dir(directory=locked)
    finally:
        locked.chmod(0o700)
    assert (row.status, row.detail.startswith(f"{locked.resolve()}: ")) == (ProbeStatus.FAIL, True)


def test_check_cassette_dir_reports_its_state_and_contents(tmp_path: Path) -> None:
    directory = tmp_path / "cassettes"
    path = directory.resolve()
    absent = check_cassette_dir(directory=directory)
    assert (absent.status, absent.detail) == (ProbeStatus.WARN, f"{path} not created yet")
    assert not directory.exists()
    directory.mkdir()
    empty = check_cassette_dir(directory=directory)
    assert (empty.status, empty.detail.endswith("(0 cassette file(s))")) == (ProbeStatus.PASS, True)
    (directory / "one.json").write_text("[]", encoding="utf-8")
    (directory / "notes.txt").write_text("ignored", encoding="utf-8")
    one = check_cassette_dir(directory=directory)
    assert (one.status, one.detail.endswith("(1 cassette file(s))")) == (ProbeStatus.PASS, True)
    blocked = tmp_path / "blocked"
    blocked.write_text("{}", encoding="utf-8")
    row = check_cassette_dir(directory=blocked)
    assert (row.status, row.detail) == (ProbeStatus.FAIL, f"{blocked.resolve()} is not a directory")
    # With no configuration and no path neither probe fails: both warn that they could not run.
    unchecked = [check_artifacts_dir(), check_cassette_dir()]
    expected = [(ProbeStatus.WARN, _UNCHECKED, True)]
    assert [(row.status, row.detail, row.required) for row in unchecked] == expected * 2


def test_the_socket_guard_probe_reads_the_state_the_suite_publishes(socket_guard: object) -> None:
    # The fixture's guard type lives in tests/conftest.py, so its members are read off the object.
    count: int = socket_guard.non_loopback_attempts
    state = detect_socket_guard()
    assert (state.installed, state.source, state.non_loopback_attempts) == (True, "conftest", count)
    row = check_socket_guard(state)
    assert (row.name, row.required, row.detail) == (PROBE_NAME, False, state.describe())
    assert row.status is (ProbeStatus.PASS if count == 0 else ProbeStatus.FAIL)


def test_a_guard_publishes_its_count_separately_from_its_announcement() -> None:
    # The two halves of the convention are separate: announcing a guard is not measuring it.
    assert detect_socket_guard({}) == SocketGuardState.absent()
    on_module = SimpleNamespace(FOUNDRY_SOCKET_GUARD=True, FOUNDRY_NON_LOOPBACK_ATTEMPTS=2)
    expected = SocketGuardState(installed=True, non_loopback_attempts=2, source="a_guard")
    assert detect_socket_guard({"a_guard": on_module}) == expected
    guard = SimpleNamespace(FOUNDRY_NON_LOOPBACK_ATTEMPTS=4)
    on_object = detect_socket_guard({"b_guard": SimpleNamespace(FOUNDRY_SOCKET_GUARD=guard)})
    assert (on_object.non_loopback_attempts, on_object.source) == (4, "b_guard")
    state = detect_socket_guard({"guard": SimpleNamespace(FOUNDRY_SOCKET_GUARD=object())})
    assert state == SocketGuardState(installed=True, non_loopback_attempts=None, source="guard")


@pytest.mark.parametrize(
    ("installed", "attempts", "status"),
    [
        (False, None, ProbeStatus.WARN),
        (True, None, ProbeStatus.WARN),
        (True, 0, ProbeStatus.PASS),
        (True, 1, ProbeStatus.FAIL),
    ],
    ids=["no-guard", "unmeasured", "zero", "one"],
)
def test_the_socket_guard_row_reports_what_the_count_says(
    installed: bool, attempts: int | None, status: ProbeStatus
) -> None:
    state = SocketGuardState(installed=installed, non_loopback_attempts=attempts, source="m")
    row = check_socket_guard(state)
    assert (row.status, row.detail, row.required) == (status, state.describe(), False)
    if status is ProbeStatus.FAIL:
        assert f"{attempts} non-loopback connection attempt(s)" in row.detail


def test_the_report_document_holds_the_published_members(
    tmp_path: Path, clean_foundry_env: None
) -> None:
    config = resolve(environ={}, cwd=tmp_path)
    report = run_checks(config=config, cwd=tmp_path)
    document: Mapping[str, object] = report.as_document()
    assert {status.value for status in ProbeStatus} == set(_STATUS_WORDS)
    assert set(document) == {"interpreter", "checks", "ok"}
    interpreter = document["interpreter"]
    assert isinstance(interpreter, Mapping)
    assert set(interpreter) == {"implementation", "version", "platform", "supported"}
    assert interpreter["supported"] is True
    checks = document["checks"]
    assert isinstance(checks, list)
    assert [entry["name"] for entry in checks] == [*REQUIRED_PROBES, PROBE_NAME]
    for entry in checks:
        assert set(entry) == {"name", "status", "detail"} and entry["status"] in _STATUS_WORDS
    assert document["ok"] is report.ok and json.loads(json.dumps(document)) == document
    again = run_checks(config=config, cwd=tmp_path).as_document()
    assert json.dumps(again, sort_keys=True) == json.dumps(document, sort_keys=True)
    # A guard state handed in reaches the row, and a non-required failure leaves the verdict alone.
    state = SocketGuardState(installed=True, non_loopback_attempts=3, source="a_guard")
    guarded = run_checks(config=config, cwd=tmp_path, guard_state=state)
    row = next(check for check in guarded.checks if check.name == PROBE_NAME)
    assert (row.status, row.detail, row.required) == (ProbeStatus.FAIL, state.describe(), False)
    assert PROBE_NAME not in [check.name for check in guarded.failed]
    assert guarded.ok is (guarded.failed == ())
