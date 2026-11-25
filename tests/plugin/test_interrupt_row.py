"""AAP 0.4.7's ``KeyboardInterrupt`` row through a real pytest child: partial results flushed, the
report written, and pytest's own code 2 preserved (AAP 0.4.4). A child is required -- an in-process
session that raised it would abort this suite -- and every claim is read from the files it wrote."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from xml.etree import ElementTree

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable

    import pytest

# No enumerated fixture, subcommand, data loader or report format is driven here: this module
# observes a state-table row, and the empty tuple is the declaration test_reachability parses.
REACHES: tuple[str, ...] = ()

_ROW: Final[str] = "AAP 0.4.7's KeyboardInterrupt row"
_MODULE_NAME: Final[str] = "test_interrupt.py"
_ALPHA: Final[str] = "test_alpha"
_BETA: Final[str] = "test_beta"
_GAMMA: Final[str] = "test_gamma"

_RESULT_SOURCE: Final[str] = f"""def {_ALPHA}() -> None:
    print("alpha ran")


def {_BETA}() -> None:
    raise KeyboardInterrupt


def {_GAMMA}() -> None:
    print("gamma ran")
"""

# One record per collected item: the pass earned before the interrupt, and the two it stopped.
_EXPECTED_OUTCOMES: Final[dict[str, str]] = {
    f"{_MODULE_NAME}::{_ALPHA}": "passed",
    f"{_MODULE_NAME}::{_BETA}": "not_run",
    f"{_MODULE_NAME}::{_GAMMA}": "not_run",
}


def _read(project: Path, name: str) -> str:
    path = project / name
    listing = sorted(entry.name for entry in project.iterdir())
    assert path.is_file(), f"{_ROW} requires {name} written, but {project} holds {listing}"
    return path.read_text(encoding="utf-8")


def _result_records(project: Path) -> list[dict[str, Any]]:
    document = json.loads(_read(project, "results.json"))
    assert isinstance(document, list), f"{_ROW} requires AAP 0.4.6's array: {document!r}"
    records: list[dict[str, Any]] = []
    for element in document:
        assert isinstance(element, dict), f"{_ROW} requires AAP 0.4.6 elements: {element!r}"
        # A serial interrupt loses no worker, so no run_error element belongs in this document.
        assert element.get("record_type") == "result", f"{_ROW} flushed {element!r}"
        records.append(element)
    return records


def test_interrupt_flushes_partial_results_and_writes_the_report(
    tmp_path_factory: pytest.TempPathFactory,
    run_pytest: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    project = tmp_path_factory.mktemp("interrupt")
    (project / _MODULE_NAME).write_text(_RESULT_SOURCE, encoding="utf-8", newline="\n")
    child = run_pytest("-q", cwd=project)
    output = child.stdout + child.stderr
    assert child.returncode == 2, f"{_ROW} needs code 2, got {child.returncode}\n{output}"
    outcomes = {str(r["nodeid"]): str(r["outcome"]) for r in _result_records(project)}
    assert outcomes == _EXPECTED_OUTCOMES, f"{_ROW} flushes a pass and two not_run: {outcomes}"
    html = _read(project, "report.html")
    assert html.strip(), f"{_ROW} requires report.html written, but it is empty"
    assert f"{_MODULE_NAME}::{_ALPHA}" in html, f"report.html never names {_ALPHA}"
    root = ElementTree.parse(project / "junit.xml").getroot()
    assert root.tag == "testsuite", f"{_ROW} writes a JUnit <testsuite>, not <{root.tag}>"
    names = [case.get("name") or "" for case in root.iter("testcase")]
    assert names == [_ALPHA, _BETA, _GAMMA], f"one testcase per flushed record, got {names}"
