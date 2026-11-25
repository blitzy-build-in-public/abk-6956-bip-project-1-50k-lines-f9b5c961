"""The help surface and the documented exit codes of the ``foundry`` command line.

This is the CLI half of AAP 0.14.1's criterion 9 -- ``foundry --help`` lists ``run``, ``init``,
``config``, ``report`` and ``doctor``, and every subcommand has a test asserting the exit codes
AAP 0.4.4 documents for it -- with the CLI halves of criterion 4 (a configuration fault is ``2``)
and criterion 10 (the installed console script behaves as the plan says). The contract is AAP 0.4.4
in full: ``0`` success, ``1`` failed tests or a newly failing comparison, ``2`` a usage or
configuration error Foundry itself raised, and pytest's own ``3``, ``4`` and ``5`` untouched.

**Two entry points, and each case uses the one that can show it.** ``--help``, ``--version`` and
``init`` run through :class:`click.testing.CliRunner`, where click supplies both text and status.
``config``, ``report`` and ``doctor`` run through :func:`foundry.cli.main.main`, the declared
``[project.scripts]`` target, because click discards a callback's return value in standalone mode --
a ``doctor`` whose callback returned ``2`` is reported by ``CliRunner`` as ``0`` -- so only the
entry target publishes the status a shell sees. ``foundry run`` runs through the installed console
script of the shared ``run_foundry`` fixture, because it calls :func:`pytest.main` and a nested
in-process session is not a supported execution; its pass-through cases hand the *same* argument
tuple to the bare pytest surface of the shared ``run_pytest`` fixture and assert equal codes.

**The exit-3 trigger.** pytest's internal-error code is provoked by a generated ``conftest.py``
whose ``pytest_configure`` raises, which pytest 8.4.2 reports as ``INTERNALERROR>`` and exits ``3``;
a fault raised during collection was rejected because that is a plain collection error (exit ``2``).
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from click.testing import CliRunner, Result

from foundry.cli.exit_codes import (
    FAILED,
    PYTEST_INTERNAL_ERROR,
    PYTEST_NO_TESTS,
    PYTEST_PASSTHROUGH,
    PYTEST_USAGE_ERROR,
    SUCCESS,
    USAGE_OR_CONFIG,
)
from foundry.cli.main import build_group, main

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Mapping

REACHES: tuple[str, ...] = (
    "command:run",
    "command:init",
    "command:config",
    "command:report",
    "command:doctor",
)
"""Every subcommand of AAP 0.4.4, each with evidence here: ``run`` through the installed console
script, ``init`` by :func:`test_init_exit_codes`, ``config`` by its three cases, ``report`` by the
comparison case and ``doctor`` by the probe case. ``tests/plugin/test_reachability.py`` reads this.
"""

_SUBCOMMANDS: Final[tuple[str, ...]] = ("run", "init", "config", "report", "doctor")
"""AAP 0.4.4's five subcommands, in the order the group's help lists them."""

_HELP_OPTION: Final[str] = "--help"
"""The one option every command carries and none has to document itself."""

_FOUNDRY_PREFIX: Final[str] = "--foundry-"
"""Prefix of the generic override family AAP 0.4.4 registers for every scalar key."""

_VERSION_BANNER: Final[str] = "foundry, version 0.1.0"
"""The exact banner of AAP 0.1.2, preserved character for character."""

_VALID_MESSAGE: Final[str] = "configuration valid (profile=default)"
"""The success line of AAP 0.1.2, with the default profile interpolated."""

_CI_PROFILE: Final[str] = "ci"
"""One of the four profiles of AAP 0.4.5, used to prove the name is interpolated."""

_TIMEOUT_KEY: Final[str] = "http.timeout"
"""The key criterion 4 drives out of range and every message must name."""

_TIMEOUT_OPTION: Final[str] = f"--foundry-{_TIMEOUT_KEY}"
"""The generic override AAP 0.4.4 registers for that key."""

_TIMEOUT_RANGE: Final[str] = "0 < t <= 300"
"""``http.timeout``'s accepted range, quoted exactly as AAP 0.4.5 spells it."""

_FILE_TIMEOUT: Final[float] = 2.0
"""The value the valid configuration sets, which every ``config`` rendering must report."""

_WIDE_COLUMNS: Final[str] = "400"
"""``COLUMNS`` for every table rendering, so ``rich`` never wraps a cell an assertion reads."""

_REDACTED_MARKER: Final[str] = "***"
"""The marker the redaction engine writes in place of a masked value."""

_RESULTS_FILE: Final[str] = "results.json"
"""The result document of AAP 0.4.6, written to the invocation directory."""

_REPORT_NAMES: Final[tuple[str, ...]] = ("results.json", "junit.xml", "report.html")
"""The three fixed report names AAP 0.4.6 writes for a run that got as far as reporting."""

_OPTION_TOKEN: Final[re.Pattern[str]] = re.compile(r"(?<!\w)--?[a-z][a-z0-9-]*")
"""An option as help text spells it, which is how an undocumented one is detected."""

_COMMAND_ROW: Final[re.Pattern[str]] = re.compile(
    r"^ {2}(?P<name>[a-z][a-z0-9-]*) {2,}(?P<description>\S.*?)\s*$"
)
"""One ``name  description`` row of the ``Commands:`` section, which is indented exactly two."""

_VALID_TOML: Final[str] = f"[http]\ntimeout = {_FILE_TIMEOUT}\n"
"""A valid configuration file, for the positive controls."""

_INVALID_TOML: Final[str] = "[http]\ntimeout = -1\n"
"""Criterion 4's fault: a value outside the accepted range of AAP 0.4.5."""

_SECRET_SENTINEL: Final[str] = "s3ntinel-" + "header-value"
"""A masked header's value, assembled so no scanner reads the literal as a credential."""

_SECRET_TOML: Final[str] = (
    f"[http]\ntimeout = {_FILE_TIMEOUT}\n\n"
    f'[http.headers]\nAuthorization = "Bearer {_SECRET_SENTINEL}"\n'
)
"""A configuration whose header name is one ``secrets.redact_keys`` masks (AAP 0.4.5)."""

_PASSING_TEST: Final[str] = '''"""A test that passes, so a run over this project succeeds."""


def test_ok() -> None:
    """Pass, so the exit code can only come from the surface under test."""
    assert True
'''

_FAILING_TEST: Final[str] = '''"""A test that fails, so a run over this project exits 1."""


def test_bad() -> None:
    """Fail, which AAP 0.4.4 publishes as exit code 1."""
    assert False
'''

_RAISING_CONFTEST: Final[str] = '''"""A conftest that makes pytest report an internal error."""


def pytest_configure(config: object) -> None:
    """Raise while configuring, which pytest reports as an internal error and exits 3."""
    raise RuntimeError("foundry cli surface: deliberate internal error")
'''


@dataclass(frozen=True, slots=True)
class _HelpCase:
    """One ``--help`` page, the options it may list and the ones it must.

    ``documented`` bounds what the page may print, so an unpublished option fails the case, and
    ``must_show`` pins what a reader has to find. Any ``--foundry-*`` spelling counts as documented,
    because AAP 0.4.4 registers the generic ``--foundry-<dotted.key>`` override for every scalar key
    and enumerating that family here would restate the key registry.
    """

    commands: tuple[str, ...]
    documented: frozenset[str]
    must_show: frozenset[str]

    @property
    def identifier(self) -> str:
        """Return the command path, as the parametrised case id spells it."""
        return " ".join(self.commands)


def _help_case(
    commands: tuple[str, ...], *, documented: str = "", must_show: str = ""
) -> _HelpCase:
    """Return the help case for ``commands``, spelling each option set as space-separated text.

    A table of these stays readable, one entry per line, and no reflowed literal can pick up a stray
    comma that silently merges two options into one.
    """
    return _HelpCase(
        commands=commands,
        documented=frozenset(documented.split()),
        must_show=frozenset(must_show.split()),
    )


_HELP_CASES: Final[tuple[_HelpCase, ...]] = (
    _help_case(("run",), must_show="--foundry-profile --foundry-config --foundry-timeout"),
    _help_case(("init",), documented="--template --force", must_show="--template --force"),
    _help_case(
        ("config", "show"), documented="--resolved --format", must_show="--resolved --format"
    ),
    _help_case(("config", "validate"), must_show="--foundry-profile --foundry-config"),
    _help_case(("report", "compare"), documented="--json", must_show="--json"),
    _help_case(("doctor",), documented="--json", must_show="--json --foundry-config"),
)
"""Every documented help page of AAP 0.4.4, with the options it may and must list.

``config validate`` documents no option of its own, and neither ``init`` nor ``report compare``
accepts a ``--foundry-<dotted.key>`` override at all, which is why those rows leave the family out.
"""


@dataclass(frozen=True, slots=True)
class _PytestCodeCase:
    """One pytest exit code that ``foundry run`` must reproduce unchanged (AAP 0.4.4)."""

    identifier: str
    code: int
    argv: tuple[str, ...]
    files: Mapping[str, str]
    conftest: str | None = None


_PYTEST_CODE_CASES: Final[tuple[_PytestCodeCase, ...]] = (
    _PytestCodeCase(identifier="no-tests-collected", code=PYTEST_NO_TESTS, argv=("-q",), files={}),
    _PytestCodeCase(
        identifier="usage-error",
        code=PYTEST_USAGE_ERROR,
        argv=("-q", "--no-such-pytest-option"),
        files={"test_ok.py": _PASSING_TEST},
    ),
    _PytestCodeCase(
        identifier="internal-error",
        code=PYTEST_INTERNAL_ERROR,
        argv=("-q",),
        files={"test_ok.py": _PASSING_TEST},
        conftest=_RAISING_CONFTEST,
    ),
)
"""The three codes of pytest's that Foundry must hand back untouched, one trigger each."""


@dataclass(frozen=True, slots=True)
class _CliRun:
    """One in-process run of the command line: the status it published and what it printed."""

    code: int
    stdout: str
    stderr: str


def _invoke(*args: str) -> Result:
    """Invoke the click application through a fresh ``CliRunner``, as click itself does."""
    return CliRunner().invoke(build_group(), list(args))


def _run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> _CliRun:
    """Run the ``[project.scripts]`` target in process and capture what it published.

    :func:`foundry.cli.main.main` is the one place the documented mapping is applied -- a
    :class:`foundry.core.exceptions.FoundryError` becomes ``2``, a command's own integer is handed
    back through ``passthrough`` -- so the exit codes of ``config``, ``report`` and ``doctor`` are
    observable here or in a child process, and here is faster and free of another process's
    environment.
    """
    code = main(list(args))
    captured = capsys.readouterr()
    return _CliRun(code=code, stdout=captured.out, stderr=captured.err)


def _command_rows(help_text: str) -> tuple[tuple[str, str], ...]:
    """Return the ``(name, description)`` rows of the help's ``Commands:`` section.

    Fails rather than returning nothing when the section is absent or a row is malformed, because a
    silently empty parse would let the surface assertion pass without observing anything.
    """
    lines = help_text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "Commands:"), None)
    if start is None:
        pytest.fail(f"the help text has no 'Commands:' section:\n{help_text}")
    rows: list[tuple[str, str]] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            break
        match = _COMMAND_ROW.match(line)
        if match is None:
            pytest.fail(f"cannot read the command row {line!r}:\n{help_text}")
        rows.append((match["name"], match["description"]))
    return tuple(rows)


def _option_tokens(help_text: str) -> frozenset[str]:
    """Return every option spelling the help text mentions, as the token pattern reads it."""
    return frozenset(_OPTION_TOKEN.findall(help_text))


def _snapshot(directory: Path) -> tuple[tuple[str, bytes], ...]:
    """Return every file under ``directory`` with its bytes, so two tree states compare exactly."""
    return tuple(
        (path.relative_to(directory).as_posix(), path.read_bytes())
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )


def _copy_results_fixtures(directory: Path, repo_root: Path) -> tuple[Path, Path, Path]:
    """Copy the three result documents of ``tests/fixtures`` into ``directory`` and return them.

    The repository's own copies are only read, so no case can mutate a fixture a sibling worker is
    reading, and every path a command is handed lives under ``tmp_path``.
    """
    source = repo_root / "tests" / "fixtures"
    base = "results_base.json"
    new = "results_new.json"
    unreadable = "results_unreadable.txt"
    for name in (base, new, unreadable):
        shutil.copy(source / name, directory / name)
    return directory / base, directory / new, directory / unreadable


def test_help_lists_the_five_subcommands() -> None:
    """``foundry --help`` exits 0 and lists the five subcommands with a description each."""
    result = _invoke("--help")

    assert result.exit_code == SUCCESS, result.stderr
    rows = _command_rows(result.stdout)
    listed = tuple(name for name, _ in rows)
    assert listed == _SUBCOMMANDS, (
        f"foundry --help lists {listed} instead of the five subcommands {_SUBCOMMANDS} in their "
        "published order (AAP 0.4.4)"
    )
    for name, description in rows:
        assert description.strip(), f"foundry --help lists {name} with no description"


def test_version_banner_is_exact() -> None:
    """``foundry --version`` prints the pinned banner and exits 0 (AAP 0.1.2, 0.4.4)."""
    result = _invoke("--version")

    assert result.exit_code == SUCCESS, result.stderr
    assert result.stdout.strip() == _VERSION_BANNER


@pytest.mark.parametrize("case", _HELP_CASES, ids=[case.identifier for case in _HELP_CASES])
def test_each_subcommand_help_shows_only_documented_options(case: _HelpCase) -> None:
    """Every subcommand's help exits 0 and lists documented options only, and all of them.

    Both directions matter: an undocumented option is a surface nobody published, and a missing
    documented one is a capability a reader cannot discover.
    """
    result = _invoke(*case.commands, "--help")

    assert result.exit_code == SUCCESS, result.stderr
    tokens = _option_tokens(result.stdout)
    undocumented = sorted(
        token
        for token in tokens
        if token not in case.documented
        and token != _HELP_OPTION
        and not token.startswith(_FOUNDRY_PREFIX)
    )
    assert not undocumented, (
        f"foundry {case.identifier} --help lists undocumented option(s) {undocumented} (AAP 0.4.4)"
    )
    missing = sorted(case.must_show - tokens)
    assert not missing, (
        f"foundry {case.identifier} --help omits documented option(s) {missing} (AAP 0.4.4)"
    )


def test_config_validate_success_message(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    write_project: Callable[..., Path],
    clean_foundry_env: None,
) -> None:
    """``config validate`` reports the pinned success line for the active profile.

    AAP 0.1.2 fixes the wording as ``configuration valid (profile=<name>)``, so both halves are
    observed: the default profile prints ``profile=default`` and ``--foundry-profile ci`` prints the
    name it was given, which is only true if the name is interpolated rather than spelled.
    """
    project = write_project(tmp_path / "project", foundry_toml=_VALID_TOML)
    config = str(project / "foundry.toml")

    default_run = _run_cli(capsys, "config", "validate", "--foundry-config", config)

    assert default_run.code == SUCCESS, default_run.stderr
    assert _VALID_MESSAGE in default_run.stdout
    assert default_run.stderr == ""

    profiled = _run_cli(
        capsys, "config", "validate", "--foundry-config", config, "--foundry-profile", _CI_PROFILE
    )

    assert profiled.code == SUCCESS, profiled.stderr
    assert f"configuration valid (profile={_CI_PROFILE})" in profiled.stdout


def test_config_validate_failure_exits_2(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    write_project: Callable[..., Path],
    clean_foundry_env: None,
) -> None:
    """A value outside its range exits 2, names the key, and prints nothing on stdout.

    AAP 0.4.4 fixes that shape for ``config validate``: the first error and nothing else, so a
    consumer parsing stdout never has to filter a partial result out of a failure.
    """
    project = write_project(tmp_path / "project", foundry_toml=_INVALID_TOML)

    result = _run_cli(
        capsys, "config", "validate", "--foundry-config", str(project / "foundry.toml")
    )

    assert result.code == USAGE_OR_CONFIG
    assert _TIMEOUT_KEY in result.stderr
    assert _TIMEOUT_RANGE in result.stderr
    assert result.stdout.strip() == ""


def test_config_show_exit_codes_and_documents(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_project: Callable[..., Path],
    clean_foundry_env: None,
) -> None:
    """``config show`` exits 0 for a valid project, emits one document per format, else 2."""
    monkeypatch.setenv("COLUMNS", _WIDE_COLUMNS)
    valid = write_project(tmp_path / "valid", foundry_toml=_VALID_TOML)
    broken = write_project(tmp_path / "broken", foundry_toml=_INVALID_TOML)
    config = str(valid / "foundry.toml")

    table = _run_cli(capsys, "config", "show", "--foundry-config", config)

    assert table.code == SUCCESS, table.stderr
    assert _TIMEOUT_KEY in table.stdout

    as_json = _run_cli(capsys, "config", "show", "--format", "json", "--foundry-config", config)

    assert as_json.code == SUCCESS, as_json.stderr
    document = json.loads(as_json.stdout)
    assert {"resolved", "sources"} <= set(document)
    assert document["resolved"]["http"]["timeout"] == _FILE_TIMEOUT
    assert document["sources"][_TIMEOUT_KEY]["layer"] == "file"

    as_toml = _run_cli(capsys, "config", "show", "--format", "toml", "--foundry-config", config)

    assert as_toml.code == SUCCESS, as_toml.stderr
    parsed = tomllib.loads(as_toml.stdout)
    assert parsed["http"]["timeout"] == _FILE_TIMEOUT
    assert parsed["sources"][_TIMEOUT_KEY]["layer"] == "file"

    refused = _run_cli(capsys, "config", "show", "--foundry-config", str(broken / "foundry.toml"))

    assert refused.code == USAGE_OR_CONFIG
    assert refused.stdout.strip() == ""


def test_config_show_keeps_a_redacted_header_value_off_the_terminal(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    write_project: Callable[..., Path],
    clean_foundry_env: None,
) -> None:
    """A masked header's value never reaches the terminal, in any of the three renderings.

    ``config show`` is the one command that prints resolved configuration, so it is where the
    AAP 0.3.2 rule is observed on the console: the value becomes the marker and the sentinel appears
    nowhere in the output (implicit requirement I9, AAP 0.14.3).
    """
    monkeypatch.setenv("COLUMNS", _WIDE_COLUMNS)
    project = write_project(tmp_path / "project", foundry_toml=_SECRET_TOML)
    config = str(project / "foundry.toml")
    renderings: tuple[tuple[str, ...], ...] = (
        ("config", "show"),
        ("config", "show", "--format", "json"),
        ("config", "show", "--format", "toml"),
    )

    for args in renderings:
        result = _run_cli(capsys, *args, "--foundry-config", config)

        assert result.code == SUCCESS, result.stderr
        assert _SECRET_SENTINEL not in result.stdout, (
            f"foundry {' '.join(args)} printed a value secrets.redact_keys masks (AAP 0.3.2)"
        )
        assert _SECRET_SENTINEL not in result.stderr
        assert _REDACTED_MARKER in result.stdout


def test_report_compare_exit_codes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, repo_root: Path
) -> None:
    """``report compare`` exits 1 on a regression, 0 on no change and 2 on an unreadable document.

    Only the codes are asserted: the three labelled groups and the ``--json`` document's shape are
    ``tests/plugin/test_report_compare.py``'s contract, and restating them would test one rendering
    twice.
    """
    base, new, unreadable = _copy_results_fixtures(tmp_path, repo_root)

    regressed = _run_cli(capsys, "report", "compare", str(base), str(new))

    assert regressed.code == FAILED

    unchanged = _run_cli(capsys, "report", "compare", str(base), str(base))

    assert unchanged.code == SUCCESS

    as_json = _run_cli(capsys, "report", "compare", "--json", str(base), str(new))

    assert as_json.code == FAILED
    json.loads(as_json.stdout)

    refused = _run_cli(capsys, "report", "compare", str(base), str(unreadable))

    assert refused.code == USAGE_OR_CONFIG
    assert refused.stdout.strip() == ""


def test_doctor_exit_codes_and_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, clean_foundry_env: None
) -> None:
    """``doctor --json`` emits one document, exits 0 when every required probe passes, else 2.

    The directory probes are pointed at ``tmp_path`` so the command inspects the case's own tree
    rather than the checkout's. The failure case occupies ``artifacts.dir`` with a regular file: the
    required probe then reports ``fail``, ``ok`` is false and the command exits 2 (AAP 0.4.4), a
    verdict no ``warn`` can produce.
    """
    artifacts = str(tmp_path / "artifacts")
    cassettes = str(tmp_path / "cassettes")

    ready = _run_cli(
        capsys,
        "doctor",
        "--json",
        "--foundry-artifacts.dir",
        artifacts,
        "--foundry-cassette.dir",
        cassettes,
    )

    assert ready.code == SUCCESS, ready.stderr
    document = json.loads(ready.stdout)
    assert {"interpreter", "checks", "ok"} <= set(document)
    assert document["ok"] is True
    assert isinstance(document["checks"], list)

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory\n", encoding="utf-8")

    failing = _run_cli(
        capsys,
        "doctor",
        "--json",
        "--foundry-artifacts.dir",
        str(blocked),
        "--foundry-cassette.dir",
        cassettes,
    )

    assert failing.code == USAGE_OR_CONFIG
    failed_document = json.loads(failing.stdout)
    assert failed_document["ok"] is False
    assert any(
        check["name"] == "artifacts" and check["status"] == "fail"
        for check in failed_document["checks"]
    )

    unconfigured = _run_cli(
        capsys, "doctor", "--json", "--foundry-config", str(tmp_path / "nope.toml")
    )

    assert unconfigured.code == USAGE_OR_CONFIG
    assert unconfigured.stdout.strip() == ""


def test_init_exit_codes(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """``init`` exits 0 when it writes the scaffold and 2 when it refuses, having written nothing.

    AAP 0.4.4 gives the command those two codes and no other, and the refusal is asserted by content
    as well as by code: the tree is byte-identical afterwards, so a refusal cannot have half-written
    a scaffold. Running the scaffold is ``tests/e2e/test_init_project.py``'s evidence.
    """
    target = tmp_path / "demo"

    created = _invoke("init", str(target))

    assert created.exit_code == SUCCESS, created.stderr
    printed = [line.strip() for line in created.stdout.splitlines() if line.strip()]
    assert printed, "foundry init printed no created paths"
    for line in printed:
        assert Path(line).is_file(), f"foundry init reported {line}, which is not a file"

    before = _snapshot(target)

    repeated = _invoke("init", str(target))

    assert repeated.exit_code == USAGE_OR_CONFIG
    assert _snapshot(target) == before, "a refused init changed the tree"

    forced = _invoke("init", str(target), "--force")

    assert forced.exit_code == SUCCESS, forced.stderr
    assert _snapshot(target) == before

    occupied = tmp_path / "occupied"
    occupied.write_text("kept\n", encoding="utf-8")

    refused = _invoke("init", str(occupied))

    assert refused.exit_code == USAGE_OR_CONFIG
    assert occupied.read_text(encoding="utf-8") == "kept\n"


def test_run_passing_exits_0_and_failing_exits_1(
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_foundry: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """The installed script publishes 0 for a passing suite and 1 for a failing one.

    The passing run is also the evidence that a real run leaves ``results.json`` in the invocation
    directory (AAP 0.4.6), which is what makes ``foundry run`` a reporter and not only a wrapper.
    """
    passing = write_project(tmp_path / "passing", files={"test_ok.py": _PASSING_TEST})

    passed = run_foundry("run", "-q", cwd=passing)

    assert passed.returncode == SUCCESS, passed.stdout + passed.stderr
    results = json.loads((passing / _RESULTS_FILE).read_text(encoding="utf-8"))
    outcomes = [item["outcome"] for item in results if item.get("record_type") == "result"]
    assert outcomes == ["passed"]

    failing = write_project(tmp_path / "failing", files={"test_bad.py": _FAILING_TEST})

    failed = run_foundry("run", "-q", cwd=failing)

    assert failed.returncode == FAILED, failed.stdout + failed.stderr


@pytest.mark.parametrize(
    "case", _PYTEST_CODE_CASES, ids=[case.identifier for case in _PYTEST_CODE_CASES]
)
def test_run_preserves_pytest_exit_codes(
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_foundry: Callable[..., subprocess.CompletedProcess[str]],
    run_pytest: Callable[..., subprocess.CompletedProcess[str]],
    case: _PytestCodeCase,
) -> None:
    """pytest's own 3, 4 and 5 reach the shell unchanged through ``foundry run``.

    Both surfaces get the same argument tuple, so the only difference between the runs is the
    wrapper. Equality is the assertion AAP 0.4.4 makes -- "Foundry never rewrites them" -- and
    membership of pytest's own set is asserted beside it, so the case cannot pass by both surfaces
    agreeing on a code neither should have produced.
    """
    project = write_project(tmp_path / case.identifier, files=case.files, conftest=case.conftest)

    bare = run_pytest(*case.argv, cwd=project)
    wrapped = run_foundry("run", *case.argv, cwd=project)

    assert bare.returncode == case.code, bare.stdout + bare.stderr
    assert wrapped.returncode == bare.returncode, (
        f"foundry run published {wrapped.returncode} for {case.identifier} while pytest published "
        f"{bare.returncode}; Foundry never rewrites a code pytest produced (AAP 0.4.4)\n"
        f"{wrapped.stdout}{wrapped.stderr}"
    )
    assert wrapped.returncode in PYTEST_PASSTHROUGH
    if case.code == PYTEST_NO_TESTS:
        assert "no tests ran" in wrapped.stdout


def test_run_configuration_error_before_start_exits_2(
    tmp_path: Path,
    write_project: Callable[..., Path],
    run_foundry: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """A configuration fault ends ``foundry run`` with 2 and writes no report at all.

    This is the only case in which ``run`` produces ``2`` from Foundry itself (AAP 0.4.4): the fault
    is found while the command resolves the configuration, before pytest starts, so the absence of
    every report file is what proves no session ever began.
    """
    project = write_project(tmp_path / "project", files={"test_ok.py": _PASSING_TEST})

    result = run_foundry("run", f"{_TIMEOUT_OPTION}=-1", cwd=project)

    assert result.returncode == USAGE_OR_CONFIG, result.stdout + result.stderr
    assert _TIMEOUT_KEY in result.stderr
    written = sorted(path.name for path in project.iterdir() if path.name in _REPORT_NAMES)
    assert not written, f"a run that could not be configured wrote {written}"
    assert not (project / "artifacts").exists(), "a run that could not be configured captured one"
