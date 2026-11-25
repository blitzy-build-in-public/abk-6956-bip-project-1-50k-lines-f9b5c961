"""The help surface and the documented exit codes of the ``foundry`` command line (AAP 0.4.4)."""

from __future__ import annotations

import json
import re
import shutil
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, NamedTuple

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


class _CliRun(NamedTuple):
    code: int
    stdout: str
    stderr: str


if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    _Run = Callable[..., subprocess.CompletedProcess[str]]
    _Project = Callable[..., Path]
    _Cli = Callable[..., _CliRun]
    _Config = Callable[..., str]

REACHES: tuple[str, ...] = (
    "command:run",
    "command:init",
    "command:config",
    "command:report",
    "command:doctor",
)
_SUBCOMMANDS: Final[tuple[str, ...]] = ("run", "init", "config", "report", "doctor")
_TIMEOUT_KEY: Final[str] = "http.timeout"
_FILE_TIMEOUT: Final[float] = 2.0
_REPORT_NAMES: Final[tuple[str, ...]] = ("results.json", "junit.xml", "report.html")
_OPTION_TOKEN: Final[re.Pattern[str]] = re.compile(r"(?<!\w)--?[a-z][a-z0-9-]*")
_COMMAND_ROW: Final[re.Pattern[str]] = re.compile(
    r"^ {2}(?P<name>[a-z][a-z0-9-]*) {2,}(?P<description>\S.*?)\s*$", re.MULTILINE
)
_VALID_TOML: Final[str] = f"[http]\ntimeout = {_FILE_TIMEOUT}\n"
_INVALID_TOML: Final[str] = "[http]\ntimeout = -1\n"
_SENTINEL: Final[str] = "s3ntinel-" + "header-value"
_SECRET_TOML: Final[str] = (
    f'[http]\ntimeout = {_FILE_TIMEOUT}\n\n[http.headers]\nAuthorization = "Bearer {_SENTINEL}"\n'
)
_PASSING_TEST: Final[str] = "def test_ok() -> None:\n    assert True\n"
_FAILING_TEST: Final[str] = "def test_bad() -> None:\n    assert False\n"
_RAISING_CONFTEST: Final[str] = (
    'def pytest_configure(config: object) -> None:\n    raise RuntimeError("deliberate")\n'
)
_HELP_CASES: Final[tuple[tuple[tuple[str, ...], str, str], ...]] = (
    (("run",), "", "--foundry-profile --foundry-config --foundry-timeout"),
    (("init",), "--template --force", "--template --force"),
    (("config", "show"), "--resolved --format", "--resolved --format"),
    (("config", "validate"), "", "--foundry-profile --foundry-config"),
    (("report", "compare"), "--json", "--json"),
    (("doctor",), "--json", "--json --foundry-config"),
)
# (identifier, pytest code, argv, files, conftest) for each code foundry run must hand back.
_CodeCase = tuple[str, int, tuple[str, ...], dict[str, str], str | None]
_PYTEST_CODE_CASES: Final[tuple[_CodeCase, ...]] = (
    ("no-tests-collected", PYTEST_NO_TESTS, ("-q",), {}, None),
    ("usage-error", PYTEST_USAGE_ERROR, ("-q", "--nope"), {"t.py": _PASSING_TEST}, None),
    ("internal-error", PYTEST_INTERNAL_ERROR, ("-q",), {"t.py": _PASSING_TEST}, _RAISING_CONFTEST),
)


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(build_group(), list(args))


def _run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> _CliRun:
    code = main(list(args))
    captured = capsys.readouterr()
    return _CliRun(code=code, stdout=captured.out, stderr=captured.err)


def _snapshot(directory: Path) -> tuple[tuple[str, bytes], ...]:
    files = (path for path in sorted(directory.rglob("*")) if path.is_file())
    return tuple((path.relative_to(directory).as_posix(), path.read_bytes()) for path in files)


@pytest.fixture(name="run")
def _run_fixture(capsys: pytest.CaptureFixture[str], clean_foundry_env: None) -> _Cli:
    return partial(_run_cli, capsys)


@pytest.fixture(name="config")
def _config_fixture(tmp_path: Path, write_project: _Project, clean_foundry_env: None) -> _Config:
    return lambda name="project", toml=_VALID_TOML: str(
        write_project(tmp_path / name, foundry_toml=toml) / "foundry.toml"
    )


def test_group_help_lists_the_five_subcommands_and_the_version_banner() -> None:
    help_result = _invoke("--help")
    assert help_result.exit_code == SUCCESS, help_result.stderr
    listed = tuple(match["name"] for match in _COMMAND_ROW.finditer(help_result.stdout))
    assert listed, f"foundry --help lists no command row:\n{help_result.stdout}"
    assert listed == _SUBCOMMANDS, listed
    version = _invoke("--version")
    assert (version.exit_code, version.stdout.strip()) == (SUCCESS, "foundry, version 0.1.0")


@pytest.mark.parametrize(
    ("commands", "documented", "must_show"), _HELP_CASES, ids=[" ".join(c[0]) for c in _HELP_CASES]
)
def test_each_subcommand_help_shows_only_documented_options(
    commands: tuple[str, ...], documented: str, must_show: str
) -> None:
    result = _invoke(*commands, "--help")
    assert result.exit_code == SUCCESS, result.stderr
    tokens = frozenset(_OPTION_TOKEN.findall(result.stdout))
    allowed = set(documented.split()) | {"--help"}
    undocumented = sorted(t for t in tokens if t not in allowed and not t.startswith("--foundry-"))
    assert not undocumented, (commands, undocumented)
    assert not set(must_show.split()) - tokens, (commands, must_show)


def test_config_validate_and_show_exit_codes_formats_and_redaction(
    run: _Cli, config: _Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "400")
    valid = config()
    for extra, profile in (((), "default"), (("--foundry-profile", "ci"), "ci")):
        validated = run("config", "validate", "--foundry-config", valid, *extra)
        assert (validated.code, validated.stderr) == (SUCCESS, "")
        assert validated.stdout.strip() == f"configuration valid (profile={profile})"
    rejected = run("config", "validate", "--foundry-config", config("broken", _INVALID_TOML))
    assert (rejected.code, rejected.stdout.strip()) == (USAGE_OR_CONFIG, "")
    assert _TIMEOUT_KEY in rejected.stderr and "0 < t <= 300" in rejected.stderr, rejected.stderr
    table = run("config", "show", "--foundry-config", valid)
    assert table.code == SUCCESS and _TIMEOUT_KEY in table.stdout, table.stderr
    refused = run("config", "show", "--foundry-config", config("broken", _INVALID_TOML))
    assert (refused.code, refused.stdout.strip()) == (USAGE_OR_CONFIG, "")
    shown = run("config", "show", "--foundry-config", config("secret", _SECRET_TOML))
    assert shown.code == SUCCESS and "***" in shown.stdout and _SENTINEL not in shown.stdout


def test_report_compare_exit_codes(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, repo_root: Path
) -> None:
    for name in ("results_base.json", "results_new.json", "results_unreadable.txt"):
        shutil.copy(repo_root / "tests" / "fixtures" / name, tmp_path / name)
    base, new = str(tmp_path / "results_base.json"), str(tmp_path / "results_new.json")
    assert _run_cli(capsys, "report", "compare", base, new).code == FAILED
    assert _run_cli(capsys, "report", "compare", base, base).code == SUCCESS
    as_json = _run_cli(capsys, "report", "compare", "--json", base, new)
    assert (as_json.code, bool(json.loads(as_json.stdout))) == (FAILED, True)
    refused = _run_cli(capsys, "report", "compare", base, str(tmp_path / "results_unreadable.txt"))
    assert (refused.code, refused.stdout.strip()) == (USAGE_OR_CONFIG, "")


def test_doctor_exit_codes_and_json(run: _Cli, tmp_path: Path) -> None:
    cassettes = f"--foundry-cassette.dir={tmp_path / 'cassettes'}"
    ready = run("doctor", "--json", f"--foundry-artifacts.dir={tmp_path / 'artifacts'}", cassettes)
    assert ready.code == SUCCESS, ready.stderr
    document = json.loads(ready.stdout)
    assert {"interpreter", "checks", "ok"} <= set(document)
    assert (document["ok"], isinstance(document["checks"], list)) == (True, True)
    (tmp_path / "blocked").write_text("not a directory\n", encoding="utf-8")
    failing = run("doctor", "--json", f"--foundry-artifacts.dir={tmp_path / 'blocked'}", cassettes)
    assert failing.code == USAGE_OR_CONFIG
    failed_document = json.loads(failing.stdout)
    statuses = {check["name"]: check["status"] for check in failed_document["checks"]}
    assert (failed_document["ok"], statuses["artifacts"]) == (False, "fail")
    unconfigured = run("doctor", "--json", "--foundry-config", str(tmp_path / "nope.toml"))
    assert (unconfigured.code, unconfigured.stdout.strip()) == (USAGE_OR_CONFIG, "")


def test_init_exit_codes(tmp_path: Path) -> None:
    target = tmp_path / "demo"
    created = _invoke("init", str(target))
    assert created.exit_code == SUCCESS, created.stderr
    printed = [line.strip() for line in created.stdout.splitlines() if line.strip()]
    assert printed and all(Path(line).is_file() for line in printed), printed
    before = _snapshot(target)
    assert _invoke("init", str(target)).exit_code == USAGE_OR_CONFIG
    assert _snapshot(target) == before, "a refused init changed the tree"
    forced = _invoke("init", str(target), "--force")
    assert (forced.exit_code, _snapshot(target)) == (SUCCESS, before), forced.stderr
    (occupied := tmp_path / "occupied").write_text("kept\n", encoding="utf-8")
    assert _invoke("init", str(occupied)).exit_code == USAGE_OR_CONFIG
    assert occupied.read_text(encoding="utf-8") == "kept\n"


def test_run_hands_back_pass_fail_and_configuration_exit_codes(
    tmp_path: Path, write_project: _Project, run_foundry: _Run
) -> None:
    passing = write_project(tmp_path / "passing", files={"test_ok.py": _PASSING_TEST})
    passed = run_foundry("run", "-q", cwd=passing)
    assert passed.returncode == SUCCESS, passed.stdout + passed.stderr
    results = json.loads((passing / "results.json").read_text(encoding="utf-8"))
    outcomes = [item["outcome"] for item in results if item.get("record_type") == "result"]
    assert outcomes == ["passed"]
    failing = write_project(tmp_path / "failing", files={"test_bad.py": _FAILING_TEST})
    assert run_foundry("run", "-q", cwd=failing).returncode == FAILED
    unconfigured = write_project(tmp_path / "unconfigured", files={"test_ok.py": _PASSING_TEST})
    refused = run_foundry("run", f"--foundry-{_TIMEOUT_KEY}=-1", cwd=unconfigured)
    assert refused.returncode == USAGE_OR_CONFIG and _TIMEOUT_KEY in refused.stderr, refused.stderr
    written = sorted(path.name for path in unconfigured.iterdir() if path.name in _REPORT_NAMES)
    assert not written, f"a run that could not be configured wrote {written}"
    assert not (unconfigured / "artifacts").exists()


@pytest.mark.parametrize("case", _PYTEST_CODE_CASES, ids=[case[0] for case in _PYTEST_CODE_CASES])
def test_run_preserves_pytest_exit_codes(
    tmp_path: Path,
    write_project: _Project,
    run_foundry: _Run,
    run_pytest: _Run,
    case: _CodeCase,
) -> None:
    identifier, code, argv, files, conftest = case
    project = write_project(tmp_path / identifier, files=files, conftest=conftest)
    bare, wrapped = run_pytest(*argv, cwd=project), run_foundry("run", *argv, cwd=project)
    assert bare.returncode == code, bare.stdout + bare.stderr
    assert wrapped.returncode == bare.returncode, (identifier, wrapped.stdout, wrapped.stderr)
    assert wrapped.returncode in PYTEST_PASSTHROUGH
    if code == PYTEST_NO_TESTS:
        assert "no tests ran" in wrapped.stdout
