"""The exit-2 contract of every configuration fault, observed through real pytest runs.

AAP 0.14.1's criterion 4 has two halves, and this module is its failure half: ``http.timeout = -1``
must end a run with exit code 2 and a message naming the key and its accepted range. The cases that
share that code are the AAP 0.4.7 state-table rows for a configuration fault -- an explicitly named
file that does not exist, an unknown key in any layer with ``difflib``'s closest valid spelling, an
unparseable ``FOUNDRY_*`` value, an absent auth secret variable, and the ``--foundry-record`` /
``--foundry-replay`` combination AAP 0.4.4 calls a usage error. Every one of them must also write no
report, because a session that could not be configured collected nothing to report on.

Each case runs through ``pytester.runpytest_subprocess()``. Every fault here ends the session inside
``pytest_configure`` through ``pytest.exit(..., returncode=2)`` on both surfaces (assumption A4), so
only a child process reports the code a shell would see, and that child's ``FOUNDRY_*`` environment
is exactly what the case injected. The trivial passing test written beside every fault is the
control: the code cannot come from an empty directory, because a run over a directory with no test
answers pytest's own no-tests-collected 5.

``test_valid_configuration_still_runs`` is the positive control: the same project with a valid
``[http] timeout`` must exit 0 and write the three report files of AAP 0.4.6, so no negative
case can pass because the project is broken for an unrelated reason. Secret indirection (implicit
requirement I9) is asserted positively in the bearer case -- the configuration names a variable, the
message names that variable only, and a sentinel exported into another variable never reaches the
terminal. Nothing is mocked: the inputs are environment variables and fault-bearing TOML.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

REACHES: tuple[str, ...] = ()
"""This module drives no fixture, subcommand, loader or report format through the real entry point.

It asserts the exit code and the message of a configuration fault (AAP 0.10.1), so it claims no
public-surface entry; the declaration is explicit so the reachability scanner reads a decision.
"""

_CONFIG_EXIT: Final[int] = 2
"""The code a usage or configuration fault raised by Foundry itself ends a run with (AAP 0.4.4)."""

_REPORT_FILES: Final[tuple[str, ...]] = ("results.json", "junit.xml", "report.html")
"""The three fixed report names AAP 0.4.6 writes to the invocation directory."""

_PASS_SUMMARY: Final[str] = "1 passed"
"""What a run that collected the control test reports, and so what a configured fault prevents."""

_TRIVIAL_TEST: Final[str] = '''"""The control: it passes if the session ever reaches collection."""


def test_smoke() -> None:
    """Pass, unless the configuration fault ended the session before collection."""
    assert True
'''

_VALID_TOML: Final[str] = "[http]\ntimeout = 2.0\n"
"""A valid configuration, for the positive control."""

_TIMEOUT_KEY: Final[str] = "http.timeout"
"""The key criterion 4 drives out of range, spelled as every message must name it."""

_TIMEOUT_OPTION: Final[str] = f"--foundry-{_TIMEOUT_KEY}"
"""The generic override AAP 0.4.4 registers for the key."""

_TIMEOUT_RANGE: Final[str] = "0 < t <= 300"
"""``http.timeout``'s accepted range, quoted by the message exactly as AAP 0.4.5 spells it."""

_TIMEOUT_VARIABLE: Final[str] = "FOUNDRY_HTTP__TIMEOUT"
"""The environment spelling of the same key: prefix, then the dotted key with ``__`` nesting."""

_NUMBER_EXPECTED: Final[str] = "expected a number"
"""What the environment layer calls a float: the expected type AAP 0.4.5 asks a message to name."""

_MISSPELT_KEY: Final[str] = "http.timout"
"""A key a person misspells, one letter short of ``http.timeout``."""

_MISSPELT_TOML: Final[str] = f"[http]\n{_MISSPELT_KEY.rsplit('.', 1)[-1]} = 3.0\n"
"""The same misspelling in the file layer, which is the path that always yields the suggestion."""

_CLOSEST_KEY_HINT: Final[str] = "did you mean 'http.timeout'?"
"""The suggestion the configuration layer builds from ``difflib.get_close_matches`` (AAP 0.4.5)."""

_MISSING_CONFIG_NAME: Final[str] = "nope.toml"
"""The file the missing-path case names explicitly, and the path the message must repeat."""

_TOKEN_VARIABLE: Final[str] = "FOUNDRY_TEST_TOKEN_MISSING"
"""The absent bearer-token variable; the configuration stores this name, never a value (I9)."""

_BEARER_TOML: Final[str] = f'[http.auth]\nstrategy = "bearer"\ntoken_env = "{_TOKEN_VARIABLE}"\n'
"""The configuration that makes the session resolve a bearer token at start."""

_SENTINEL_VARIABLE: Final[str] = "DEMO_API_TOKEN"
"""An ordinary variable holding a value Foundry must never print.

Deliberately not spelled with the ``FOUNDRY_`` prefix: such a name would itself be an unknown
configuration variable, and the fault this case asserts is the absent secret, not that one.
"""

_SENTINEL_VALUE: Final[str] = "sentinel-value-that-must-not-be-printed"
"""The stand-in for the credential a real deployment would hold in the token variable."""

_RECORD_OPTION: Final[str] = "--foundry-record"
"""The switch that sets ``cassette.mode`` to record (AAP 0.4.4)."""

_REPLAY_OPTION: Final[str] = "--foundry-replay"
"""The switch that sets ``cassette.mode`` to replay; supplying both is the usage error."""


def _output(result: pytest.RunResult) -> str:
    """Return everything the child wrote.

    pytester keeps the child's stdout and stderr in one ordered line list, which is what a person
    reading a terminal sees, so a message fragment is looked for in both streams at once.
    """
    return "\n".join(result.outlines)


def _prepare(pytester: pytest.Pytester, *, foundry_toml: str | None = None) -> Path:
    """Write the control test and, when the case needs one, its configuration file.

    Both land in ``pytester.path``, which is the child's working directory: that is the invocation
    directory configuration discovery reads and the directory the three report files are written to.
    """
    pytester.makepyfile(test_smoke=_TRIVIAL_TEST)
    if foundry_toml is not None:
        (pytester.path / "foundry.toml").write_text(foundry_toml, encoding="utf-8")
    return pytester.path


def _assert_exit_2_without_reports(result: pytest.RunResult, directory: Path, *, case: str) -> str:
    """Assert ``case`` ended with Foundry's exit code 2, wrote no report and ran no test.

    Returns the child's output so the caller can assert the documented message fragment on it.
    ``result.ret`` is compared numerically because pytest's ``ExitCode`` gives 2 the member name
    ``INTERRUPTED``: AAP 0.4.7 publishes 2 for a configuration fault, while 4 is pytest's own usage
    error and 5 its no-tests-collected code, and neither of those is acceptable here.
    """
    output = _output(result)
    assert result.ret == _CONFIG_EXIT, (
        f"{case}: a configuration fault must end the run with exit {_CONFIG_EXIT} "
        f"(AAP 0.4.4, AAP 0.4.7, assumption A4) but it answered {result.ret}. "
        "Exit 4 is pytest's unrecognised-argument code and 5 its no-tests-collected code, so "
        f"either answer means the fault never reached Foundry's configuration layer.\n{output}"
    )
    written = sorted(name for name in _REPORT_FILES if (directory / name).exists())
    assert not written, (
        f"{case}: a run that could not be configured writes no report at all (AAP 0.4.7, "
        f"'no collection, no reports'), but {written} exist in {directory}"
    )
    assert _PASS_SUMMARY not in output, (
        f"{case}: the control test must not run, so nothing was collected and the exit code can "
        f"only come from the fault.\n{output}"
    )
    return output


def test_out_of_range_http_timeout_names_the_key_and_its_range(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """Criterion 4: ``http.timeout = -1`` exits 2 naming the key and ``0 < t <= 300``.

    The value travels in ``--option=value`` form, because a bare ``-1`` would be read as a flag by
    any parser. A child process is what makes the code observable: the fault ends the session inside
    ``pytest_configure`` (assumption A4), before a test is collected or a report is written.
    """
    directory = _prepare(pytester)
    result = pytester.runpytest_subprocess(f"{_TIMEOUT_OPTION}=-1")
    output = _assert_exit_2_without_reports(result, directory, case="out-of-range http.timeout")
    assert _TIMEOUT_KEY in output, f"the message must name {_TIMEOUT_KEY}:\n{output}"
    assert _TIMEOUT_RANGE in output, f"the message must quote {_TIMEOUT_RANGE}:\n{output}"


def test_unknown_key_in_the_configuration_file_names_the_closest_key(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """AAP 0.4.7 "Unknown key in any layer": the file layer names the key and the nearest valid one.

    The suggestion is produced by the configuration layer's own ``difflib.get_close_matches`` call,
    and the file layer is the path guaranteed to reach it (AAP 0.4.5), so this case is the reference
    for the wording the command-line case must reproduce.
    """
    directory = _prepare(pytester, foundry_toml=_MISSPELT_TOML)
    result = pytester.runpytest_subprocess()
    output = _assert_exit_2_without_reports(result, directory, case="unknown key in the file layer")
    assert _MISSPELT_KEY in output, f"the message must name {_MISSPELT_KEY}:\n{output}"
    assert _CLOSEST_KEY_HINT in output, f"the message must suggest the closest key:\n{output}"


def test_unknown_key_on_the_command_line_names_the_closest_key(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """AAP 0.4.5: an unknown key is rejected in *any* layer, the command line included, at exit 2.

    This is the one path pytest does not answer by itself. The option group registers one option per
    registered key, so a misspelt dotted key is not an option argparse knows and pytest's own parser
    answers its usage-error code 4 for the unrecognised argument. AAP 0.4.7 publishes 2 for an
    unknown key in any layer, so the plugin registers the spelling as it was typed and hands the
    value to the configuration layer, which rejects it with difflib's nearest key -- the same
    message the file layer produces. The assertion below therefore fails, and says which surface
    deviates, if a run ever answers 4 here: that would mean a misspelt option is again reaching
    pytest's parser instead of Foundry's configuration layer.
    """
    directory = _prepare(pytester)
    result = pytester.runpytest_subprocess(f"--foundry-{_MISSPELT_KEY}=3.0")
    output = _assert_exit_2_without_reports(
        result, directory, case="unknown key on the command line"
    )
    assert _MISSPELT_KEY in output, f"the message must name {_MISSPELT_KEY}:\n{output}"
    assert _CLOSEST_KEY_HINT in output, f"the message must suggest the closest key:\n{output}"


def test_unparseable_environment_value_names_the_variable_and_its_type(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AAP 0.4.7: an unparseable ``FOUNDRY_*`` value names the variable and the expected type.

    The environment layer reports a float as "a number", the wording its own messages publish for
    the expected type AAP 0.4.5 asks them to name. The variable is the only ``FOUNDRY_*`` name in
    the child's environment, because ``clean_foundry_env`` removed every other one.
    """
    monkeypatch.setenv(_TIMEOUT_VARIABLE, "abc")
    directory = _prepare(pytester)
    result = pytester.runpytest_subprocess()
    output = _assert_exit_2_without_reports(result, directory, case="unparseable environment value")
    assert _TIMEOUT_VARIABLE in output, f"the message must name the variable:\n{output}"
    assert _NUMBER_EXPECTED in output, f"the message must name the expected type:\n{output}"


def test_missing_explicit_configuration_file_names_the_path(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """AAP 0.4.7 "Explicit config path missing": ``config file not found: <path>``, exit 2.

    An explicitly named file is never replaced by discovery of another one, so the message repeats
    the path that was typed, which is the whole reason an operator can act on it.
    """
    directory = _prepare(pytester)
    missing = directory / _MISSING_CONFIG_NAME
    result = pytester.runpytest_subprocess("--foundry-config", str(missing))
    output = _assert_exit_2_without_reports(
        result, directory, case="missing explicitly named config file"
    )
    expected = f"config file not found: {missing}"
    assert expected in output, f"the message must read {expected!r}:\n{output}"


def test_absent_secret_variable_is_named_without_its_value(
    pytester: pytest.Pytester, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AAP 0.4.7: an absent configured secret variable is named alone, never with a value.

    ``token_env`` stores a variable *name* (implicit requirement I9), so the fault can only name
    that name. The sentinel exported beside it stands for the credential a deployment would hold:
    were a value ever printed, or the environment echoed, the sentinel would appear in the output.
    """
    monkeypatch.delenv(_TOKEN_VARIABLE, raising=False)
    monkeypatch.setenv(_SENTINEL_VARIABLE, _SENTINEL_VALUE)
    directory = _prepare(pytester, foundry_toml=_BEARER_TOML)
    result = pytester.runpytest_subprocess()
    output = _assert_exit_2_without_reports(result, directory, case="absent bearer token variable")
    assert _TOKEN_VARIABLE in output, f"the message must name the variable:\n{output}"
    assert _SENTINEL_VALUE not in output, (
        "no value may reach the terminal: the configuration stores variable names only "
        f"(implicit requirement I9), yet the sentinel appeared in:\n{output}"
    )


def test_record_and_replay_together_is_a_usage_error(
    pytester: pytest.Pytester, clean_foundry_env: None
) -> None:
    """AAP 0.4.4: supplying both cassette switches is a usage error, exit 2, with no reports.

    The conflict is decided after every option has been read, so the message names both switches
    whichever order they came in, and no run starts far enough to write a report.
    """
    directory = _prepare(pytester)
    result = pytester.runpytest_subprocess(_RECORD_OPTION, _REPLAY_OPTION)
    output = _assert_exit_2_without_reports(result, directory, case="record and replay together")
    assert _RECORD_OPTION in output, f"the message must name {_RECORD_OPTION}:\n{output}"
    assert _REPLAY_OPTION in output, f"the message must name {_REPLAY_OPTION}:\n{output}"


def test_valid_configuration_still_runs(pytester: pytest.Pytester, clean_foundry_env: None) -> None:
    """The control for every case above: the same project, a valid configuration, exit 0.

    Without it a defect in the environment could make all seven negative cases pass for one
    unrelated reason. Here the control test runs, the profile banner shows the plugin configured the
    session, and the three report files AAP 0.4.6 names are written beside it.
    """
    directory = _prepare(pytester, foundry_toml=_VALID_TOML)
    result = pytester.runpytest_subprocess()
    output = _output(result)
    assert result.ret == 0, (
        f"a valid configuration must run green, not exit {result.ret}:\n{output}"
    )
    result.assert_outcomes(passed=1)
    missing = sorted(name for name in _REPORT_FILES if not (directory / name).exists())
    assert not missing, f"a green run writes all three reports; missing: {missing}\n{output}"
    assert "foundry: profile=default" in output, f"the banner must reach the terminal:\n{output}"
