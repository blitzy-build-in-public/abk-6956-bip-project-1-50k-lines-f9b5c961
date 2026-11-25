"""The published surface as an executable contract: fixed names, keys, exit codes and options.

AAP 0.4.9 fixes a set of names for ``0.1.0`` -- addable, never renamed and never removed -- and
AAP 0.13.2 requires every public name to carry a docstring and every configuration key to be visible
through ``foundry config show``. This module is the executable form of both, one group per contract
source: AAP 0.4.3 (fixtures, client surface, assertions, enumerations, exceptions, version),
AAP 0.4.4 (the subcommands and their options), AAP 0.4.5 (the 28 keys, both directions, through the
command line), AAP 0.4.6 (the record fields and the nine outcomes) and AAP 0.4.9 (the direction of
the promise: a removal or rename always fails, while an addition is reported).

Introspection is legitimate because the contract *is* the surface, with one exception: AAP 0.14.3
forbids a capability that exists only in a unit test, so one inner test driven by ``pytester``
requests all twelve fixtures and asserts the member documented for each, and the result-record group
reads the ``results.json`` that run writes. Nothing here redeclares a shared fixture, adds a
conftest, sleeps, reads a clock or issues an HTTP request.
"""

from __future__ import annotations

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
"""The surfaces this module drives through a real pytest session (AAP 0.14.1, criterion 13).

``tests/plugin/test_reachability.py`` parses this with :mod:`ast`, so each entry names a fixture the
inner run requests or a file it writes.
"""

FIXTURE_SCOPES: Final[Mapping[str, str]] = {
    "foundry_config": "session",
    "foundry_profile": "session",
    "foundry_run_id": "session",
    "foundry_artifacts": "session",
    "foundry_http": "function",
    "foundry_api": "function",
    "foundry_data": "function",
    "foundry_db": "function",
    "foundry_factory": "function",
    "foundry_clock": "function",
    "foundry_log_capture": "function",
    "foundry_poll": "function",
}
"""The twelve public fixtures and the scope AAP 0.4.3 publishes for each, verbatim."""

OUTCOMES: Final[tuple[str, ...]] = (
    "passed",
    "failed",
    "skipped",
    "xfailed",
    "xpassed",
    "error",
    "quarantined_failed",
    "quarantine_passed",
    "not_run",
)
"""The nine outcome values of AAP 0.4.6, in the order that table lists them."""

RESULT_RECORD_KEYS: Final[tuple[str, ...]] = (
    "record_type",
    "run_id",
    "started_at",
    "finished_at",
    "profile",
    "nodeid",
    "outcome",
    "attempts",
    "duration_ms",
    "markers",
    "failures",
    "artifacts",
    "quarantine",
)
"""The thirteen fields a result record carries, in the order AAP 0.4.6 tabulates them."""

RUN_ERROR_KEYS: Final[tuple[str, ...]] = (
    "record_type",
    "run_id",
    "recorded_at",
    "worker_id",
    "message",
)
"""The five fields of a run-error element, the second kind ``results.json`` holds (AAP 0.4.6)."""

CONFIG_KEYS: Final[tuple[str, ...]] = (
    "profile",
    "http.base_url",
    "http.timeout",
    "http.headers",
    "http.retries.max_attempts",
    "http.retries.backoff_base",
    "http.retries.backoff_max",
    "http.retries.retry_on",
    "http.auth.strategy",
    "http.auth.token_env",
    "http.auth.user_env",
    "http.auth.password_env",
    "http.auth.header_name",
    "http.auth.header_value_env",
    "artifacts.dir",
    "artifacts.keep_passed",
    "reporting.html",
    "reporting.json",
    "data.dir",
    "data.schema_strict",
    "cassette.dir",
    "cassette.mode",
    "execution.retries",
    "execution.timeout",
    "execution.quarantine_mode",
    "secrets.redact_keys",
    "logging.level",
    "logging.capture_lines",
)
"""The 28 dotted configuration keys of AAP 0.4.5, row for row and in that table's order."""

EXCEPTIONS: Final[Mapping[str, tuple[str, tuple[str, ...]]]] = {
    "FoundryError": ("Exception", ("message", "hint")),
    "ConfigError": ("FoundryError", ("key", "layer", "path")),
    "DataError": ("FoundryError", ("file", "line", "column")),
    "ContractError": ("FoundryError", ("json_path", "detail")),
    "TransportError": ("FoundryError", ("method", "path", "cause")),
    "RetryExhaustedError": ("TransportError", ("attempts",)),
    "CassetteMissError": ("FoundryError", ("method", "path", "cassette_path")),
    "ArtifactError": ("FoundryError", ("attempted_path", "run_dir")),
}
"""Each exception's documented base and attributes (AAP 0.4.3), all of them part of the promise."""

SUBCOMMANDS: Final[tuple[str, ...]] = ("run", "init", "config", "report", "doctor")
"""The five subcommands of AAP 0.4.4, in the order that table and ``--help`` publish them."""

SUBCOMMAND_OPTIONS: Final[Mapping[tuple[str, ...], tuple[str, ...]]] = {
    ("run",): (
        "--foundry-profile",
        "--foundry-config",
        "--foundry-retries",
        "--foundry-timeout",
        "--foundry-record",
        "--foundry-replay",
        "--foundry-json-report",
    ),
    ("init",): ("--template", "--force"),
    ("config", "show"): ("--resolved", "--format"),
    ("report", "compare"): ("--json",),
    ("doctor",): ("--json",),
}
"""The options AAP 0.4.4 documents for each subcommand, keyed by its argument path.

The generic ``--foundry-<dotted.key>`` overrides are covered by the key-table test, and the strict
"nothing undocumented" parse belongs to ``tests/plugin/test_cli_surface.py``.
"""

DOCSTRING_MODULES: Final[tuple[str, ...]] = (
    "foundry",
    "foundry.fixtures.plugin",
    "foundry.http",
    "foundry.assertions",
    "foundry.config",
)
"""The modules AAP 0.4.3 declares fixtures, classes, helpers and enumerations in (AAP 0.13.2)."""

ENUM_MEMBERS: Final[Mapping[str, tuple[str, ...]]] = {
    "ProfileName": ("default", "local", "ci", "staging"),
    "AuthStrategy": ("none", "bearer", "basic", "header"),
    "RetryPredicate": ("timeout", "connect", "status_5xx", "status_429"),
    "CassetteMode": ("off", "record", "replay", "passthrough"),
    "QuarantineMode": ("report", "strict"),
    "LogLevel": ("DEBUG", "INFO", "WARNING", "ERROR"),
}
"""The config enumerations of AAP 0.4.3 with the values AAP 0.4.5 accepts for each of them."""

SESSION_MEMBERS: Final[tuple[str, ...]] = ("request", "get", "post", "put", "patch", "delete")
"""The request method plus the five verb conveniences of ``FoundrySession`` (AAP 0.4.3, A13)."""

RESPONSE_MEMBERS: Final[tuple[str, ...]] = (
    "status_code",
    "headers",
    "text",
    "elapsed_ms",
    "source",
    "json",
    "request_dump",
)
"""Every member ``FoundryResponse`` publishes (AAP 0.4.3); a rename or removal breaks it."""

ASSERTION_ARGUMENTS: Final[Mapping[str, tuple[str, ...]]] = {
    "assert_status": ("response", "expected"),
    "assert_json_subset": ("actual", "expected", "path"),
    "assert_json_schema": ("instance", "schema"),
    "eventually": ("fn", "timeout", "interval", "sleep"),
}
"""The four helpers of AAP 0.4.3 and the arguments that table gives each signature."""

CASES_NAME: Final[str] = "cases.csv"
"""The 12-row data file the inner project loads, copied from ``tests/fixtures``."""

INNER_TEST_NAME: Final[str] = "test_surface"
"""Stem of the inner module that requests all twelve fixtures and asserts what each returned."""

INNER_TASK_NAME: Final[str] = "test_every_fixture_resolves"
"""Name of that inner test, which is also the node id ``results.json`` records it under."""

RESULT_DOCUMENT: Final[str] = "results.json"
"""The result document the inner run writes into its own directory (AAP 0.4.6)."""

INNER_CONFTEST_SOURCE: Final[str] = '''
"""Override the public configuration fixture: this project's base URL and data directory."""

from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:1/"


@pytest.fixture(scope="session")
def foundry_config(foundry_config):
    """Pytest's documented same-name override: the parameter is the public session fixture.

    ``http.base_url`` becomes an unused loopback address and ``data.dir`` becomes this project,
    where the case file was copied; every other key keeps the parent's value.
    """
    http_config = foundry_config.http.model_copy(update={"base_url": BASE_URL})
    data_config = foundry_config.data.model_copy(update={"dir": HERE})
    return foundry_config.model_copy(update={"http": http_config, "data": data_config})
'''
"""The inner conftest: one fixture override, and nothing else (AAP 0.4.3's override contract)."""

INNER_TEST_SOURCE: Final[str] = '''
"""Request every published fixture at once and assert the member AAP 0.4.3 documents for it."""

import pathlib
import re
from datetime import datetime

from foundry.http import FoundrySession

_RUN_ID = re.compile(r"\\d{8}T\\d{6}-[0-9a-f]{6}")
_NODEID = "test_surface.py::test_every_fixture_resolves"
_BASE_URL = "http://127.0.0.1:1/"


def test_every_fixture_resolves(
    foundry_config,
    foundry_profile,
    foundry_run_id,
    foundry_artifacts,
    foundry_http,
    foundry_api,
    foundry_data,
    foundry_db,
    foundry_factory,
    foundry_clock,
    foundry_log_capture,
    foundry_poll,
) -> None:
    """Every fixture resolves to what its contract publishes, and no request is issued."""
    assert callable(foundry_config.source_of)
    assert str(foundry_config.profile) == "default", foundry_config.profile
    assert foundry_profile == "default", foundry_profile
    assert _RUN_ID.fullmatch(foundry_run_id), foundry_run_id

    node_dir = foundry_artifacts.dir_for(_NODEID)
    assert isinstance(node_dir, pathlib.Path), node_dir
    assert node_dir.is_absolute(), node_dir
    assert node_dir.parent.name == foundry_run_id, node_dir
    assert node_dir.parent.parent.name == "artifacts", node_dir
    assert "test_surface.py" in node_dir.name, node_dir
    assert "::" not in node_dir.name, node_dir

    assert isinstance(foundry_http, FoundrySession), foundry_http
    assert foundry_http.base_url is None, foundry_http.base_url
    assert isinstance(foundry_api, FoundrySession), foundry_api
    assert foundry_api.base_url == _BASE_URL, foundry_api.base_url

    assert len(foundry_data.cases("cases.csv")) == 12
    assert callable(foundry_db.execute), foundry_db
    assert isinstance(foundry_factory.name(), str)

    before = foundry_clock.now()
    assert isinstance(before, datetime), before
    foundry_clock.advance(seconds=1)
    assert (foundry_clock.now() - before).total_seconds() == 1.0

    assert foundry_log_capture.tail(0) == ()
    assert foundry_poll.eventually(lambda: True, timeout=1.0, interval=0.01) is True
'''
"""The one inner test: all twelve fixtures, and the member AAP 0.4.3 documents for each.

``foundry_db`` is requested without the indirect parametrisation that registers models, which its
contract allows: the session then covers an empty schema, whose ``execute`` is asserted.
"""


def _fixture_marker(candidate: object) -> Any:
    """Return ``candidate``'s fixture marker, or ``None`` when it is not a fixture.

    pytest 8 keeps the marker as ``_fixture_function_marker``, older releases as
    ``_pytestfixturefunction``; both are read.
    """
    for attribute in ("_fixture_function_marker", "_pytestfixturefunction"):
        marker = getattr(candidate, attribute, None)
        if marker is not None:
            return marker
    return None


def _fixture_functions() -> dict[str, Any]:
    """Return every ``foundry_*`` attribute of the fixture module carrying a fixture marker."""
    return {
        name: getattr(foundry.fixtures.plugin, name)
        for name in dir(foundry.fixtures.plugin)
        if name.startswith("foundry_")
        and _fixture_marker(getattr(foundry.fixtures.plugin, name)) is not None
    }


def _public_names(module: Any) -> tuple[str, ...]:
    """Return a module's public names: its ``__all__``, else the public names it defines itself."""
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return tuple(declared)
    return tuple(
        name
        for name in dir(module)
        if not name.startswith("_")
        and getattr(getattr(module, name), "__module__", None) == module.__name__
    )


def _flatten(document: Mapping[str, Any], *, prefix: str = "") -> dict[str, Any]:
    """Return ``document`` as dotted leaves, treating a documented key's own mapping as one leaf.

    The key set says where a document stops nesting: the empty ``http.headers`` table is one leaf,
    and a group no key declares keeps nesting, so the comparison bites in both directions.
    """
    leaves: dict[str, Any] = {}
    for name, value in document.items():
        dotted = f"{prefix}{name}"
        if isinstance(value, Mapping) and dotted not in CONFIG_KEYS:
            leaves.update(_flatten(value, prefix=f"{dotted}."))
        else:
            leaves[dotted] = value
    return leaves


def _help_text(*path: str) -> str:
    """Return the ``--help`` text of the command at ``path``, failing if it cannot be printed."""
    result = CliRunner().invoke(foundry.cli.main.build_group(), [*path, "--help"])
    assert result.exit_code == 0, (
        f"`foundry {' '.join(path)} --help` exited {result.exit_code}, so the documented surface "
        f"cannot be inspected:\n{result.output}"
    )
    return result.output


def _listed_commands(help_text: str) -> dict[str, str]:
    """Return the ``Commands:`` section of a group's help as ``name -> description``.

    click indents each entry by two spaces, so splitting once leaves the description.
    """
    commands: dict[str, str] = {}
    in_section = False
    for line in help_text.splitlines():
        if line.strip() == "Commands:":
            in_section = True
            continue
        if not in_section:
            continue
        if not line.strip():
            break
        parts = line.split(maxsplit=1)
        commands[parts[0]] = parts[1] if len(parts) > 1 else ""
    assert commands, f"`foundry --help` printed no Commands section:\n{help_text}"
    return commands


def _run_full_surface(pytester: pytest.Pytester, repo_root: Path) -> tuple[pytest.RunResult, Path]:
    """Run the inner project once and return its result and the directory it ran in.

    ``tests/fixtures/cases.csv`` is copied in, so the run resolves it under its own ``data.dir``.
    """
    cases = repo_root / "tests" / "fixtures" / CASES_NAME
    assert cases.is_file(), f"the shared case file is missing: {cases}"
    pytester.makeconftest(INNER_CONFTEST_SOURCE)
    (pytester.path / CASES_NAME).write_bytes(cases.read_bytes())
    pytester.makepyfile(**{INNER_TEST_NAME: INNER_TEST_SOURCE})
    return pytester.runpytest(), pytester.path


def _result_elements(directory: Path) -> list[dict[str, Any]]:
    """Return the elements of the run's ``results.json``, in document order."""
    document = directory / RESULT_DOCUMENT
    assert document.is_file(), (
        f"the run wrote no {RESULT_DOCUMENT} into {directory}, so the record contract of AAP 0.4.6 "
        "cannot be checked"
    )
    parsed = json.loads(document.read_text(encoding="utf-8"))
    assert isinstance(parsed, list), f"{document} holds {type(parsed).__name__}, not an array"
    return [element for element in parsed if isinstance(element, dict)]


@pytest.mark.parametrize(("name", "scope"), sorted(FIXTURE_SCOPES.items()))
def test_fixture_names_and_scopes(name: str, scope: str) -> None:
    """AAP 0.4.3: each fixture exists in the registration module at its published scope."""
    marker = _fixture_marker(getattr(foundry.fixtures.plugin, name, None))
    assert marker is not None, (
        f"{name} is absent from foundry.fixtures.plugin or carries no fixture marker, so a suite "
        "requesting it cannot run (AAP 0.4.3, AAP 0.4.9: names are added to, never removed)"
    )
    assert marker.scope == scope, (
        f"{name} is registered at scope {marker.scope!r}, but AAP 0.4.3 publishes {scope!r}: a "
        "scope change alters fixture lifecycle for every consuming suite"
    )


def test_the_fixture_module_registers_exactly_the_published_set() -> None:
    """AAP 0.4.3: the family is these twelve, and a thirteenth is named rather than ignored."""
    published = sorted(FIXTURE_SCOPES)
    sources = {
        "the registered fixtures": sorted(_fixture_functions()),
        "FIXTURE_NAMES": sorted(foundry.fixtures.plugin.FIXTURE_NAMES),
    }
    for source, names in sources.items():
        assert names == published, (
            f"foundry.fixtures.plugin lists {names} in {source}, "
            f"but AAP 0.4.3 publishes {published}"
        )


def test_all_twelve_fixtures_resolve_in_a_real_run(
    pytester: pytest.Pytester, repo_root: Path
) -> None:
    """AAP 0.14.3: one real run requests all twelve fixtures and each returns its publication."""
    result, directory = _run_full_surface(pytester, repo_root)
    assert result.ret == 0, (
        "the run that requests all twelve published fixtures did not pass, so at least one of them "
        f"failed to resolve or broke its documented contract (AAP 0.4.3):\n{result.outlines}"
    )
    result.assert_outcomes(passed=1)
    assert (directory / RESULT_DOCUMENT).is_file(), (
        f"the run wrote no {RESULT_DOCUMENT}, so the plugin's report write did not happen"
    )


def test_result_record_field_set(pytester: pytest.Pytester, repo_root: Path) -> None:
    """AAP 0.4.6: every record carries the thirteen documented fields; an extra one is additive.

    The frozen models are checked beside the written document, so a renamed field fails here too.
    """
    for model, keys in ((ResultRecord, RESULT_RECORD_KEYS), (RunError, RUN_ERROR_KEYS)):
        declared = set(model.model_fields)
        missing = [key for key in keys if key not in declared]
        assert not missing, (
            f"{model.__name__} no longer declares {missing}, which AAP 0.4.6 publishes and "
            f"AAP 0.4.9 forbids renaming or removing; it declares {sorted(declared)}"
        )
    result, directory = _run_full_surface(pytester, repo_root)
    assert result.ret == 0, f"the run failed, so no record can be trusted:\n{result.outlines}"
    records = [
        element for element in _result_elements(directory) if element.get("record_type") == "result"
    ]
    assert records, f"{directory / RESULT_DOCUMENT} holds no result record for the inner test"
    for record in records:
        nodeid = record.get("nodeid", "<no nodeid>")
        assert nodeid == f"{INNER_TEST_NAME}.py::{INNER_TASK_NAME}", nodeid
        missing = [key for key in RESULT_RECORD_KEYS if key not in record]
        assert not missing, (
            f"the record for {nodeid} is missing {missing}, which AAP 0.4.6 publishes and "
            "AAP 0.4.9 forbids renaming or removing"
        )
        assert record["record_type"] == "result", record["record_type"]
        assert record["outcome"] in OUTCOMES, (
            f"the record for {nodeid} reports outcome {record['outcome']!r}, which is not one of "
            f"the nine values of AAP 0.4.6: {list(OUTCOMES)}"
        )


def test_outcome_values() -> None:
    """AAP 0.4.6, AAP 0.4.9: the outcome enumeration is the nine documented values.

    A removal or rename fails; an addition fails too, saying it is an addition, because whether a
    tenth value belongs in this release is a human's call rather than a test's.
    """
    published = tuple(member.value for member in Outcome)
    removed = [value for value in OUTCOMES if value not in published]
    assert not removed, (
        f"Outcome no longer holds {removed}, which AAP 0.4.6 publishes and AAP 0.4.9 forbids "
        f"renaming or removing; the enumeration is {list(published)}"
    )
    added = [value for value in published if value not in OUTCOMES]
    assert not added, (
        f"Outcome holds {added}, which no AAP 0.4.6 outcome value names. An addition is permitted "
        "by AAP 0.4.9, but a consumer switching on the documented nine must be told, so this needs "
        f"a human decision rather than a silently wider enumeration: {list(published)}"
    )


@pytest.mark.parametrize(
    ("name", "base_name", "attributes"),
    [(name, base, attrs) for name, (base, attrs) in EXCEPTIONS.items()],
)
def test_exception_names_bases_and_attributes(
    name: str, base_name: str, attributes: tuple[str, ...]
) -> None:
    """AAP 0.4.3, AAP 0.4.9: each exception's name, base and documented attributes are the promise.

    ``foundry`` re-exports the class object ``foundry.core.exceptions`` defines, so a caller
    catching the re-exported name catches what a module raises. An attribute counts as published
    when the constructor takes it or the class annotates it.
    """
    re_exported = getattr(foundry, name, None)
    assert re_exported is not None, f"foundry does not re-export {name} (AAP 0.4.3)"
    defined = getattr(foundry.core.exceptions, name)
    assert re_exported is defined, (
        f"foundry.{name} is not the class foundry.core.exceptions defines, so a caller catching "
        "one would not catch the other"
    )
    base = Exception if base_name == "Exception" else getattr(foundry.core.exceptions, base_name)
    assert issubclass(re_exported, base) and base in re_exported.__bases__, (
        f"{name} derives from {[c.__name__ for c in re_exported.__bases__]} rather than from "
        f"{base_name} (AAP 0.4.3)"
    )
    parameters = inspect.signature(re_exported.__init__).parameters
    annotated = getattr(re_exported, "__annotations__", {})
    missing = [a for a in attributes if a not in parameters and a not in annotated]
    assert not missing, (
        f"{name} does not publish the documented attribute(s) {missing}: neither its constructor "
        f"nor its annotations carry them (AAP 0.4.3, AAP 0.4.9)"
    )


def test_exception_re_exports_and_version() -> None:
    """AAP 0.4.3: all eight exceptions are in ``foundry.__all__``, and the version is ``0.1.0``."""
    absent = [name for name in EXCEPTIONS if name not in foundry.__all__]
    assert not absent, f"foundry.__all__ omits {absent}, so the star-import surface is incomplete"
    assert foundry.__version__ == "0.1.0", (
        f"foundry.__version__ is {foundry.__version__!r}; AAP 0.4.3 publishes '0.1.0' for this "
        "release, and `foundry --version` prints it"
    )


def test_configuration_key_set_matches_the_documented_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_foundry_env: None
) -> None:
    """AAP 0.4.5, AAP 0.13.2: the 28 keys, compared in both directions through the command line.

    The command runs in an empty directory with no ``FOUNDRY_*`` variable, so it prints the field
    defaults. A missing key and an undocumented key each fail, and the provenance document covers
    the same leaf set, because AAP 0.4.5 promises a source per key.
    """
    monkeypatch.chdir(tmp_path)
    cli = foundry.cli.main.build_group()
    result = CliRunner().invoke(cli, ["config", "show", "--format", "json"])
    assert result.exit_code == 0, (
        f"`foundry config show --format json` exited {result.exit_code}:\n{result.output}"
    )
    document = json.loads(result.output)
    assert isinstance(document, dict), type(document).__name__
    leaves = _flatten(document["resolved"])
    missing = sorted(set(CONFIG_KEYS) - set(leaves))
    undocumented = sorted(set(leaves) - set(CONFIG_KEYS))
    assert not missing, (
        f"`config show` does not resolve {missing}, which AAP 0.4.5 publishes as configuration keys"
    )
    assert not undocumented, (
        f"`config show` resolves {undocumented}, which no AAP 0.4.5 key declares: a key is part of "
        "the published surface, and an undocumented one is invisible to a consumer"
    )
    sources = document["sources"]
    assert sorted(sources) == sorted(leaves), (
        "the provenance document does not cover exactly the resolved keys (AAP 0.4.5 promises a "
        f"source for every key): missing={sorted(set(leaves) - set(sources))} "
        f"extra={sorted(set(sources) - set(leaves))}"
    )


@pytest.mark.parametrize("module_name", DOCSTRING_MODULES)
def test_public_names_have_docstrings(module_name: str) -> None:
    """AAP 0.13.2: every public class or function carries a docstring; data names are exempt."""
    module = __import__(module_name, fromlist=["__all__"])
    offenders = [
        name
        for name in _public_names(module)
        if callable(getattr(module, name))
        and not str(getattr(getattr(module, name), "__doc__", "") or "").strip()
    ]
    assert not offenders, (
        f"{module_name} publishes {offenders} without a docstring, which AAP 0.13.2 requires of "
        "every public name in AAP 0.4.3"
    )


def test_subcommands_are_documented() -> None:
    """AAP 0.4.4, AAP 0.7.4: five subcommands, each described in one line, with its options."""
    commands = _listed_commands(_help_text())
    assert list(commands) == list(SUBCOMMANDS), (
        f"`foundry --help` lists {list(commands)}, but AAP 0.4.4 publishes {list(SUBCOMMANDS)} in "
        "that order"
    )
    undescribed = [name for name, description in commands.items() if not description.strip()]
    assert not undescribed, f"`foundry --help` gives {undescribed} no one-line description"

    missing: dict[str, list[str]] = {}
    for path, options in SUBCOMMAND_OPTIONS.items():
        help_text = _help_text(*path)
        absent = [option for option in options if option not in help_text]
        if absent:
            missing[" ".join(path)] = absent
    assert not missing, (
        "these documented options are absent from their subcommand's `--help`, so an operator "
        f"cannot discover them (AAP 0.4.4, AAP 0.7.4): {missing}"
    )


def test_http_surface_members() -> None:
    """AAP 0.4.3, AAP 0.4.9: the two client types publish the documented members.

    The request signature is checked as a shape: ``method`` and ``path`` positional, the optional
    arguments keyword-only, so ``request("GET", "/x", json={})`` keeps working.
    """
    for member in SESSION_MEMBERS:
        assert callable(getattr(foundry.http.FoundrySession, member, None)), (
            f"FoundrySession.{member} is missing or not callable, so the request surface of "
            "AAP 0.4.3 is incomplete"
        )
    for member in RESPONSE_MEMBERS:
        assert hasattr(foundry.http.FoundryResponse, member), (
            f"FoundryResponse.{member} is missing: AAP 0.4.3 publishes status_code, headers, text, "
            "elapsed_ms, source, json() and request_dump()"
        )
    assert callable(foundry.http.FoundryResponse.json)
    assert callable(foundry.http.FoundryResponse.request_dump)
    parameters = inspect.signature(foundry.http.FoundrySession.request).parameters
    assert list(parameters)[:3] == ["self", "method", "path"], list(parameters)
    for keyword in ("params", "json", "headers", "timeout"):
        assert parameters[keyword].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"FoundrySession.request takes {keyword!r} as {parameters[keyword].kind}, but "
            "AAP 0.4.3 publishes it as a keyword-only argument"
        )


def test_assertion_helpers_and_eventually() -> None:
    """AAP 0.4.3: the four helpers are published, callable, and take their documented arguments."""
    published = set(foundry.assertions.__all__)
    missing = sorted(set(ASSERTION_ARGUMENTS) - published)
    assert not missing, (
        f"foundry.assertions no longer publishes {missing}; AAP 0.4.3 names "
        f"{sorted(ASSERTION_ARGUMENTS)} and AAP 0.4.9 forbids removing one"
    )
    for name, arguments in ASSERTION_ARGUMENTS.items():
        helper = getattr(foundry.assertions, name)
        parameters = inspect.signature(helper).parameters
        absent = [argument for argument in arguments if argument not in parameters]
        assert not absent, (
            f"foundry.assertions.{name} no longer takes {absent}: {list(parameters)} (AAP 0.4.3)"
        )


@pytest.mark.parametrize(("enum_name", "members"), sorted(ENUM_MEMBERS.items()))
def test_configuration_enums(enum_name: str, members: tuple[str, ...]) -> None:
    """AAP 0.4.3, AAP 0.4.5: each published enumeration holds exactly the values its key accepts."""
    published = getattr(foundry.config, enum_name, None)
    assert published is not None, f"foundry.config does not publish {enum_name} (AAP 0.4.3)"
    values = tuple(member.value for member in published)
    assert values == members, (
        f"{enum_name} holds {list(values)}, but AAP 0.4.5 accepts {list(members)} for the keys it "
        "governs: a change here changes what every configuration layer accepts"
    )
