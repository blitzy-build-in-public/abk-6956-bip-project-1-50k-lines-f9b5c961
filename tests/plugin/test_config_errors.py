"""The exit-2 contract of every configuration fault, observed through real child pytest runs: each
fault names the key, variable or path an operator has to change, writes no report, and leaves the
control test unrun. A valid configuration is the positive control.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

import pytest

if TYPE_CHECKING:
    from pathlib import Path

REACHES: tuple[str, ...] = ()

_CONFIG_EXIT: Final[int] = 2
_REPORT_FILES: Final[tuple[str, ...]] = ("results.json", "junit.xml", "report.html")
_PASS_SUMMARY: Final[str] = "1 passed"

_TRIVIAL_TEST: Final[str] = """def test_smoke():
    assert True
"""

_VALID_TOML: Final[str] = "[http]\ntimeout = 2.0\n"
_BASE_URL_KEY: Final[str] = "http.base_url"
_BASE_URL_OPTION: Final[str] = f"--foundry-{_BASE_URL_KEY}"
_NO_USERINFO: Final[str] = "no userinfo"
_URL_USER: Final[str] = "QAURLUSER_2b7d"
_URL_PASSWORD: Final[str] = "QAURLPASS_9c4e"
# Assembled from parts so no literal scheme://user:password@host is committed.
_USERINFO_URL: Final[str] = "http://" + _URL_USER + ":" + _URL_PASSWORD + "@127.0.0.1:1"
_TIMEOUT_KEY: Final[str] = "http.timeout"
_TOPT: Final[str] = f"--foundry-{_TIMEOUT_KEY}"
_TIMEOUT_RANGE: Final[str] = "0 < t <= 300"
_TIMEOUT_VAR: Final[str] = "FOUNDRY_HTTP__TIMEOUT"
_NUMBER: Final[str] = "expected a number"
_INTEGER: Final[str] = "expected an integer"
_CAPTURE_LINES: Final[str] = "--foundry-logging.capture_lines"
_ALIAS: Final[str] = "--foundry-timeout"
_VALID_TIMEOUT: Final[str] = "1.0"
_PYTEST_USAGE: Final[tuple[str, ...]] = (
    "usage: pytest",
    "invalid float value",
    "invalid int value",
    "unrecognized arguments",
)
_PROBE: Final[str] = (
    "from foundry.config import FoundryConfig\n\n\n"
    "def test_timeout_comes_from_the_command_line(foundry_config: FoundryConfig) -> None:\n"
    "    assert foundry_config.http.timeout == 1.0\n"
    '    assert foundry_config.source_of("http.timeout").detail == "--foundry-http.timeout"\n'
)

_TYPO: Final[str] = "http.timout"
_TYPO_ARG: Final[str] = f"--foundry-{_TYPO}=3.0"
_TYPO_TOML: Final[str] = f"[http]\n{_TYPO.rsplit('.', 1)[-1]} = 3.0\n"
_HINT: Final[str] = "did you mean 'http.timeout'?"

_MISSING: Final[str] = "nope.toml"
_TOKEN_VAR: Final[str] = "FOUNDRY_TEST_TOKEN_MISSING"
_BEARER_TOML: Final[str] = f'[http.auth]\nstrategy = "bearer"\ntoken_env = "{_TOKEN_VAR}"\n'
# Deliberately not a FOUNDRY_ prefix: such a name would itself be an unknown configuration variable.
_SENTINEL_VAR: Final[str] = "DEMO_API_TOKEN"
_SENTINEL: Final[str] = "sentinel-value-that-must-not-be-printed"

_REC: Final[str] = "--foundry-record"
_REP: Final[str] = "--foundry-replay"


def _prepare(pytester: pytest.Pytester, foundry_toml: str | None = None) -> Path:
    pytester.makepyfile(test_smoke=_TRIVIAL_TEST)
    if foundry_toml is not None:
        (pytester.path / "foundry.toml").write_text(foundry_toml, encoding="utf-8")
    return pytester.path


def _assert_exit_2_without_reports(result: pytest.RunResult, directory: Path, case: str) -> str:
    """Assert ``case`` ended with Foundry's exit code 2, wrote no report and ran no test."""
    output = "\n".join(result.outlines)
    assert result.ret == _CONFIG_EXIT, f"{case}: exit {result.ret}, not {_CONFIG_EXIT}\n{output}"
    written = sorted(name for name in _REPORT_FILES if (directory / name).exists())
    assert not written, f"{case}: a run that could not be configured wrote {written}\n{output}"
    assert _PASS_SUMMARY not in output, f"{case}: the control test must not run\n{output}"
    return output


# --- The fault table: one row per published configuration fault (AAP 0.4.7) --


@pytest.mark.parametrize(
    ("foundry_toml", "environment", "argv", "fragments"),
    [
        pytest.param(None, {}, (f"{_TOPT}=-1",), (_TIMEOUT_KEY, _TIMEOUT_RANGE), id="range"),
        pytest.param(_TYPO_TOML, {}, (), (_TYPO, _HINT), id="unknown-file-key"),
        pytest.param(None, {}, (_TYPO_ARG,), (_TYPO, _HINT), id="unknown-cli-key"),
        pytest.param(None, {_TIMEOUT_VAR: "abc"}, (), (_TIMEOUT_VAR, _NUMBER), id="bad-env"),
        pytest.param(None, {}, (_REC, _REP), (_REC, _REP), id="both-modes"),
    ],
)
def test_configuration_faults_exit_2_without_reports(
    pytester: pytest.Pytester,
    clean_foundry_env: None,
    monkeypatch: pytest.MonkeyPatch,
    foundry_toml: str | None,
    environment: Mapping[str, str],
    argv: tuple[str, ...],
    fragments: tuple[str, ...],
    request: pytest.FixtureRequest,
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    directory = _prepare(pytester, foundry_toml)
    case = request.node.callspec.id
    output = _assert_exit_2_without_reports(pytester.runpytest_subprocess(*argv), directory, case)
    for fragment in fragments:
        assert fragment in output, f"{case}: the message must quote {fragment!r}\n{output}"


def test_userinfo_in_a_base_url_override_exits_2_without_reports(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    directory = _prepare(pytester)
    result = pytester.runpytest_subprocess(f"{_BASE_URL_OPTION}={_USERINFO_URL}")
    output = _assert_exit_2_without_reports(result, directory, "userinfo in a base URL override")
    assert _BASE_URL_KEY in output and _NO_USERINFO in output, output
    assert _URL_USER not in output and _URL_PASSWORD not in output, "no credential may be printed"


def test_missing_explicit_configuration_file_names_the_path(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    directory = _prepare(pytester)
    missing = directory / _MISSING
    result = pytester.runpytest_subprocess("--foundry-config", str(missing))
    output = _assert_exit_2_without_reports(result, directory, "missing explicit config file")
    assert f"config file not found: {missing}" in output, output


def test_absent_secret_variable_is_named_without_its_value(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_TOKEN_VAR, raising=False)
    monkeypatch.setenv(_SENTINEL_VAR, _SENTINEL)
    directory = _prepare(pytester, _BEARER_TOML)
    result = pytester.runpytest_subprocess()
    output = _assert_exit_2_without_reports(result, directory, "absent bearer token variable")
    assert _TOKEN_VAR in output, output
    assert _SENTINEL not in output, "no value may reach the terminal: token_env stores a name only"


def test_valid_configuration_still_runs(pytester: pytest.Pytester, clean_foundry_env: None) -> None:
    directory = _prepare(pytester, _VALID_TOML)
    result = pytester.runpytest_subprocess()
    output = "\n".join(result.outlines)
    assert result.ret == 0, f"a valid configuration must run green, not exit {result.ret}\n{output}"
    result.assert_outcomes(passed=1)
    assert "foundry: profile=default" in output, output
    missing = sorted(name for name in _REPORT_FILES if not (directory / name).exists())
    assert not missing, f"a green run writes all three reports; missing: {missing}\n{output}"


@pytest.mark.parametrize(
    ("spelling", "value", "expected"),
    [
        (_TOPT, "abc", _NUMBER),
        (_CAPTURE_LINES, "1.5", _INTEGER),
        (_ALIAS, "abc", _NUMBER),
    ],
    ids=["float-generic-option", "int-generic-option", "float-named-alias"],
)
def test_unparseable_command_line_value_exits_2_with_the_expected_type(
    pytester: pytest.Pytester,
    clean_foundry_env: None,
    spelling: str,
    value: str,
    expected: str,
) -> None:
    directory = _prepare(pytester)
    result = pytester.runpytest_subprocess(f"{spelling}={value}")
    output = _assert_exit_2_without_reports(result, directory, f"unparseable {spelling}")
    assert f"invalid value for option {spelling}: {expected}" in output, output
    # pytest's own argument parser answers an unreadable number with its usage-error code 4.
    assert not [text for text in _PYTEST_USAGE if text in output], output


def test_valid_numeric_override_still_runs(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    pytester.makepyfile(test_probe=_PROBE)
    result = pytester.runpytest_subprocess(f"{_TOPT}={_VALID_TIMEOUT}")
    assert result.ret == 0, f"a readable override must run green, not exit {result.ret}"
    result.assert_outcomes(passed=1)
