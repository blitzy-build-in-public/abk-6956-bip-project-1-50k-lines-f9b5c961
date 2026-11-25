"""End-to-end proof that ``foundry init`` scaffolds a project which runs green offline.

AAP 0.14.1 criterion 10 is the literal sequence ``foundry init <dir> && cd <dir> && pytest`` ending
at exit 0 with at least one passing test. Implicit requirement I11 supplies the condition that
sequence assumes: no network and no echo server are available, so the scaffold has to be green by
construction rather than because something happened to be listening. Both templates the command
offers are driven here -- ``minimal``, which writes the commented configuration, the smoke test and
a data file with its JSON Schema, and ``api``, which adds an HTTP test that the cassette the
scaffold ships replays -- together with the refusal rule of AAP 0.7.4: a non-empty directory
without ``--force`` exits 2 having created nothing, and ``--force`` rewrites identical files.

Every invocation is a child process driven through the shared ``run_foundry`` and ``run_pytest``
fixtures, because AAP 0.14.5 accepts only the real entry point as evidence: the installed console
script and pytest itself. Nothing here calls into :mod:`foundry.cli.init` in process, starts a
server, or passes a flag that would let the scaffold reach the network.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

import pytest

SUCCESS: Final[int] = 0
"""``foundry init`` wrote the scaffold, and the scaffolded suite passed (AAP 0.4.4)."""

USAGE_OR_CONFIG: Final[int] = 2
"""The code Foundry publishes for a usage or configuration error (AAP 0.4.4)."""

REFUSAL_EXIT: Final[int] = 2
"""The code a refused ``foundry init`` exits with: nothing created, the reason on stderr."""

EXPECTED_MINIMAL_FILES: Final[tuple[str, ...]] = (
    "foundry.toml",
    "README.md",
    ".gitignore",
    "tests/test_smoke.py",
)
"""The files both templates write, as POSIX paths relative to the scaffold root (AAP 0.7.4).

The data file and its schema are checked by suffix instead of by name, because which file the
template ships is the CLI package's choice and not part of the command's contract.
"""

EXPECTED_TESTDATA_SUFFIXES: Final[tuple[str, ...]] = (".csv", ".schema.json")
"""One file of each kind must appear under ``testdata/`` (AAP 0.7.4)."""

IGNORE_ENTRY: Final[str] = "artifacts"
"""The entry the scaffold's ``.gitignore`` must carry, written with or without a trailing slash."""

_IGNORED_TREE_PARTS: Final[frozenset[str]] = frozenset({"__pycache__", ".pytest_cache"})
"""Directories a child process creates that say nothing about what ``init`` itself wrote."""

_BYTECODE_SUFFIX: Final[str] = ".pyc"
"""The one file suffix :func:`_tree` ignores, for the same reason."""

_PASSED_SUMMARY: Final[re.Pattern[str]] = re.compile(r"(\d+) passed")
"""The summary fragment pytest prints for a run that had passing tests."""

_INTERACTION_REQUEST_KEYS: Final[tuple[str, ...]] = (
    "method",
    "path",
    "query",
    "body_sha256",
    "headers_subset",
)
"""The request fields one recorded interaction carries (AAP 0.4.6)."""

_INTERACTION_RESPONSE_KEYS: Final[tuple[str, ...]] = ("status", "headers", "body")
"""The response fields one recorded interaction carries (AAP 0.4.6)."""


class _Completed(Protocol):
    """The three attributes of the ``subprocess.CompletedProcess`` the shared fixtures return.

    The fixtures are annotated with :class:`subprocess.CompletedProcess`, which this module names
    only through a protocol: an end-to-end module that imported :mod:`subprocess` itself would be
    indistinguishable from one that bypassed the fixtures, which is the property the offline and
    entry-point rules depend on.
    """

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class _Scaffold:
    """A scaffolded project together with the ``foundry init`` invocation that wrote it.

    Attributes:
        project: The scaffold root, inside the session's temporary base directory.
        printed: The command's stdout lines, which AAP 0.7.4 makes the list of created paths.
    """

    project: Path
    printed: tuple[str, ...]


def _failure_detail(label: str, result: _Completed) -> str:
    """Return the diagnostic a failed child invocation reports: its status and both streams."""
    return (
        f"{label} exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


def _tree(root: Path) -> tuple[tuple[str, str | None], ...]:
    """Return a sorted snapshot of everything under ``root``, as relative paths and digests.

    A directory carries ``None`` in place of a digest, so creating, removing or emptying one changes
    the snapshot. ``__pycache__``, ``*.pyc`` and ``.pytest_cache`` are skipped because a child
    pytest process writes them; every other byte under ``root`` is what the refusal and ``--force``
    rules are compared against.
    """
    entries: list[tuple[str, str | None]] = []
    for path in sorted(root.rglob("*")):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if _IGNORED_TREE_PARTS.intersection(relative.parts):
            continue
        if path.suffix == _BYTECODE_SUFFIX:
            continue
        digest = None if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append((relative.as_posix(), digest))
    return tuple(entries)


def _printed_paths(result: _Completed) -> tuple[str, ...]:
    """Return the non-empty, stripped lines of the command's stdout."""
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def _passing_count(output: str) -> int:
    """Return the passing count from pytest's summary line, failing when there is none."""
    match = _PASSED_SUMMARY.search(output)
    assert match is not None, f"no 'N passed' summary line in the child output:\n{output}"
    return int(match.group(1))


def _load_results(project: Path) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Return the ``results.json`` records of ``project`` split into results and run errors.

    AAP 0.4.6 discriminates the document's elements by ``record_type``: per-test result records and,
    when the run itself failed, ``run_error`` elements that carry no node id.
    """
    document_path = project / "results.json"
    assert document_path.is_file(), (
        f"the scaffolded run wrote no results.json in {project}; a plain pytest invocation there "
        "must produce the three report files (AAP 0.14.1 criterion 10)"
    )
    document = json.loads(document_path.read_text(encoding="utf-8"))
    assert isinstance(document, list), f"{document_path} holds a JSON document, not an array"
    results = tuple(entry for entry in document if entry.get("record_type") == "result")
    errors = tuple(entry for entry in document if entry.get("record_type") == "run_error")
    return results, errors


def _assert_cassette_shape(document: Any) -> None:
    """Assert one cassette document carries every field AAP 0.4.6 fixes.

    The scaffold ships the recording its HTTP test replays, so a document missing an interaction --
    or missing a matchable request field such as the body hash -- would fail at replay time in a way
    a reader of the template could not diagnose.
    """
    assert isinstance(document, dict), "a cassette document is a JSON object (AAP 0.4.6)"
    missing = {"recorded_at", "redacted_keys", "interactions"} - set(document)
    assert not missing, f"the shipped cassette omits {sorted(missing)} (AAP 0.4.6)"
    assert isinstance(document["recorded_at"], str), "recorded_at is an ISO 8601 timestamp"
    assert isinstance(document["redacted_keys"], list), "redacted_keys is a list of key names"
    interactions = document["interactions"]
    assert isinstance(interactions, list) and interactions, (
        "a replayable cassette holds at least one interaction (AAP 0.4.6)"
    )
    for interaction in interactions:
        request = interaction["request"]
        response = interaction["response"]
        for key in _INTERACTION_REQUEST_KEYS:
            assert key in request, f"a recorded request omits {key} (AAP 0.4.6)"
        for key in _INTERACTION_RESPONSE_KEYS:
            assert key in response, f"a recorded response omits {key} (AAP 0.4.6)"


@pytest.fixture(scope="module")
def minimal_scaffold(
    tmp_path_factory: pytest.TempPathFactory,
    run_foundry: Callable[..., _Completed],
) -> _Scaffold:
    """Module scope: one minimal scaffold, shared by the creation test and the refusal test.

    ``foundry init`` is the process under test, so scaffolding a second project for the refusal test
    would spend a subprocess on no new fact. The project lives in the session's temporary base
    directory rather than at a literal path such as ``/tmp/demo``, because the outer suite runs
    under ``-n auto`` and two workers must never share one directory.
    """
    root = tmp_path_factory.mktemp("minimal")
    project = root / "demo"
    result = run_foundry("init", str(project), cwd=root)
    assert result.returncode == SUCCESS, _failure_detail("foundry init", result)
    return _Scaffold(project=project, printed=_printed_paths(result))


def test_minimal_scaffold_is_created_and_runs_green(
    minimal_scaffold: _Scaffold,
    run_pytest: Callable[..., _Completed],
) -> None:
    """Criterion 10, first half: the documented layout, then a green suite with nothing listening.

    The passing count comes from pytest's own summary line and the records from the
    ``results.json`` the installed plugin wrote inside the scaffold, so the run proves the plugin
    activates there with no configuration beyond the file ``init`` wrote.
    """
    project = minimal_scaffold.project
    for relative in EXPECTED_MINIMAL_FILES:
        assert (project / relative).is_file(), (
            f"foundry init wrote no {relative} into {project}: the minimal template ships "
            f"{', '.join(EXPECTED_MINIMAL_FILES)} (AAP 0.7.4)"
        )
    testdata_dir = project / "testdata"
    assert testdata_dir.is_dir(), f"foundry init created no {testdata_dir} directory (AAP 0.7.4)"
    testdata = sorted(path for path in testdata_dir.iterdir() if path.is_file())
    for suffix in EXPECTED_TESTDATA_SUFFIXES:
        assert any(path.name.endswith(suffix) for path in testdata), (
            f"testdata/ holds no {suffix} file: {[path.name for path in testdata]} (AAP 0.7.4)"
        )
    assert (project / "artifacts").is_dir(), (
        f"foundry init did not create {project / 'artifacts'}; the ignore entry names a directory "
        "the scaffold owns (AAP 0.7.4)"
    )
    ignore_lines = {
        line.strip().rstrip("/")
        for line in (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert IGNORE_ENTRY in ignore_lines, (
        f".gitignore carries no {IGNORE_ENTRY} entry: {sorted(ignore_lines)}"
    )

    # A TOMLDecodeError escaping here is a defect in the template, not in this test: foundry.toml is
    # the document a plain pytest run inside the scaffold parses while it resolves configuration.
    document = tomllib.loads((project / "foundry.toml").read_text(encoding="utf-8"))
    assert isinstance(document, dict), "foundry.toml holds a TOML document"

    created = [
        *EXPECTED_MINIMAL_FILES,
        *(path.relative_to(project).as_posix() for path in testdata),
    ]
    printed = minimal_scaffold.printed
    assert len(printed) >= len(created), (
        f"foundry init printed {len(printed)} line(s) for {len(created)} created file(s); the "
        f"created paths print one per line (AAP 0.7.4): {printed}"
    )
    for relative in created:
        assert any(PurePosixPath(line).as_posix().endswith(relative) for line in printed), (
            f"foundry init never printed {relative}, so its output does not list what it wrote "
            f"(AAP 0.7.4): {printed}"
        )

    # No server is started and no network flag is passed: the smoke test requests only
    # foundry_config, so I11's offline condition holds by construction rather than by luck.
    run = run_pytest(cwd=project)
    output = f"{run.stdout}\n{run.stderr}"
    assert run.returncode == SUCCESS, _failure_detail("pytest in the scaffold", run)
    assert "passed" in output, output
    assert _passing_count(output) >= 1, f"the scaffolded suite passed nothing:\n{output}"

    results, errors = _load_results(project)
    assert errors == (), f"the scaffolded run reported a run error: {errors}"
    assert any(record.get("outcome") == "passed" for record in results), (
        f"results.json records no passing test: {results}"
    )


def test_second_init_into_a_non_empty_directory_is_refused_then_forced(
    minimal_scaffold: _Scaffold,
    run_foundry: Callable[..., _Completed],
) -> None:
    """AAP 0.7.4's refusal rule: exit 2, the reason on stderr, and a directory left byte-identical.

    The second half is the same rule seen from the other side: ``--force`` is what an operator
    passes to write anyway, and since the scaffold is deterministic it must leave the existing bytes
    alone.
    """
    project = minimal_scaffold.project
    before = _tree(project)
    assert before, f"{project} is empty, so the refusal below would prove nothing"

    refused = run_foundry("init", str(project), cwd=project.parent)
    assert refused.returncode == REFUSAL_EXIT, (
        f"a refusal must exit {USAGE_OR_CONFIG}, the code Foundry publishes for a usage or "
        f"configuration error (AAP 0.4.4): {_failure_detail('foundry init refusal', refused)}"
    )
    assert refused.stderr.strip(), (
        "the refusal printed no reason on stderr, which is where AAP 0.7.4 reports it:\n"
        f"{_failure_detail('foundry init refusal', refused)}"
    )
    assert _tree(project) == before, (
        "a refused foundry init created, removed or modified a file; every destination is resolved "
        "before the first byte is written (AAP 0.7.4)"
    )

    forced = run_foundry("init", str(project), "--force", cwd=project.parent)
    assert forced.returncode == SUCCESS, _failure_detail("foundry init --force", forced)
    assert _tree(project) == before, (
        "foundry init --force changed a byte of a scaffold it had already written; two runs must "
        "write identical files (AAP 0.13.1)"
    )


def test_api_template_adds_a_replayable_http_test(
    tmp_path: Path,
    run_foundry: Callable[..., _Completed],
    run_pytest: Callable[..., _Completed],
) -> None:
    """The api scaffold: the minimal layout plus an HTTP test its shipped cassette replays offline.

    Nothing listens at the template's ``http.base_url`` and no server is started here. The template
    selects cassette mode ``replay``, so the interaction it ships is served from the transport and
    no socket is opened (AAP 0.7.4, assumption A7).
    """
    project = tmp_path / "api-demo"
    result = run_foundry("init", str(project), "--template", "api", cwd=tmp_path)
    assert result.returncode == SUCCESS, _failure_detail("foundry init --template api", result)
    for relative in EXPECTED_MINIMAL_FILES:
        assert (project / relative).is_file(), (
            f"the api template inherits the minimal layout, yet {relative} is missing (AAP 0.7.4)"
        )

    api_test = project / "tests" / "test_api.py"
    assert api_test.is_file(), f"the api template wrote no {api_test} (AAP 0.7.4)"
    assert "foundry_api" in api_test.read_text(encoding="utf-8"), (
        "tests/test_api.py never requests the foundry_api fixture, so it would not exercise the "
        "HTTP layer the api template exists for"
    )
    cassettes = sorted((project / "cassettes").glob("*.json"))
    assert cassettes, f"the api template shipped no cassette in {project / 'cassettes'}"
    _assert_cassette_shape(json.loads(cassettes[0].read_text(encoding="utf-8")))

    run = run_pytest(cwd=project)
    output = f"{run.stdout}\n{run.stderr}"
    assert run.returncode == SUCCESS, _failure_detail("pytest in the api scaffold", run)
    results, errors = _load_results(project)
    assert errors == (), f"the api scaffold's run reported a run error: {errors}"
    assert len(results) >= 2, (
        f"the api scaffold reported {len(results)} test(s); the smoke test and the HTTP test both "
        f"write a record: {[record.get('nodeid') for record in results]}"
    )
    assert {record.get("outcome") for record in results} == {"passed"}, (
        f"the api scaffold did not pass every test, so the shipped cassette did not replay: "
        f"{[(record.get('nodeid'), record.get('outcome')) for record in results]}\n{output}"
    )
    assert all(record.get("failures") == [] for record in results), results

    report = (project / "report.html").read_text(encoding="utf-8")
    assert "http://" not in report and "https://" not in report, (
        "report.html references an http(s) location, so the scaffold's own report does not open "
        "offline (AAP 0.4.6)"
    )
