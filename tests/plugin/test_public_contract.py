"""The published surface as an executable contract: fixed names, keys and options (AAP 0.4.9).
AAP 0.4.9 fixes a set for ``0.1.0``, addable but never renamed or removed, with a docstring on
every public name. The inner ``pytester`` run requests all twelve fixtures and its records are read.
"""

import inspect
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from click.testing import CliRunner

import foundry
import foundry.assertions
import foundry.cli.main
import foundry.config
import foundry.core.exceptions
import foundry.fixtures.plugin
import foundry.http
from foundry.reporting.record import Outcome, ResultRecord, RunError

REACHES: tuple[str, ...] = (
    "fixture:foundry_http",
    "fixture:foundry_api",
    "fixture:foundry_db",
    "fixture:foundry_factory",
    "fixture:foundry_clock",
    "fixture:foundry_poll",
    "fixture:foundry_config",
    "fixture:foundry_profile",
    "fixture:foundry_run_id",
    "fixture:foundry_artifacts",
    "fixture:foundry_data",
    "fixture:foundry_log_capture",
    "format:results.json",
)
_FIXTURE_NAMES: Final[str] = (
    "foundry_config foundry_profile foundry_run_id foundry_artifacts foundry_http foundry_api "
    "foundry_data foundry_db foundry_factory foundry_clock foundry_log_capture foundry_poll"
)
FIXTURE_SCOPES: Final[Mapping[str, str]] = dict(
    zip(_FIXTURE_NAMES.split(), ("session " * 4 + "function " * 8).split(), strict=True)
)
OUTCOMES: Final[str] = (
    "passed failed skipped xfailed xpassed error quarantined_failed quarantine_passed not_run"
)
RESULT_RECORD_KEYS: Final[str] = (
    "record_type run_id started_at finished_at profile nodeid outcome attempts duration_ms markers "
    "failures artifacts quarantine artifacts_disabled"
)
RUN_ERROR_KEYS: Final[str] = "record_type run_id recorded_at worker_id message"
EXCEPTIONS: Final[Mapping[str, tuple[str, str]]] = {
    "FoundryError": ("Exception", "message,hint"),
    "ConfigError": ("FoundryError", "key,layer,path"),
    "DataError": ("FoundryError", "file,line,column"),
    "ContractError": ("FoundryError", "json_path,detail"),
    "TransportError": ("FoundryError", "method,path,cause"),
    "RetryExhaustedError": ("TransportError", "attempts"),
    "CassetteMissError": ("FoundryError", "method,path,cassette_path"),
    "ArtifactError": ("FoundryError", "attempted_path,run_dir"),
}
SUBCOMMANDS: Final[str] = "run init config report doctor"
SUBCOMMAND_OPTIONS: Final[Mapping[str, str]] = {
    "run": "--foundry-profile --foundry-config --foundry-retries --foundry-timeout "
    "--foundry-record --foundry-replay --foundry-json-report",
    "init": "--template --force",
    "config show": "--resolved --format",
    "report compare": "--json",
    "doctor": "--json",
}
DOCSTRING_MODULES: Final[str] = (
    "foundry foundry.fixtures.plugin foundry.http foundry.assertions foundry.config"
)
ENUM_MEMBERS: Final[Mapping[str, tuple[str, ...]]] = {
    "ProfileName": ("default", "local", "ci", "staging"),
    "AuthStrategy": ("none", "bearer", "basic", "header"),
    "RetryPredicate": ("timeout", "connect", "status_5xx", "status_429"),
    "CassetteMode": ("off", "record", "replay", "passthrough"),
    "QuarantineMode": ("report", "strict"),
    "LogLevel": ("DEBUG", "INFO", "WARNING", "ERROR"),
}
SESSION_MEMBERS: Final[str] = "request get post put patch delete"
RESPONSE_MEMBERS: Final[str] = "status_code headers text elapsed_ms source json request_dump"
ASSERTION_ARGUMENTS: Final[Mapping[str, tuple[str, ...]]] = {
    "assert_status": ("response", "expected"),
    "assert_json_subset": ("actual", "expected", "path"),
    "assert_json_schema": ("instance", "schema"),
    "eventually": ("fn", "timeout", "interval", "sleep"),
}
CASES_NAME: Final[str] = "cases.csv"
INNER_TEST_NAME: Final[str] = "test_surface"
_RECORD_PAIRS: Final = ((ResultRecord, RESULT_RECORD_KEYS, 14), (RunError, RUN_ERROR_KEYS, 5))

INNER_TEST_SOURCE: Final[str] = """
import re
from datetime import datetime
from foundry.http import FoundrySession
NAMES = "config profile run_id artifacts http api data db factory clock log_capture poll".split()
def test_every_fixture_resolves(request) -> None:
    got = {name: request.getfixturevalue(f"foundry_{name}") for name in NAMES}
    clock, before = got["clock"], got["clock"].now(); clock.advance(seconds=1)
    node_dir = got["artifacts"].dir_for("test_surface.py::test_every_fixture_resolves")
    assert callable(got["config"].source_of)
    assert (str(got["config"].profile), got["profile"]) == ("default", "default")
    assert re.fullmatch(r"\\d{8}T\\d{6}-[0-9a-f]{6}", got["run_id"]), got["run_id"]
    assert node_dir.is_absolute() and node_dir.parent.name == got["run_id"], node_dir
    assert node_dir.parent.parent.name == "artifacts" and "test_surface.py" in node_dir.name
    assert isinstance(got["http"], FoundrySession) and got["http"].base_url is None
    assert got["api"].base_url == "http://127.0.0.1:1/", got["api"].base_url
    assert len(got["data"].cases("cases.csv")) == 12 and callable(got["db"].execute)
    assert isinstance(got["factory"].name(), str)
    assert isinstance(before, datetime) and (clock.now() - before).total_seconds() == 1.0
    assert got["log_capture"].tail(0) == ()
    assert got["poll"].eventually(lambda: True, timeout=1.0, interval=0.01) is True
"""


def _fixture_marker(candidate: object) -> Any:
    names = ("_fixture_function_marker", "_pytestfixturefunction")
    return next((found for name in names if (found := getattr(candidate, name, None))), None)


def _help_text(*path: str) -> str:
    result = CliRunner().invoke(foundry.cli.main.build_group(), [*path, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


@pytest.mark.parametrize(("name", "scope"), sorted(FIXTURE_SCOPES.items()))
def test_fixture_names_and_scopes(name: str, scope: str) -> None:
    marker = _fixture_marker(getattr(foundry.fixtures.plugin, name, None))
    assert marker is not None and marker.scope == scope, (name, scope, marker)


def test_the_published_sets_and_the_version() -> None:
    module, published = foundry.fixtures.plugin, sorted(FIXTURE_SCOPES)
    registered = sorted(n for n in dir(module) if _fixture_marker(getattr(module, n, None)))
    assert len(published) == 12, published
    assert (registered, sorted(module.FIXTURE_NAMES)) == (published, published), registered
    outcomes = tuple(member.value for member in Outcome)
    assert len(OUTCOMES.split()) == 9, OUTCOMES
    assert set(OUTCOMES.split()) == set(outcomes), (OUTCOMES, list(outcomes))
    assert (len(EXCEPTIONS), foundry.__version__) == (8, "0.1.0")


def test_the_inner_run_resolves_every_fixture_and_writes_the_documented_record(
    pytester: pytest.Pytester, repo_root: Path
) -> None:
    for model, keys, size in _RECORD_PAIRS:
        missing = [key for key in keys.split() if key not in model.model_fields]
        assert not missing and len(keys.split()) == size, (model.__name__, missing)
    source = repo_root / "tests" / "fixtures" / CASES_NAME
    (pytester.path / CASES_NAME).write_bytes(source.read_bytes())
    pytester.makepyfile(**{INNER_TEST_NAME: INNER_TEST_SOURCE})
    result = pytester.runpytest(
        "--foundry-http.base_url=http://127.0.0.1:1/", f"--foundry-data.dir={pytester.path}"
    )
    assert (result.ret, result.parseoutcomes().get("passed")) == (0, 1)
    loaded = json.loads((pytester.path / "results.json").read_text(encoding="utf-8"))
    records = [element for element in loaded if element.get("record_type") == "result"]
    assert records, f"{pytester.path} holds no result record for the inner test"
    assert {r["nodeid"] for r in records} == {f"{INNER_TEST_NAME}.py::test_every_fixture_resolves"}
    assert not [key for r in records for key in RESULT_RECORD_KEYS.split() if key not in r]
    assert all(r["outcome"] in OUTCOMES.split() for r in records)


@pytest.mark.parametrize(("name", "entry"), EXCEPTIONS.items())
def test_exception_names_bases_and_attributes(name: str, entry: tuple[str, str]) -> None:
    base_name, attributes = entry
    re_exported = getattr(foundry, name, None)
    assert re_exported is getattr(foundry.core.exceptions, name, None), f"foundry.{name} differs"
    assert name in foundry.__all__, f"foundry.__all__ omits {name}"
    base = Exception if base_name == "Exception" else getattr(foundry.core.exceptions, base_name)
    assert base in re_exported.__bases__, (name, re_exported.__bases__)
    parameters = inspect.signature(re_exported.__init__).parameters
    annotated = getattr(re_exported, "__annotations__", {})
    absent = [a for a in attributes.split(",") if a not in parameters and a not in annotated]
    assert not absent, f"{name} does not publish {absent}"


@pytest.mark.parametrize("module_name", DOCSTRING_MODULES.split())
def test_public_names_have_docstrings(module_name: str) -> None:
    module = __import__(module_name, fromlist=["__all__"])
    names = getattr(module, "__all__", None) or [
        n for n in dir(module) if getattr(getattr(module, n), "__module__", "") == module_name
    ]
    offenders = []
    for name in names:
        doc = getattr(getattr(module, name), "__doc__", None)
        if callable(getattr(module, name)) and not (doc or "").strip():
            offenders.append(name)
    assert not offenders, f"{module_name} publishes {offenders} without a docstring"


def test_the_documented_subcommands_and_the_published_members() -> None:
    section = _help_text().partition("Commands:")[2]
    rows = [line.split() for line in section.splitlines() if line.strip()]
    assert [row[0] for row in rows] == SUBCOMMANDS.split(), rows
    assert all(len(row) > 1 for row in rows), f"a subcommand has no description: {rows}"
    assert len(SUBCOMMAND_OPTIONS) == 5, sorted(SUBCOMMAND_OPTIONS)
    missing = {
        path: [o for o in options.split() if o not in _help_text(*path.split())]
        for path, options in SUBCOMMAND_OPTIONS.items()
    }
    assert not any(missing.values()), {p: f for p, f in missing.items() if f}
    session, response = foundry.http.FoundrySession, foundry.http.FoundryResponse
    assert (len(SESSION_MEMBERS.split()), len(RESPONSE_MEMBERS.split())) == (6, 7)
    assert all(callable(getattr(session, m, None)) for m in SESSION_MEMBERS.split())
    assert all(hasattr(response, m) for m in RESPONSE_MEMBERS.split())
    assert callable(response.json) and callable(response.request_dump)
    request = inspect.signature(session.request).parameters
    assert list(request)[:3] == ["self", "method", "path"], list(request)
    kinds = {request[k].kind for k in ("params", "json", "headers", "timeout")}
    assert kinds == {inspect.Parameter.KEYWORD_ONLY}
    exported = set(foundry.assertions.__all__)
    assert len(ASSERTION_ARGUMENTS) == 4 and not set(ASSERTION_ARGUMENTS) - exported
    for name, args in ASSERTION_ARGUMENTS.items():
        given = inspect.signature(getattr(foundry.assertions, name)).parameters
        assert not [a for a in args if a not in given], (name, list(given))
    for enum_name, members in sorted(ENUM_MEMBERS.items()):
        offered = getattr(foundry.config, enum_name, None)
        assert offered is not None, enum_name
        assert tuple(member.value for member in offered) == members, enum_name
