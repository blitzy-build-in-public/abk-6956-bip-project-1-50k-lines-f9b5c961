"""AAP 0.4.7's ``KeyboardInterrupt`` row, driven through a real pytest child process.

The row fixes three outcomes for an interrupted run: partial results are flushed, the report is
written, and pytest's own interrupt code -- ``2`` in the same row -- is preserved rather than
recomputed (AAP 0.4.4: Foundry never rewrites a code pytest produced). The case below observes all
three at once on a three-test project: ``test_alpha`` passes and prints, ``test_beta`` raises
``KeyboardInterrupt``, and ``test_gamma`` is never reached.

**Why a child process.** The interrupt is raised inside the session under test. An in-process
``pytester`` session that raises it would propagate the ``KeyboardInterrupt`` into this suite and
abort it, so the run goes through the shared ``run_pytest`` fixture -- ``python -m pytest`` with a
stripped environment -- and every claim is read back from the files that child wrote. Nothing is
mocked: the inputs are the generated module and the command line.

**The partial document, as observed.** ``results.json`` holds one ``result`` element per collected
item (AAP 0.4.6, assumption A12): ``test_alpha`` with ``outcome == "passed"``, and ``test_beta`` and
``test_gamma`` with ``outcome == "not_run"``, because neither executed to a verdict once the session
stopped. No element claims either of them ran, and ``report.html`` and ``junit.xml`` are written
beside ``results.json`` from the same partial record set.

**Determinism** (AAP 0.14.3): no sleep, no wall-clock read, no socket, and the project is written
under ``tmp_path_factory`` because the shared fixture is session-scoped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from xml.etree import ElementTree

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    import pytest

# The module drives no enumerated fixture, subcommand, data loader or report format through a real
# entry point: it observes the interrupt row of the state table. The empty tuple is the declaration
# that tests/plugin/test_reachability.py parses.
REACHES: tuple[str, ...] = ()

_ROW: Final[str] = "AAP 0.4.7's KeyboardInterrupt row"
"""Named in every assertion message, so a failure reads as the row it contradicts."""

_INTERRUPT_EXIT: Final[int] = 2
"""pytest's interrupt code, which the row preserves and AAP 0.4.4 forbids rewriting."""

_MODULE_NAME: Final[str] = "test_interrupt.py"
"""The one module of the generated project."""

_ALPHA: Final[str] = "test_alpha"
"""The test that passes before the interrupt, whose record is the pass the flush must keep."""

_BETA: Final[str] = "test_beta"
"""The interrupted test: its call phase raises ``KeyboardInterrupt``, so it reaches no verdict."""

_GAMMA: Final[str] = "test_gamma"
"""The test after the interrupted one, which the stopped session never reaches."""

_NOT_RUN: Final[str] = "not_run"
"""AAP 0.4.6's outcome for an item that never executed because the session stopped (A12)."""

_EXPECTED_OUTCOMES: Final[dict[str, str]] = {
    f"{_MODULE_NAME}::{_ALPHA}": "passed",
    f"{_MODULE_NAME}::{_BETA}": _NOT_RUN,
    f"{_MODULE_NAME}::{_GAMMA}": _NOT_RUN,
}
"""One record per collected item: the pass earned before the interrupt, and the two it stopped."""

_RESULTS_NAME: Final[str] = "results.json"
_REPORT_NAME: Final[str] = "report.html"
_JUNIT_NAME: Final[str] = "junit.xml"

_RESULT_SOURCE: Final[str] = f'''"""A pass, then an interrupt, then a test never reached."""


def {_ALPHA}() -> None:
    print("alpha ran")


def {_BETA}() -> None:
    raise KeyboardInterrupt


def {_GAMMA}() -> None:
    print("gamma ran")
'''
"""A whole project: the interrupt is the only input, and no case sleeps or opens a socket."""


def _write_project(root: Path) -> Path:
    """Write the generated module into ``root`` and return the directory as the run's cwd."""
    root.mkdir(parents=True, exist_ok=True)
    (root / _MODULE_NAME).write_text(_RESULT_SOURCE, encoding="utf-8", newline="\n")
    return root


def _read(project: Path, name: str) -> str:
    """Return the text of one file the child wrote, naming what it left behind when it is absent."""
    path = project / name
    assert path.is_file(), (
        f"{_ROW} requires the report to be written, but {name} is missing from {project}; the "
        f"directory holds {sorted(entry.name for entry in project.iterdir())}"
    )
    return path.read_text(encoding="utf-8")


def _result_records(project: Path) -> list[dict[str, Any]]:
    """Return the child's result records from the ``results.json`` array of AAP 0.4.6."""
    document = json.loads(_read(project, _RESULTS_NAME))
    assert isinstance(document, list), (
        f"{_ROW} requires the partial results flushed as AAP 0.4.6's JSON array, but results.json "
        f"holds a {type(document).__name__}"
    )
    records: list[dict[str, Any]] = []
    for element in document:
        assert isinstance(element, dict), (
            f"{_ROW} requires AAP 0.4.6 elements, but results.json holds {element!r}"
        )
        assert element.get("record_type") == "result", (
            f"{_ROW} flushes the partial record set alone -- this serial interrupt loses no "
            f"worker, so no run_error element belongs here -- but the document holds {element!r}"
        )
        records.append(element)
    return records


def test_interrupt_flushes_partial_results_and_writes_the_report(
    tmp_path_factory: pytest.TempPathFactory,
    run_pytest: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    """An interrupted session ends 2 with the partial records and both reports on disk.

    The exit code is the child's own, so a preserved interrupt is observed rather than inferred;
    the record set is what separates a flush from a document rewritten after the fact, because a
    completed run would carry a verdict for each of the three items instead.
    """
    project = _write_project(tmp_path_factory.mktemp("interrupt"))

    child = run_pytest("-q", cwd=project)
    output = child.stdout + child.stderr

    assert child.returncode == _INTERRUPT_EXIT, (
        f"{_ROW} preserves pytest's interrupt code {_INTERRUPT_EXIT}, but the run ended "
        f"{child.returncode}\n{output}"
    )

    records = _result_records(project)
    outcomes = {str(record["nodeid"]): str(record["outcome"]) for record in records}
    assert outcomes == _EXPECTED_OUTCOMES, (
        f"{_ROW} flushes one record per collected item -- the pass earned before the interrupt and "
        f"{_NOT_RUN!r} for the two the stop prevented from finishing (assumption A12) -- but "
        f"results.json carries {outcomes}"
    )

    html = _read(project, _REPORT_NAME)
    assert html.strip(), f"{_ROW} requires the report written, but {_REPORT_NAME} is empty"
    assert f"{_MODULE_NAME}::{_ALPHA}" in html, (
        f"{_ROW} renders the flushed record, but {_REPORT_NAME} never names {_ALPHA}"
    )

    root = ElementTree.parse(project / _JUNIT_NAME).getroot()
    assert root.tag == "testsuite", (
        f"{_ROW} writes the JUnit document of AAP 0.4.6, but {_JUNIT_NAME} parses to <{root.tag}>"
    )
    names = [case.get("name") or "" for case in root.iter("testcase")]
    assert names == [_ALPHA, _BETA, _GAMMA], (
        f"{_ROW} writes one <testcase> per flushed record, but {_JUNIT_NAME} carries {names}"
    )
