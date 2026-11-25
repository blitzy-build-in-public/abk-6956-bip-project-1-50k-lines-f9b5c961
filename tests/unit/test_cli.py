"""Unit tests for ``foundry.cli``: the command line surface, its exit codes and its renderers.

AAP 0.4.4 fixes what each subcommand does and which code it publishes, AAP 0.7.3 the single document
``--format json`` and ``--json`` emit, AAP 0.7.4 the terminal tables, and AAP 0.1.2 the lines
``--version`` and ``config validate`` print. Every invocation is in process (AAP 0.14.5): the click
group is driven through ``CliRunner``, and exit codes are read from
:func:`foundry.cli.main.main`, the ``[project.scripts]`` target, because click discards a callback's
return value in standalone mode and that function is the one place AAP 0.4.4's mapping is applied.
The installed console script and any ``pytest`` child process belong to ``tests/e2e``.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import shutil
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final, NamedTuple

import pytest
from click.testing import CliRunner

import foundry.cli
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
from foundry.cli.render import render_comparison
from foundry.cli.run import build_pytest_argv
from foundry.cli.run import run as run_command
from foundry.core.exceptions import ConfigError
from foundry.diagnostics import checks
from foundry.reporting.compare import ComparisonDiff

_Capture = pytest.CaptureFixture[str]
_Monkey = pytest.MonkeyPatch

_TIMEOUT_KEY: Final[str] = "http.timeout"
"""The key criterion 4 drives out of range and every fault message has to name."""

_TIMEOUT_OPTION: Final[str] = "--foundry-http.timeout"
"""``http.timeout``'s generic override spelling, registered for every scalar key (AAP 0.4.4)."""

_TIMEOUT_RANGE: Final[str] = "0 < t <= 300"
"""``http.timeout``'s accepted range, quoted as AAP 0.4.5 spells it."""

_VERSION_BANNER: Final[str] = "foundry, version 0.1.0"
"""The banner of AAP 0.1.2, preserved character for character."""

_VALID_LINE: Final[str] = "configuration valid (profile=default)"
"""The success line of AAP 0.1.2 and AAP 0.4.4, with the default profile interpolated."""

_SUBCOMMANDS: Final[tuple[str, ...]] = ("run", "init", "config", "report", "doctor")
"""AAP 0.4.4's five subcommands, in the order the group's help lists them."""

_WIDE_COLUMNS: Final[str] = "400"
"""``COLUMNS`` for every rendering, so rich never wraps a cell an assertion reads."""

_SECRET: Final[str] = "s3ntinel-" + "header-value"
"""A masked value, assembled at run time so no scanner reads the literal as a credential."""

_REDACTION_MARKER: Final[str] = "***"
"""The marker the redaction engine writes in place of a masked value (AAP 0.4.5)."""

_TOKEN_VARIABLE: Final[str] = "CHECKOUT_API_TOKEN"
"""The variable an auth strategy names, never its value (implicit requirement I9).

Outside the ``FOUNDRY_`` prefix on purpose: that prefix is the environment layer's own namespace.
"""

_VALID_TOML: Final[str] = "[http]\ntimeout = 2.0\n"
"""A valid configuration file, for the positive controls; ``-1`` is criterion 4's fault."""

_SECRET_TOML: Final[str] = (
    "[http]\ntimeout = 2.0\n\n"
    f'[http.headers]\nAuthorization = "Bearer {_SECRET}"\n\n'
    f'[http.auth]\nstrategy = "bearer"\ntoken_env = "{_TOKEN_VARIABLE}"\n'
)
"""A configuration holding a credential a header and an auth strategy both name (AAP 0.4.5)."""

_MINIMAL_SCAFFOLD: Final[tuple[str, ...]] = (
    "foundry.toml",
    ".gitignore",
    "README.md",
    "tests/test_smoke.py",
    "testdata/orders.csv",
    "testdata/orders.schema.json",
)
"""What ``foundry init`` writes, in the order it prints them (AAP 0.7.4, I11)."""


class _CliRun(NamedTuple):
    """One in-process run of the command line: the status it published and what it printed."""

    code: int
    stdout: str
    stderr: str


def _run_cli(capsys: _Capture, *args: str) -> _CliRun:
    """Run the ``[project.scripts]`` target in process, where AAP 0.4.4's mapping is applied."""
    code = main(list(args))
    captured = capsys.readouterr()
    return _CliRun(code=code, stdout=captured.out, stderr=captured.err)


def _write_config(directory: Path, text: str) -> Path:
    """Write ``foundry.toml`` into ``directory`` and return its path."""
    path = directory / "foundry.toml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _snapshot(directory: Path) -> tuple[str, ...]:
    """Return every entry under ``directory``, relative and sorted, so two states compare."""
    return tuple(sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*")))


def _copied_documents(directory: Path, repo_root: Path) -> tuple[Path, Path, Path]:
    """Copy the three committed result documents into ``directory``, the only tree written."""
    fixtures = repo_root / "tests" / "fixtures"
    base = directory / "results_base.json"
    new = directory / "results_new.json"
    unreadable = directory / "results_unreadable.txt"
    for path in (base, new, unreadable):
        shutil.copy(fixtures / path.name, path)
    return base, new, unreadable


def _patch_probes(monkeypatch: _Monkey, overrides: Mapping[str, str] | None = None) -> None:
    """Replace every doctor probe with a fixed row reporting ``pass`` unless ``overrides`` says.

    The patch goes through :mod:`foundry.diagnostics.checks`, which owns the probe order and calls
    each probe by name, so the command runs its real report model and exit-code mapping while the
    verdict is deterministic. Nothing on disk is edited.
    """
    probes = {
        "interpreter": "check_interpreter",
        "plugin": "check_plugin_registered",
        "configuration": "check_configuration",
        "artifacts": "check_artifacts_dir",
        "cassettes": "check_cassette_dir",
        "socket_guard": "check_socket_guard",
    }
    wanted = dict(overrides or {})
    for probe, function in probes.items():
        row = checks.ProbeResult(
            name=probe,
            status=checks.ProbeStatus(wanted.get(probe, "pass")),
            detail=f"patched {probe}",
            required=probe in checks.REQUIRED_PROBES,
        )
        monkeypatch.setattr(checks, function, lambda *args, _row=row, **kwargs: _row)


def _command_names(help_text: str) -> list[str]:
    """Return the subcommand names of the help text's ``Commands:`` section, in printed order."""
    lines = help_text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "Commands:"), None)
    if start is None:
        pytest.fail(f"the help text has no 'Commands:' section:\n{help_text}")
    names: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        names.append(line.split()[0])
    return names


@pytest.fixture(autouse=True)
def _logical_width(monkeypatch: _Monkey) -> None:
    """Pin the terminal width, so rich never wraps a cell an assertion reads."""
    monkeypatch.setenv("COLUMNS", _WIDE_COLUMNS)


@pytest.fixture
def project(tmp_path: Path, clean_foundry_env: None, monkeypatch: _Monkey) -> Path:
    """Function scope: an empty project directory with no Foundry configuration, as the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_code_vocabulary_is_the_published_one() -> None:
    """``0`` success, ``1`` failure, ``2`` Foundry's own fault; pytest's ``3``/``4``/``5`` kept."""
    assert (SUCCESS, FAILED, USAGE_OR_CONFIG) == (0, 1, 2)
    assert (PYTEST_INTERNAL_ERROR, PYTEST_USAGE_ERROR, PYTEST_NO_TESTS) == (3, 4, 5)
    for pytest_code in (PYTEST_INTERNAL_ERROR, PYTEST_USAGE_ERROR, PYTEST_NO_TESTS):
        assert is_pytest_code(pytest_code) is True
    for foundry_code in (SUCCESS, FAILED, USAGE_OR_CONFIG):
        assert is_pytest_code(foundry_code) is False


@pytest.mark.parametrize(("given", "expected"), [(0, 0), (1, 1), (3, 3), (4, 4), (5, 5), (None, 0)])
def test_passthrough_returns_every_code_unchanged_and_normalises_none(
    given: int | None, expected: int
) -> None:
    """A rewrite that collapsed pytest's ``4`` into Foundry's ``2``, or ``None``, fails here."""
    assert passthrough(given) == expected


def test_the_entry_point_maps_a_configuration_fault_and_a_usage_fault_to_two(
    project: Path, capsys: _Capture
) -> None:
    """An out-of-range value names the key and its range on stderr; an unknown option is ``2``."""
    _write_config(project, "[http]\ntimeout = -1\n")

    from_file = _run_cli(capsys, "config", "validate")
    from_option = _run_cli(capsys, "config", "validate", _TIMEOUT_OPTION, "0")
    misused = _run_cli(capsys, "config", "show", "--foundry-nope", "1.0")

    for run in (from_file, from_option):
        assert run.code == USAGE_OR_CONFIG
        assert run.stdout == ""
        assert _TIMEOUT_KEY in run.stderr
        assert _TIMEOUT_RANGE in run.stderr
    assert misused.code == USAGE_OR_CONFIG
    assert "No such option" in misused.stderr


# --- The click group and its help pages (AAP 0.4.4, AAP 0.7.4) -----------


def test_the_group_help_lists_the_five_subcommands_and_the_banner_is_exact() -> None:
    """Activation is not a flag, and AAP 0.1.2 fixes the banner character for character."""
    help_result = CliRunner().invoke(build_group(), ["--help"])
    version_result = CliRunner().invoke(build_group(), ["--version"])

    assert help_result.exit_code == SUCCESS, help_result.output
    assert _command_names(help_result.output) == list(_SUBCOMMANDS)
    assert version_result.exit_code == SUCCESS, version_result.output
    assert version_result.output.strip() == _VERSION_BANNER


@pytest.mark.parametrize(
    ("arguments", "documented"),
    [
        (("run",), "--foundry-profile"),
        (("init",), "--template"),
        (("config", "show"), "--resolved"),
        (("config", "validate"), "--foundry-config"),
        (("report", "compare"), "--json"),
        (("doctor",), "--json"),
    ],
)
def test_each_subcommand_help_lists_only_its_documented_options(
    arguments: tuple[str, ...], documented: str
) -> None:
    """Both matter: the documented option is discoverable, an unpublished one is absent."""
    result = CliRunner().invoke(build_group(), [*arguments, "--help"])

    assert result.exit_code == SUCCESS, result.output
    assert documented in result.output
    assert "--foundry-nope" not in result.output


# --- foundry config show / validate (AAP 0.4.4, AAP 0.4.5, AAP 0.7.4) ----


def test_config_show_prints_the_key_value_source_table(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    """Three columns, one row per key, and the three layer labels a project produces."""
    _write_config(project, _VALID_TOML)
    monkeypatch.setenv("FOUNDRY_DATA__DIR", "elsewhere")

    run = _run_cli(capsys, "config", "show")
    printed = run.stdout

    assert run.code == SUCCESS, run.stderr
    assert printed.index("Key") < printed.index(_TIMEOUT_KEY)
    for column in ("Key", "Value", "Source"):
        assert column in printed
    timeout_row = next(line for line in printed.splitlines() if _TIMEOUT_KEY in line)
    assert "2.0" in timeout_row
    assert "file:" in timeout_row
    assert "env:FOUNDRY_DATA__DIR" in printed
    assert "default" in printed


def test_config_show_emits_one_document_per_machine_readable_format(
    project: Path, capsys: _Capture
) -> None:
    """Each format prints one document: a stray rich line would break the parse below."""
    _write_config(project, _VALID_TOML)

    as_json = _run_cli(capsys, "config", "show", "--format", "json")
    as_toml = _run_cli(capsys, "config", "show", "--format", "toml")

    assert as_json.code == SUCCESS, as_json.stderr
    document = json.loads(as_json.stdout)
    assert set(document) == {"resolved", "sources"}
    assert document["resolved"]["http"]["timeout"] == 2.0
    assert document["sources"][_TIMEOUT_KEY]["layer"] == "file"
    assert document["sources"][_TIMEOUT_KEY]["detail"] == str(project / "foundry.toml")

    assert as_toml.code == SUCCESS, as_toml.stderr
    parsed = tomllib.loads(as_toml.stdout)
    assert parsed["http"]["timeout"] == 2.0
    assert parsed["sources"][_TIMEOUT_KEY]["layer"] == "file"


def test_config_show_applies_or_defers_an_override_and_validate_reports_success(
    project: Path, capsys: _Capture
) -> None:
    """``--resolved`` attributes the value to ``cli:``, and ``validate`` prints its success line."""
    applied = _run_cli(capsys, "config", "show", "--resolved", _TIMEOUT_OPTION, "1.0")
    deferred = _run_cli(capsys, "config", "show", _TIMEOUT_OPTION, "1.0")

    assert applied.code == SUCCESS, applied.stderr
    applied_row = next(line for line in applied.stdout.splitlines() if _TIMEOUT_KEY in line)
    assert "1.0" in applied_row
    assert "cli:--foundry-http.timeout" in applied_row

    assert deferred.code == SUCCESS, deferred.stderr
    assert "not applied" in deferred.stdout
    assert "not-applied:--foundry-http.timeout" in deferred.stdout
    resolved_row = next(
        line
        for line in deferred.stdout.splitlines()
        if _TIMEOUT_KEY in line and "not-applied:" not in line
    )
    assert "10.0" in resolved_row

    valid = _run_cli(capsys, "config", "validate")

    assert valid.code == SUCCESS, valid.stderr
    assert valid.stdout.strip() == _VALID_LINE
    assert valid.stderr == ""


# --- foundry report compare (AAP 0.4.4, AAP 0.7.3, AAP 0.7.4) ------------


def test_compare_prints_the_three_groups_in_order_and_its_json_document(
    tmp_path: Path, repo_root: Path, capsys: _Capture
) -> None:
    """A regression is exit ``1``, the labels keep their order, and ``--json`` replaces them."""
    base, new, _ = _copied_documents(tmp_path, repo_root)

    printed = _run_cli(capsys, "report", "compare", str(base), str(new))
    as_json = _run_cli(capsys, "report", "compare", str(base), str(new), "--json")

    assert printed.code == FAILED, printed.stderr
    labels = [line for line in printed.stdout.splitlines() if line.endswith("):")]
    assert labels == ["Newly failing (1):", "Newly passing (1):", "Still failing (1):"]
    assert "  tests/test_checkout.py::test_payment_ok" in printed.stdout

    assert as_json.code == FAILED, as_json.stderr
    assert json.loads(as_json.stdout) == {
        "newly_failing": ["tests/test_checkout.py::test_payment_ok"],
        "newly_passing": ["tests/test_checkout.py::test_payment_declined"],
        "still_failing": ["tests/test_cart.py::test_two_items"],
    }


def test_compare_of_two_identical_documents_exits_zero_and_one_that_is_unreadable_two(
    tmp_path: Path, repo_root: Path, capsys: _Capture
) -> None:
    """A standing failure alone is not news, and a document Foundry cannot read is a usage fault."""
    base, new, unreadable = _copied_documents(tmp_path, repo_root)
    same = tmp_path / "same.json"
    shutil.copy(new, same)

    identical = _run_cli(capsys, "report", "compare", str(new), str(same), "--json")
    broken = _run_cli(capsys, "report", "compare", str(base), str(unreadable))

    assert identical.code == SUCCESS, identical.stderr
    assert json.loads(identical.stdout) == {
        "newly_failing": [],
        "newly_passing": [],
        "still_failing": [
            "tests/test_cart.py::test_two_items",
            "tests/test_checkout.py::test_payment_ok",
        ],
    }

    assert broken.code == USAGE_OR_CONFIG
    assert broken.stdout == ""
    assert unreadable.name in broken.stderr


# --- foundry doctor (AAP 0.4.4, AAP 0.7.3, AAP 0.7.4) --------------------


def test_doctor_prints_the_table_and_the_json_document_of_a_passing_environment(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    """``0`` when every required probe passes, with the three columns and the published document."""
    _patch_probes(monkeypatch)

    table = _run_cli(capsys, "doctor")
    document_run = _run_cli(capsys, "doctor", "--json")

    assert table.code == SUCCESS, table.stderr
    for column in ("Probe", "Status", "Detail"):
        assert column in table.stdout
    assert "(ok)" in table.stdout

    assert document_run.code == SUCCESS, document_run.stderr
    document = json.loads(document_run.stdout)
    assert set(document) == {"interpreter", "checks", "ok"}
    assert document["ok"] is True
    assert [check["name"] for check in document["checks"]] == [
        "interpreter",
        "plugin",
        "configuration",
        "artifacts",
        "cassettes",
        "socket_guard",
    ]
    for check in document["checks"]:
        assert set(check) == {"name", "status", "detail"}
    assert set(document["interpreter"]) == {"implementation", "version", "platform", "supported"}


def test_doctor_exits_two_when_a_required_probe_fails(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    """One required ``fail`` publishes ``2`` and clears the document's verdict."""
    _patch_probes(monkeypatch, {"artifacts": "fail"})

    run = _run_cli(capsys, "doctor", "--json")

    assert run.code == USAGE_OR_CONFIG
    document = json.loads(run.stdout)
    assert document["ok"] is False
    failed = next(check for check in document["checks"] if check["name"] == "artifacts")
    assert failed["status"] == "fail"


@pytest.mark.parametrize("overrides", [{"configuration": "warn"}, {"socket_guard": "fail"}])
def test_doctor_keeps_zero_for_a_warning_and_for_the_optional_probe(
    project: Path, monkeypatch: _Monkey, capsys: _Capture, overrides: dict[str, str]
) -> None:
    """A warning never changes the code, and neither does the one probe that is not required."""
    _patch_probes(monkeypatch, overrides)

    run = _run_cli(capsys, "doctor", "--json")

    assert run.code == SUCCESS, run.stderr
    assert json.loads(run.stdout)["ok"] is True


# --- foundry init (AAP 0.4.4, AAP 0.7.4, implicit requirement I11) -------


def test_init_writes_the_published_minimal_scaffold(tmp_path: Path, capsys: _Capture) -> None:
    """Every planned path is printed once, in plan order, and exists with its ignore entry."""
    target = tmp_path / "demo"

    run = _run_cli(capsys, "init", str(target))
    printed = [line.strip() for line in run.stdout.splitlines() if line.strip()]

    assert run.code == SUCCESS, run.stderr
    assert printed == [(target / name).as_posix() for name in _MINIMAL_SCAFFOLD]
    for name in _MINIMAL_SCAFFOLD:
        assert (target / name).is_file(), name
    assert (target / "artifacts").is_dir()
    assert "artifacts/" in (target / ".gitignore").read_text(encoding="utf-8")


def test_init_refuses_a_non_empty_directory_and_writes_it_only_with_force(
    tmp_path: Path, capsys: _Capture
) -> None:
    """The refusal exits ``2`` and changes nothing; ``--force`` rewrites and keeps the file."""
    target = tmp_path / "demo"
    assert _run_cli(capsys, "init", str(target)).code == SUCCESS
    before = _snapshot(target)
    notes = target / "notes.txt"
    notes.write_text("keep me\n", encoding="utf-8")

    refused = _run_cli(capsys, "init", str(target))
    forced = _run_cli(capsys, "init", str(target), "--force")

    assert refused.code == USAGE_OR_CONFIG
    assert refused.stdout == ""
    assert "not empty" in refused.stderr
    assert set(_snapshot(target)) == {*before, "notes.txt"}

    assert forced.code == SUCCESS, forced.stderr
    assert notes.read_text(encoding="utf-8") == "keep me\n"
    for name in _MINIMAL_SCAFFOLD:
        assert (target / name).is_file(), name


def test_init_api_template_adds_the_cassette_and_a_file_path_is_refused(
    tmp_path: Path, capsys: _Capture
) -> None:
    """``--template api`` adds the API test and its cassette; a file path is a usage error."""
    api_target = tmp_path / "demo"
    file_target = tmp_path / "notes.txt"
    file_target.write_text("x\n", encoding="utf-8")

    api = _run_cli(capsys, "init", str(api_target), "--template", "api")
    refused = _run_cli(capsys, "init", str(file_target))

    assert api.code == SUCCESS, api.stderr
    assert (api_target / "tests/test_api.py").is_file()
    assert (api_target / "cassettes").is_dir()
    assert list((api_target / "cassettes").rglob("*.json"))

    assert refused.code == USAGE_OR_CONFIG
    assert refused.stderr.strip() != ""
    assert file_target.read_text(encoding="utf-8") == "x\n"


# --- No secret reaches a rendered surface (AAP 0.3.2, AAP 0.7.4) ---------


def test_no_rendered_surface_carries_a_configuration_secret(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    """The table masks the value, and no rendering of it reaches either stream.

    The value arrives the two ways a configuration can hold a credential: a header named by
    ``secrets.redact_keys`` and the variable an auth strategy names, which the model holds only as a
    name (I9).
    """
    _write_config(project, _SECRET_TOML)
    monkeypatch.setenv(_TOKEN_VARIABLE, _SECRET)

    table = _run_cli(capsys, "config", "show")
    assert table.code == SUCCESS, table.stderr
    assert _REDACTION_MARKER in table.stdout

    for run in (
        table,
        _run_cli(capsys, "config", "show", "--format", "json"),
        _run_cli(capsys, "config", "show", "--format", "toml"),
        _run_cli(capsys, "config", "validate"),
        _run_cli(capsys, "doctor", "--json"),
    ):
        assert run.code == SUCCESS, run.stderr
        assert _SECRET not in run.stdout
        assert _SECRET not in run.stderr

    _write_config(project, _SECRET_TOML.replace("timeout = 2.0", "timeout = -1"))

    faulted = _run_cli(capsys, "config", "validate")

    assert faulted.code == USAGE_OR_CONFIG
    assert _TIMEOUT_KEY in faulted.stderr
    assert _TIMEOUT_RANGE in faulted.stderr
    assert _SECRET not in faulted.stdout + faulted.stderr


@pytest.mark.parametrize(
    "code", [SUCCESS, FAILED, PYTEST_INTERNAL_ERROR, PYTEST_USAGE_ERROR, PYTEST_NO_TESTS]
)
def test_run_returns_pytests_own_code(project: Path, monkeypatch: _Monkey, code: int) -> None:
    """Once pytest is running, its status is the status Foundry publishes."""
    monkeypatch.setattr("pytest.main", lambda argv: code)

    result = CliRunner().invoke(run_command, ["-q"], standalone_mode=False)

    assert result.return_value == code


def test_run_hands_pytest_the_builder_argv_and_never_starts_it_on_a_fault(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    """The override becomes canonical; an unresolvable one stops the run before pytest starts."""
    calls: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        """Record the argument list pytest would have been given."""
        calls.append(list(argv))
        return SUCCESS

    monkeypatch.setattr("pytest.main", fake_main)

    blocked = _run_cli(capsys, "run", _TIMEOUT_OPTION, "-1")
    result = CliRunner().invoke(
        run_command, [_TIMEOUT_OPTION, "1.0", "tests/unit", "-q"], standalone_mode=False
    )

    assert blocked.code == USAGE_OR_CONFIG
    assert blocked.stdout == ""
    assert _TIMEOUT_KEY in blocked.stderr
    assert calls == [[_TIMEOUT_OPTION, "1.0", "tests/unit", "-q"]]
    assert result.return_value == SUCCESS


def test_an_interrupt_keeps_pytests_interrupt_code(
    project: Path, monkeypatch: _Monkey, capsys: _Capture
) -> None:
    """``^C`` publishes pytest's interrupt code, never a usage error (AAP 0.4.7)."""

    def interrupted(argv: list[str]) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr("pytest.main", interrupted)

    run = _run_cli(capsys, "run", "-q")

    assert run.code == INTERRUPT
    assert "aborted" in run.stderr


def test_render_comparison_prints_the_groups_in_order_and_marks_an_empty_one(
    capsys: _Capture,
) -> None:
    """An empty group prints ``(none)``, so an empty set is never mistaken for a missing one."""
    diff = ComparisonDiff(
        newly_failing=("tests/a.py::test_one", "tests/b.py::test_two"),
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


def test_the_argv_orders_the_registry_then_the_specials_then_the_pytest_arguments() -> None:
    """The order is fixed, so two runs and two workers build the same list."""
    supplied = {
        _TIMEOUT_OPTION: "1.5",
        "--foundry-record": True,
        "--foundry-profile": "ci",
        "--foundry-json-report": "results.json",
    }

    assert build_pytest_argv(supplied, ["tests/unit", "-q"]) == [
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


def test_a_switch_read_as_off_is_not_emitted_and_an_unknown_key_is_refused() -> None:
    """A switch read as off configures nothing; an override naming no key is reported."""
    for off in (False, "false", "off", ""):
        assert build_pytest_argv({"--foundry-record": off}, []) == []

    with pytest.raises(ConfigError) as raised:
        build_pytest_argv({"http.timeot": "1.0"}, [])

    assert raised.value.key == "http.timeot"
    assert "http.timeot" in str(raised.value)


def test_every_cli_module_imports_on_its_own() -> None:
    """Each module imports without the group builder, so no import cycle hides in the package."""
    names = tuple(
        sorted(
            f"{foundry.cli.__name__}.{module.name}"
            for module in pkgutil.iter_modules(foundry.cli.__path__)
        )
    )

    assert "foundry.cli.main" in names
    for name in names:
        assert importlib.import_module(name) is not None
