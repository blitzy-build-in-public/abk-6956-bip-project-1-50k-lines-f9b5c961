"""Unit tests for ``foundry.cli``: the five subcommands, the exit-code map and the renderers.
AAP 0.4.4 fixes what each publishes and which code it returns; invocations stay in process."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, NamedTuple

import pytest
from click.testing import CliRunner

from foundry.cli.exit_codes import (
    FAILED,
    INTERRUPT,
    PYTEST_INTERNAL_ERROR,
    PYTEST_NO_TESTS,
    PYTEST_USAGE_ERROR,
    SUCCESS,
    USAGE_OR_CONFIG,
    is_pytest_code,
    passthrough,
)
from foundry.cli.main import build_group, main
from foundry.cli.run import build_pytest_argv
from foundry.cli.run import run as run_command
from foundry.core.exceptions import ConfigError
from foundry.diagnostics import checks

_Capture = pytest.CaptureFixture[str]
_Monkey = pytest.MonkeyPatch
_TIMEOUT_KEY: Final[str] = "http.timeout"
_TIMEOUT_OPTION: Final[str] = "--foundry-http.timeout"
_VERSION_BANNER: Final[str] = "foundry, version 0.1.0"
_VALID_LINE: Final[str] = "configuration valid (profile=default)"
_SUBCOMMANDS: Final[tuple[str, ...]] = ("run", "init", "config", "report", "doctor")
_SECRET: Final[str] = "s3ntinel-" + "header-value"
_TOKEN_VARIABLE: Final[str] = "CHECKOUT_API_TOKEN"
_VALID_TOML: Final[str] = "[http]\ntimeout = 2.0\n"
_SECRET_TOML: Final[str] = (
    f'[http]\ntimeout = 2.0\n\n[http.headers]\nAuthorization = "Bearer {_SECRET}"\n\n'
    f'[http.auth]\nstrategy = "bearer"\ntoken_env = "{_TOKEN_VARIABLE}"\n'
)
_PROBES: Final[tuple[tuple[str, str], ...]] = (
    ("interpreter", "check_interpreter"),
    ("plugin", "check_plugin_registered"),
    ("configuration", "check_configuration"),
    ("artifacts", "check_artifacts_dir"),
    ("cassettes", "check_cassette_dir"),
    ("socket_guard", "check_socket_guard"),
)


class _CliRun(NamedTuple):
    code: int
    stdout: str
    stderr: str


def _run_cli(capsys: _Capture, *args: str) -> _CliRun:
    code = main(list(args))
    captured = capsys.readouterr()
    return _CliRun(code=code, stdout=captured.out, stderr=captured.err)


def _write_config(directory: Path, text: str) -> Path:
    path = directory / "foundry.toml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _command_names(help_text: str) -> list[str]:
    assert "Commands:" in help_text, help_text
    section = help_text.split("Commands:", 1)[1]
    return [line.split()[0] for line in section.splitlines() if line.strip()][:5]


@pytest.fixture(autouse=True)
def _logical_width(monkeypatch: _Monkey) -> None:
    monkeypatch.setenv("COLUMNS", "400")


@pytest.fixture
def project(tmp_path: Path, clean_foundry_env: None, monkeypatch: _Monkey) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_published_code_vocabulary_the_five_subcommands_and_the_banner() -> None:
    assert (SUCCESS, FAILED, USAGE_OR_CONFIG) == (0, 1, 2)
    assert (PYTEST_INTERNAL_ERROR, PYTEST_USAGE_ERROR, PYTEST_NO_TESTS) == (3, 4, 5)
    given = (0, 1, 2, 3, 4, 5)
    assert [is_pytest_code(code) for code in given] == [False, False, False, True, True, True]
    assert [passthrough(code) for code in (*given, None)] == [0, 1, 2, 3, 4, 5, 0]
    help_result = CliRunner().invoke(build_group(), ["--help"])
    version_result = CliRunner().invoke(build_group(), ["--version"])
    assert help_result.exit_code == SUCCESS, help_result.output
    assert _command_names(help_result.output) == list(_SUBCOMMANDS)
    assert version_result.exit_code == SUCCESS, version_result.output
    assert version_result.output.strip() == _VERSION_BANNER


def test_config_show_prints_its_table_its_documents_and_the_source_of_every_key(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    _write_config(project, _VALID_TOML)
    monkeypatch.setenv("FOUNDRY_DATA__DIR", "elsewhere")
    table = _run_cli(capsys, "config", "show")
    assert table.code == SUCCESS, table.stderr
    assert table.stdout.index("Key") < table.stdout.index(_TIMEOUT_KEY)
    row = next(line for line in table.stdout.splitlines() if _TIMEOUT_KEY in line)
    assert "2.0" in row and "file:" in row
    assert "env:FOUNDRY_DATA__DIR" in table.stdout and "default" in table.stdout
    document = json.loads(_run_cli(capsys, "config", "show", "--format", "json").stdout)
    assert set(document) == {"resolved", "sources"}
    assert document["resolved"]["http"]["timeout"] == 2.0
    assert document["sources"][_TIMEOUT_KEY]["layer"] == "file"
    assert document["sources"][_TIMEOUT_KEY]["detail"] == str(project / "foundry.toml")


def test_config_show_applies_or_defers_an_override_and_validate_reports_success(
    project: Path, capsys: _Capture
) -> None:
    applied = _run_cli(capsys, "config", "show", "--resolved", _TIMEOUT_OPTION, "1.0")
    assert applied.code == SUCCESS, applied.stderr
    applied_row = next(line for line in applied.stdout.splitlines() if _TIMEOUT_KEY in line)
    assert "1.0" in applied_row and "cli:--foundry-http.timeout" in applied_row
    deferred = _run_cli(capsys, "config", "show", _TIMEOUT_OPTION, "1.0")
    assert deferred.code == SUCCESS, deferred.stderr
    assert "not applied" in deferred.stdout
    assert "not-applied:--foundry-http.timeout" in deferred.stdout
    resolved = next(line for line in deferred.stdout.splitlines() if "10.0" in line)
    assert _TIMEOUT_KEY in resolved and "not-applied:" not in resolved
    valid = _run_cli(capsys, "config", "validate")
    assert (valid.code, valid.stdout.strip(), valid.stderr) == (SUCCESS, _VALID_LINE, "")


def test_doctor_keeps_zero_for_a_warning_and_for_the_optional_probe(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    def patch_probes(overrides: dict[str, str] | None = None) -> None:
        wanted = dict(overrides or {})
        for probe, function in _PROBES:
            row = checks.ProbeResult(
                name=probe,
                status=checks.ProbeStatus(wanted.get(probe, "pass")),
                detail=f"patched {probe}",
                required=probe in checks.REQUIRED_PROBES,
            )
            monkeypatch.setattr(checks, function, lambda *args, _row=row, **kwargs: _row)

    patch_probes()
    table = _run_cli(capsys, "doctor")
    assert table.code == SUCCESS, table.stderr
    assert all(column in table.stdout for column in ("Probe", "Status", "Detail"))
    for overrides in ({"configuration": "warn"}, {"socket_guard": "fail"}):
        patch_probes(overrides)
        warned = _run_cli(capsys, "doctor", "--json")
        assert (warned.code, json.loads(warned.stdout)["ok"]) == (SUCCESS, True)


def test_no_rendered_surface_carries_a_configuration_secret(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    _write_config(project, _SECRET_TOML)
    monkeypatch.setenv(_TOKEN_VARIABLE, _SECRET)
    table = _run_cli(capsys, "config", "show")
    assert table.code == SUCCESS, table.stderr
    assert "***" in table.stdout
    for run in (
        table,
        _run_cli(capsys, "config", "show", "--format", "json"),
        _run_cli(capsys, "config", "show", "--format", "toml"),
        _run_cli(capsys, "config", "validate"),
    ):
        assert run.code == SUCCESS, run.stderr
        assert _SECRET not in run.stdout + run.stderr
    _write_config(project, _SECRET_TOML.replace("timeout = 2.0", "timeout = -1"))
    faulted = _run_cli(capsys, "config", "validate")
    assert (faulted.code, faulted.stdout) == (USAGE_OR_CONFIG, "")
    assert _TIMEOUT_KEY in faulted.stderr and "0 < t <= 300" in faulted.stderr
    assert _SECRET not in faulted.stdout + faulted.stderr
    misused = _run_cli(capsys, "config", "show", "--foundry-nope", "1.0")
    assert misused.code == USAGE_OR_CONFIG and "No such option" in misused.stderr


@pytest.mark.parametrize(
    "code", [SUCCESS, FAILED, PYTEST_INTERNAL_ERROR, PYTEST_USAGE_ERROR, PYTEST_NO_TESTS]
)
def test_run_returns_pytests_own_code(project: Path, monkeypatch: _Monkey, code: int) -> None:
    monkeypatch.setattr("pytest.main", lambda argv: code)
    assert CliRunner().invoke(run_command, ["-q"], standalone_mode=False).return_value == code


def test_run_hands_pytest_the_builder_argv_and_never_starts_it_on_a_fault(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("pytest.main", lambda argv: calls.append(list(argv)) or SUCCESS)
    blocked = _run_cli(capsys, "run", _TIMEOUT_OPTION, "-1")
    assert (blocked.code, blocked.stdout) == (USAGE_OR_CONFIG, "")
    pytest_args = [_TIMEOUT_OPTION, "1.0", "tests/unit", "-q"]
    result = CliRunner().invoke(run_command, pytest_args, standalone_mode=False)
    assert calls == [pytest_args] and result.return_value == SUCCESS

    def interrupting(argv: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("pytest.main", interrupting)
    interrupted = _run_cli(capsys, "run", "-q")
    assert interrupted.code == INTERRUPT
    assert "aborted" in interrupted.stderr


def test_the_argv_orders_the_registry_then_the_specials_then_the_pytest_arguments() -> None:
    supplied = {
        _TIMEOUT_OPTION: "1.5",
        "--foundry-record": True,
        "--foundry-profile": "ci",
        "--foundry-json-report": "results.json",
    }
    expected = f"--foundry-profile ci {_TIMEOUT_OPTION} 1.5 --foundry-json-report results.json "
    expected += "--foundry-record tests/unit -q"
    assert build_pytest_argv(supplied, ["tests/unit", "-q"]) == expected.split()
    for off in (False, "false", "off", ""):
        assert build_pytest_argv({"--foundry-record": off}, []) == []
    with pytest.raises(ConfigError) as raised:
        build_pytest_argv({"http.timeot": "1.0"}, [])
    assert raised.value.key == "http.timeot" and "http.timeot" in str(raised.value)
