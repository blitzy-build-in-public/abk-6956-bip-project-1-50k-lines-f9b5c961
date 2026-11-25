"""Unit tests for ``foundry.cli``: the argv builder, the command bodies and the renderers.

AAP 0.4.4 fixes what each command does and which code it publishes, AAP 0.7.3 the single document
``--format json`` and ``--json`` emit, and AAP 0.7.4 the three terminal tables. This module pins
that surface through the ``CliRunner``, one command at a time: ``build_pytest_argv`` translates an
option into the canonical ``--foundry-<dotted.key>`` spelling, emits the registry order and then the
caller's pytest arguments verbatim; ``foundry run`` returns pytest's own status; ``config show``
prints one ``{"resolved": ..., "sources": ...}`` document; ``report compare`` exits ``1`` on a new
failure; and the renderers write the columns, the ``(none)`` marker and the ``***`` marker.

The exit code ``2`` a configuration or document fault earns is asserted where it is decided: the
command raises the typed error that mapping consumes and prints nothing on stdout. The run bootstrap
``foundry run`` ends in is covered too. The package's import closure and the single owner of the
override option set are guarded here, so every module ``pkgutil`` finds is imported.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import click
import pytest
from click.testing import CliRunner

import foundry.cli
import foundry.cli.config_cmd as config_cmd
import foundry.cli.run as run_module
from foundry.cli.config_cmd import config_group
from foundry.cli.exit_codes import FAILED, SUCCESS, USAGE_OR_CONFIG
from foundry.cli.render import (
    render_comparison,
    render_config_table,
    render_not_applied,
    render_probe_table,
)
from foundry.cli.report_cmd import report_group
from foundry.cli.run import build_pytest_argv
from foundry.cli.run import run as run_command
from foundry.config import cli_layer
from foundry.config.render import ConfigRow
from foundry.core.exceptions import ConfigError, DataError
from foundry.diagnostics.report import ProbeResult, ProbeStatus
from foundry.execution.session import bootstrap_session
from foundry.obs.redaction import REDACTED, Redactor
from foundry.reporting.compare import ComparisonDiff

_TIMEOUT_OPTION: Final[str] = "--foundry-http.timeout"
"""``http.timeout``'s generic option: the spelling the plugin registers and the argv carries."""

_SECRET: Final[str] = "h34d3r-s3nt1nel"
"""A value assembled at run time, so the literal never reads as a credential to a scanner."""

_LOGICAL_WIDTH: Final[str] = "400"
"""``COLUMNS`` for every captured rendering, so rich never wraps a cell an assertion reads."""

_CLI_SOURCE: Final[str] = "cli:--foundry-http.timeout"
"""The ``Source`` cell AAP 0.7.4 fixes for a value the command line supplied."""

_NOT_APPLIED_SOURCE: Final[str] = "not-applied:--foundry-http.timeout"
"""The ``Source`` cell of a deferred override, which no layer label can be mistaken for."""


# --- The package's import closure (AAP 0.4.4) -----------------------------


_OVERRIDE_PREFIX = "--foundry-"
"""The prefix AAP 0.4.4 gives every option of the override set."""

_OVERRIDE_COUNT = 28
"""Options in the set: one per scalar key, the named aliases, and ``--foundry-config``."""

_RUN_ONLY_OPTIONS: frozenset[str] = frozenset(
    {"--foundry-json-report", "--foundry-record", "--foundry-replay"}
)
"""The three ``foundry run`` options of AAP 0.4.4 that configure no key and only ``run`` declares.

They are the difference between the command that executes a suite and the two that inspect the
configuration, so the shared override set is their common subset rather than an equality.
"""


def _cli_module_names() -> tuple[str, ...]:
    """Return the name of every module the ``foundry.cli`` package holds.

    Read from the package rather than listed, so the modules written after this test still run
    through it: a planned module such as ``foundry.cli.main`` needs no edit here to be covered.
    """
    return tuple(
        sorted(
            f"{foundry.cli.__name__}.{module.name}"
            for module in pkgutil.iter_modules(foundry.cli.__path__)
        )
    )


_CLI_MODULE_NAMES: tuple[str, ...] = _cli_module_names()
"""Every module of ``foundry.cli``, collected once at import."""


def _override_spellings(command: click.Command) -> set[str]:
    """Return the override option spellings ``command`` declares.

    Only the ``--foundry-`` spellings are kept, leaving out a command's own options (``--resolved``,
    ``--format``, ``--json``) and its pass-through arguments.
    """
    return {
        spelling
        for parameter in command.params
        for spelling in parameter.opts
        if spelling.startswith(_OVERRIDE_PREFIX)
    }


# --- Builders -------------------------------------------------------------


def _record(nodeid: str, outcome: str) -> dict[str, object]:
    """Return one ``results.json`` element in the shape AAP 0.4.6 publishes."""
    return {
        "artifacts": [],
        "attempts": 1,
        "duration_ms": 1.5,
        "failures": (
            []
            if outcome == "passed"
            else [{"message": "AssertionError: boom", "phase": "call", "traceback_path": None}]
        ),
        "finished_at": "2024-05-01T12:00:01.000+00:00",
        "markers": [],
        "nodeid": nodeid,
        "outcome": outcome,
        "profile": "default",
        "quarantine": {"state": "none"},
        "record_type": "result",
        "run_id": "20240501T120000-a1b2c3",
        "started_at": "2024-05-01T12:00:00.000+00:00",
    }


def _documents(
    tmp_path: Path,
    base: Sequence[tuple[str, str]],
    new: Sequence[tuple[str, str]],
) -> tuple[Path, Path]:
    """Write two result documents from ``(nodeid, outcome)`` pairs and return their paths."""
    paths: list[Path] = []
    for name, rows in (("base", base), ("new", new)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps([_record(*row) for row in rows]), encoding="utf-8")
        paths.append(path)
    return paths[0], paths[1]


# --- Fixtures -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _logical_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the terminal width for every test here, so rich never wraps a cell an assertion reads."""
    monkeypatch.setenv("COLUMNS", _LOGICAL_WIDTH)


@pytest.fixture
def config(tmp_path: Path, clean_foundry_env: None, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Function scope: a directory with no Foundry configuration, as the process cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- build_pytest_argv (AAP 0.4.4) ---------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "http.timeout",
        _TIMEOUT_OPTION,
        "http_timeout",
        "foundry_http_timeout",
        "foundry__http__timeout",
    ],
)
def test_every_way_of_naming_a_key_emits_the_canonical_spelling(name: str) -> None:
    """The dotted key, the option spelling and the parameter names all become one spelling."""
    assert build_pytest_argv({name: "1.0"}, []) == [_TIMEOUT_OPTION, "1.0"]


@pytest.mark.parametrize(
    ("word", "key"),
    [("profile", "profile"), ("timeout", "execution.timeout"), ("retries", "execution.retries")],
)
def test_an_alias_word_emits_the_key_it_aliases(word: str, key: str) -> None:
    """A named option is an alias onto one specific key (AAP 0.4.4), and its key is emitted."""
    assert build_pytest_argv({word: "1"}, []) == [f"--foundry-{key}", "1"]


def test_the_argv_orders_the_registry_then_the_special_options_then_the_pytest_arguments() -> None:
    """The order is fixed, so two runs and two workers build the same list."""
    supplied = {
        _TIMEOUT_OPTION: "1.5",
        "--foundry-record": True,
        "--foundry-profile": "ci",
        "--foundry-json-report": "results.json",
    }
    argv = build_pytest_argv(supplied, ["tests/unit", "-q"])
    assert argv == [
        "--foundry-profile",
        "ci",
        _TIMEOUT_OPTION,
        "1.5",
        "--foundry-json-report",
        "results.json",
        "--foundry-record",
        "tests/unit",
        "-q",
    ]


@pytest.mark.parametrize("switch", ["--foundry-record", "--foundry-replay"])
def test_a_switch_is_emitted_without_a_value(switch: str) -> None:
    """The two mode switches take no value, so pytest never reads one as an argument."""
    assert build_pytest_argv({switch: True}, []) == [switch]


@pytest.mark.parametrize("off", [False, "false", "off", ""])
def test_a_switch_supplied_as_off_is_not_emitted(off: object) -> None:
    """A switch read as "off" configures nothing, so no argument appears for it."""
    assert build_pytest_argv({"--foundry-record": off}, []) == []


def test_an_unsupplied_option_is_skipped() -> None:
    """``None`` is how a parser reports an option nobody typed, and it produces no argument."""
    assert build_pytest_argv({_TIMEOUT_OPTION: None, "profile": None}, ["-q"]) == ["-q"]


def test_an_option_that_names_no_key_raises_naming_it() -> None:
    """An override that cannot be passed on is reported rather than dropped silently."""
    with pytest.raises(ConfigError) as raised:
        build_pytest_argv({"http.timeot": "1.0"}, [])
    assert raised.value.key == "http.timeot"
    assert "http.timeot" in str(raised.value)


def test_the_callers_mapping_and_arguments_are_left_untouched() -> None:
    """The builder is pure: the same inputs twice give the same list, and nothing is mutated."""
    supplied: dict[str, object] = {_TIMEOUT_OPTION: 1.0, "--foundry-record": True}
    passthrough = ["tests/unit", "-q", "--maxfail", "1", "-q"]
    before = dict(supplied)
    first = build_pytest_argv(supplied, passthrough)
    second = build_pytest_argv(supplied, passthrough)
    assert first == second
    assert supplied == before
    assert passthrough == ["tests/unit", "-q", "--maxfail", "1", "-q"]
    assert first[len(first) - len(passthrough) :] == passthrough


# --- foundry config show / validate (AAP 0.4.4, AAP 0.7.4) --------------


def test_config_show_prints_the_key_value_source_table(config: Path) -> None:
    """The default rendering is the three-column table, in the order AAP 0.7.4 fixes."""
    result = CliRunner().invoke(config_group, ["show"])
    printed = result.output
    assert result.exit_code == SUCCESS
    assert "Key" in printed
    assert "Value" in printed
    assert "Source" in printed
    assert "http.timeout" in printed
    assert "10.0" in printed
    assert "default" in printed
    assert printed.index("Key") < printed.index("http.timeout")


def test_config_show_json_emits_the_two_documented_members(config: Path) -> None:
    """Without a deferred override the document is exactly ``resolved`` and ``sources``."""
    result = CliRunner().invoke(config_group, ["show", "--format", "json"])
    assert result.exit_code == SUCCESS
    document = json.loads(result.output)
    assert set(document) == {"resolved", "sources"}
    assert document["resolved"]["http"]["timeout"] == 10.0
    assert document["sources"]["http.timeout"] == {"layer": "default", "detail": ""}
    assert document["resolved"]["cassette"]["mode"] == "off"


def test_config_show_json_reports_a_deferred_override_separately(config: Path) -> None:
    """A run without ``--resolved`` reports the override it did not apply, additively."""
    result = CliRunner().invoke(config_group, ["show", "--format", "json", _TIMEOUT_OPTION, "1.0"])
    assert result.exit_code == SUCCESS
    document = json.loads(result.output)
    assert set(document) == {"resolved", "sources", "not_applied"}
    assert document["not_applied"] == {"http.timeout": "1.0"}
    assert document["resolved"]["http"]["timeout"] == 10.0


def test_config_show_resolved_attributes_the_override_to_the_option(config: Path) -> None:
    """With ``--resolved`` the value is applied and its Source cell names the option typed."""
    result = CliRunner().invoke(config_group, ["show", "--resolved", _TIMEOUT_OPTION, "1.0"])
    printed = result.output
    assert result.exit_code == SUCCESS
    row = next(line for line in printed.splitlines() if "http.timeout" in line)
    assert "1.0" in row
    assert _CLI_SOURCE in row


def test_config_show_lists_an_unapplied_override_apart(config: Path) -> None:
    """Without ``--resolved`` the override changes nothing and is reported in its own section."""
    result = CliRunner().invoke(config_group, ["show", _TIMEOUT_OPTION, "1.0"])
    printed = result.output
    assert result.exit_code == SUCCESS
    assert "not applied" in printed
    assert _NOT_APPLIED_SOURCE in printed
    resolved_row = next(
        line for line in printed.splitlines() if _CLI_SOURCE not in line and "http.timeout" in line
    )
    assert "10.0" in resolved_row


def test_config_show_refuses_a_value_outside_its_range(config: Path) -> None:
    """``http.timeout = -1`` is the configuration fault the command line maps to exit ``2``."""
    result = CliRunner().invoke(config_group, ["show", "--resolved", _TIMEOUT_OPTION, "-1"])
    assert result.stdout == ""
    assert isinstance(result.exception, ConfigError)
    assert result.exception.key == "http.timeout"
    assert "0 < t <= 300" in str(result.exception)
    assert USAGE_OR_CONFIG == 2


def test_config_show_refuses_an_option_that_names_no_key(config: Path) -> None:
    """A spelling click does not know is a usage error, and click exits ``2`` for it."""
    result = CliRunner().invoke(config_group, ["show", "--foundry-http.timeot", "1.0"])
    assert result.exit_code == USAGE_OR_CONFIG
    assert "No such option" in result.output


def test_config_validate_reports_the_profile(config: Path) -> None:
    """A valid configuration prints the one line AAP 0.4.4 publishes and exits ``0``."""
    result = CliRunner().invoke(config_group, ["validate"])
    assert result.exit_code == SUCCESS
    assert result.output.strip() == "configuration valid (profile=default)"


def test_config_validate_refuses_an_unusable_override(config: Path) -> None:
    """The command validates what a run would use, so the same override is refused here."""
    result = CliRunner().invoke(config_group, ["validate", _TIMEOUT_OPTION, "0"])
    assert result.stdout == ""
    assert isinstance(result.exception, ConfigError)


# --- foundry report compare (AAP 0.4.4, AAP 0.7.3) ----------------------


def test_report_compare_prints_the_three_groups_in_their_fixed_order(tmp_path: Path) -> None:
    """A regression is exit ``1``, and all three groups print in their fixed order."""
    base = [("tests/a.py::test_one", "passed"), ("tests/a.py::test_two", "failed")]
    new = [("tests/a.py::test_one", "failed"), ("tests/a.py::test_two", "failed")]
    base_path, new_path = _documents(tmp_path, base, new)
    result = CliRunner().invoke(report_group, ["compare", str(base_path), str(new_path)])
    printed = result.output
    assert result.exit_code == FAILED
    labels = [line for line in printed.splitlines() if line.endswith("):")]
    assert labels == ["Newly failing (1):", "Newly passing (0):", "Still failing (1):"]
    assert "  (none)" in printed
    assert "  tests/a.py::test_one" in printed


def test_report_compare_exits_zero_when_nothing_newly_fails(tmp_path: Path) -> None:
    """A standing failure and a recovery are both exit ``0``: neither is news about this run."""
    base = [("tests/a.py::test_one", "failed")]
    new = [("tests/a.py::test_one", "failed"), ("tests/a.py::test_two", "passed")]
    base_path, new_path = _documents(tmp_path, base, new)
    result = CliRunner().invoke(report_group, ["compare", str(base_path), str(new_path), "--json"])
    assert result.exit_code == SUCCESS
    document = json.loads(result.stdout)
    assert document == {
        "newly_failing": [],
        "newly_passing": [],
        "still_failing": ["tests/a.py::test_one"],
    }


def test_report_compare_json_emits_the_three_sets(tmp_path: Path) -> None:
    """``--json`` is one document with the three sorted node-id sets and nothing beside it."""
    base = [("tests/a.py::test_one", "failed"), ("tests/a.py::test_two", "passed")]
    new = [("tests/a.py::test_one", "passed"), ("tests/a.py::test_two", "failed")]
    base_path, new_path = _documents(tmp_path, base, new)
    result = CliRunner().invoke(report_group, ["compare", str(base_path), str(new_path), "--json"])
    assert result.exit_code == FAILED
    assert set(json.loads(result.stdout)) == {"newly_failing", "newly_passing", "still_failing"}
    assert json.loads(result.stdout)["newly_failing"] == ["tests/a.py::test_two"]


@pytest.mark.parametrize("document", ["missing", "not a document"])
def test_report_compare_refuses_a_document_it_cannot_read(tmp_path: Path, document: str) -> None:
    """An unreadable document raises the typed error the command line maps to ``2``."""
    base_path, new_path = _documents(tmp_path, [("tests/a.py::test_one", "passed")], [])
    target = tmp_path / "target.json"
    if document == "not a document":
        target.write_text("this is not JSON", encoding="utf-8")
    result = CliRunner().invoke(report_group, ["compare", str(base_path), str(target)])
    assert result.stdout == ""
    assert isinstance(result.exception, DataError)
    assert result.exception.file == target
    assert USAGE_OR_CONFIG == 2


# --- foundry run (AAP 0.4.4, AAP 0.7.3) --------------------------------


def test_run_hands_pytest_the_argv_the_builder_produces(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recorded argument list is the builder's own output, and its status is returned."""
    calls: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        """Record the argument list pytest would have been given."""
        calls.append(list(argv))
        return 5

    monkeypatch.setattr("pytest.main", fake_main)
    result = CliRunner().invoke(
        run_command, [_TIMEOUT_OPTION, "1.0", "tests/unit", "-q"], standalone_mode=False
    )
    assert result.return_value == 5
    assert calls == [build_pytest_argv({_TIMEOUT_OPTION: "1.0"}, ("tests/unit", "-q"))]
    assert calls[0] == [_TIMEOUT_OPTION, "1.0", "tests/unit", "-q"]


@pytest.mark.parametrize("code", [0, 1, 3, 4, 5])
def test_run_returns_pytests_own_code(
    config: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Foundry never rewrites a code pytest produced, and never invents one."""
    monkeypatch.setattr("pytest.main", lambda argv: code)
    result = CliRunner().invoke(run_command, ["-q"], standalone_mode=False)
    assert result.return_value == code


def test_run_normalises_a_missing_status_to_success(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that produced no status at all publishes ``0``."""
    monkeypatch.setattr("pytest.main", lambda argv: None)
    result = CliRunner().invoke(run_command, ["-q"], standalone_mode=False)
    assert result.return_value == SUCCESS


def test_run_does_not_start_pytest_when_the_configuration_cannot_be_resolved(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configuration fault is raised before pytest starts, so nothing is collected."""
    calls: list[list[str]] = []
    monkeypatch.setattr("pytest.main", lambda argv: calls.append(list(argv)) or 0)
    result = CliRunner().invoke(run_command, [_TIMEOUT_OPTION, "-1", "-q"])
    assert calls == []
    assert isinstance(result.exception, ConfigError)
    assert result.exception.key == "http.timeout"


# --- foundry.cli.render (AAP 0.7.4) ------------------------------------


def test_render_config_table_prints_the_three_columns(capsys: pytest.CaptureFixture[str]) -> None:
    """The headings are exactly ``Key``, ``Value`` and ``Source``; rows print in arrival order."""
    rows = [
        ConfigRow(key="http.timeout", value="10.0", source="default"),
        ConfigRow(key="cassette.mode", value="off", source="file:foundry.toml"),
    ]
    render_config_table(rows)
    printed = capsys.readouterr().out
    for column in ("Key", "Value", "Source"):
        assert column in printed
    assert printed.index("http.timeout") < printed.index("cassette.mode")
    assert "file:foundry.toml" in printed


def test_render_config_table_masks_a_secret_a_value_carries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A header value the run masks is rendered as the marker, never as the credential."""
    redactor = Redactor(["x-api-key"])
    rows = [ConfigRow(key="http.headers", value=f"x-api-key: {_SECRET}", source="default")]
    render_config_table(rows, redactor=redactor)
    printed = capsys.readouterr().out
    assert _SECRET not in printed
    assert REDACTED in printed


def test_render_not_applied_prints_its_own_section(capsys: pytest.CaptureFixture[str]) -> None:
    """The deferred overrides get the literal heading ``not applied`` and the same three columns."""
    rows = [ConfigRow(key="http.timeout", value="1.0", source=_NOT_APPLIED_SOURCE)]
    render_not_applied(rows)
    printed = capsys.readouterr().out
    assert "not applied" in printed
    assert "http.timeout" in printed
    assert _NOT_APPLIED_SOURCE in printed


def test_render_not_applied_prints_nothing_when_nothing_was_deferred(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A command that applied everything it was given has no section to print."""
    render_not_applied([])
    assert capsys.readouterr().out == ""


def test_render_probe_table_prints_the_columns_and_the_count_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Three rows with the three status words, then the closing count line and the verdict."""
    rows = [
        ProbeResult(name="interpreter", status=ProbeStatus.PASS, detail="CPython 3.13.0 on linux"),
        ProbeResult(name="cassettes", status=ProbeStatus.WARN, detail="/x not created yet"),
        ProbeResult(name="plugin", status=ProbeStatus.FAIL, detail="not registered"),
        ProbeResult(
            name="socket_guard", status=ProbeStatus.WARN, detail="no guard", required=False
        ),
    ]
    render_probe_table(rows, ok=False)
    printed = capsys.readouterr().out
    assert "Probe" in printed
    assert "Status" in printed
    assert "Detail" in printed
    assert "CPython 3.13.0 on linux" in printed
    assert "probes: 1 pass, 2 warn, 1 fail (failed)" in printed


def test_render_probe_table_repeats_the_verdict_it_was_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``ok=True`` prints ``ok``: a warning never clears the verdict the command publishes."""
    render_probe_table(
        [ProbeResult(name="cassettes", status=ProbeStatus.WARN, detail="absent")], ok=True
    )
    assert "probes: 0 pass, 1 warn, 0 fail (ok)" in capsys.readouterr().out


def test_render_comparison_prints_the_groups_in_their_fixed_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The three labels print in order, with their sizes, and an empty group shows ``(none)``."""
    diff = ComparisonDiff(
        newly_failing=("tests/b.py::test_two", "tests/a.py::test_one"),
        newly_passing=(),
        still_failing=("tests/c.py::test_three",),
    )
    render_comparison(diff)
    lines = [line.rstrip() for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines == [
        "Newly failing (2):",
        "  tests/a.py::test_one",
        "  tests/b.py::test_two",
        "Newly passing (0):",
        "  (none)",
        "Still failing (1):",
        "  tests/c.py::test_three",
    ]


# --- The run bootstrap foundry run ends in (AAP 0.4.2) ------------------


def test_the_bootstrap_fixes_the_run_id_the_profile_and_the_artifact_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resolution, one run id, ``artifacts/<run_id>/`` created and the two banner lines."""
    monkeypatch.chdir(tmp_path)
    state = bootstrap_session(invocation_dir=tmp_path, environ={})
    assert state.profile == "default"
    assert state.invocation_dir == tmp_path.resolve()
    assert state.artifacts_root == (tmp_path / "artifacts" / state.run_id).resolve()
    assert state.artifacts_root.is_dir()
    assert state.artifacts_enabled is True
    assert state.banner_lines() == (
        "foundry: profile=default",
        f"foundry: run {state.run_id} artifacts at {state.artifacts_root.as_posix()}",
    )
    assert state.source_of("http.timeout").layer == "default"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["artifacts"]


def test_the_bootstrap_applies_the_command_lines_overrides(tmp_path: Path) -> None:
    """The overrides the command line supplied reach the run and are attributed to ``cli``."""
    overrides = cli_layer.from_mapping({"--foundry-profile": "ci", _TIMEOUT_OPTION: "1.0"})
    state = bootstrap_session(cli_input=overrides, invocation_dir=tmp_path, environ={})
    assert state.profile == "ci"
    assert state.config.http.timeout == 1.0
    assert state.banner_lines()[0] == "foundry: profile=ci"
    assert state.source_of("profile").layer == "cli"
    assert state.source_of("http.timeout").label() == _CLI_SOURCE


def test_the_bootstrap_builds_no_state_when_the_configuration_is_unusable(tmp_path: Path) -> None:
    """A fault raises before the run id is minted and before any directory exists."""
    with pytest.raises(ConfigError):
        bootstrap_session(invocation_dir=tmp_path, environ={"FOUNDRY_HTTP__TIMEOUT": "-1"})
    assert list(tmp_path.iterdir()) == []


# --- The import closure and the option set's single owner (AAP 0.4.4) ----


def test_cli_module_discovery_finds_the_import_targets() -> None:
    """The collected package contents include the decorator's home and the command that uses it."""
    assert "foundry.cli.config_cmd" in _CLI_MODULE_NAMES
    assert "foundry.cli.run" in _CLI_MODULE_NAMES


@pytest.mark.parametrize("module_name", _CLI_MODULE_NAMES)
def test_every_cli_module_imports(module_name: str) -> None:
    """Each module of ``foundry.cli`` imports on its own, without the group builder."""
    assert importlib.import_module(module_name) is not None


def test_override_options_has_a_single_owner() -> None:
    """``run`` binds the decorator ``config_cmd`` defines, which that module also exports."""
    assert run_module.override_options is config_cmd.override_options
    assert "override_options" in config_cmd.__all__


def test_config_commands_register_the_same_override_set() -> None:
    """``run``, ``config show`` and ``config validate`` share one override set, plus run's switches.

    AAP 0.4.4 gives ``run`` the record, replay and report-path options as well, so the shared set is
    the subset the three commands have in common rather than an equality with the two inspectors.
    """
    run_set = _override_spellings(run_module.run)
    show_set = _override_spellings(config_cmd.config_group.commands["show"])
    validate_set = _override_spellings(config_cmd.config_group.commands["validate"])

    assert run_set == show_set | _RUN_ONLY_OPTIONS
    assert show_set == validate_set
    assert len(show_set) == _OVERRIDE_COUNT
