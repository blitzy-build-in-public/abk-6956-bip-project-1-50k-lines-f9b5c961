"""End-to-end proof that AAP 0.4.6's ninth outcome value is produced by real runs.

``not_run`` names a collected item that never executed because a worker was lost or the session
stopped (assumption A12), so only a real pytest process can produce one: the record comes from
comparing the controller's own collection with the records that arrived, and an xdist controller
collects nothing itself (AAP 0.7.1). Two scenarios reach the outcome and neither substitutes for the
other, so each is its own test rather than one parameterised test:

* a worker that kills its own process under ``-n 2`` -- the lost-worker route, where the dead
  worker's item is collected, never reported, and named ``not_run`` beside the ``run_error``
  element that AAP 0.4.6 places after every result record;
* a serial session whose second test calls ``pytest.exit`` -- the stopped-session route, where all
  three items were collected, one ran, and no worker was ever lost.

Both tests assert the whole document rather than the ``not_run`` record alone, because AAP 0.7.2
fixes one result record per collected item: a wrong count is as much a defect as a missing outcome.
The child's own stdout and stderr ride in every assertion message, since a run that wrote no file at
all has nothing else to report. Nothing sleeps, no socket is opened and every file is written under
``tmp_path``.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable, Sequence
from typing import Any, Final

RESULT: Final[str] = "result"
"""The ``record_type`` of a per-test element (AAP 0.4.6)."""

RUN_ERROR: Final[str] = "run_error"
"""The ``record_type`` of an element describing a failure of the run itself (AAP 0.4.6)."""

NOT_RUN: Final[str] = "not_run"
"""The ninth outcome value: a collected item that never executed (AAP 0.4.6, assumption A12)."""

NOT_RUN_MESSAGE: Final[str] = "foundry: not run"
"""The message the JUnit writer gives a ``not_run`` record's ``<skipped>`` element (AAP 0.4.6)."""

CHILD_TIMEOUT_S: Final[float] = 120.0
"""Ceiling on one child run: two workers and three tests, so a hang is a fault and not slowness."""

WORKER_LOSS_CASES: Final[int] = 6
"""The cases the lost-worker project collects, exactly one of which kills its worker."""

CRASHED_INDEX: Final[int] = 3
"""The case index that kills the worker running it, so its item is the one never reported."""

CRASHED_NODEID: Final[str] = f"test_crash.py::test_item[{CRASHED_INDEX}]"
"""The node id of the lost item: collected, never reported, and therefore named ``not_run``."""

STOPPED_OUTCOMES: Final[tuple[tuple[str, str], ...]] = (
    ("test_stop.py::test_a", "passed"),
    ("test_stop.py::test_b", NOT_RUN),
    ("test_stop.py::test_c", NOT_RUN),
)
"""The stopped run's three collected items in node-id order, with the outcome each must carry."""

_CRASH_MODULE: Final[str] = f'''"""Six parametrised cases; one kills the worker that runs it."""

import os
import signal

import pytest


@pytest.mark.parametrize("index", range({WORKER_LOSS_CASES}))
def test_item(index: int) -> None:
    """Kill this process on case {CRASHED_INDEX}, and only under a worker, so the item is lost."""
    if index == {CRASHED_INDEX} and os.environ.get("PYTEST_XDIST_WORKER"):
        os.kill(os.getpid(), signal.SIGKILL)
'''
"""The whole lost-worker project: a case that ends its worker before the case can be reported."""

_STOP_MODULE: Final[str] = '''"""Three tests; the second stops the session."""

import pytest


def test_a() -> None:
    """Pass, so a real result record sits beside the two the session never reached."""
    assert True


def test_b() -> None:
    """Stop the session here, leaving every item after this one unexecuted."""
    pytest.exit("stop here", returncode=1)


def test_c() -> None:
    """Collected before test_b stopped the session, and never executed."""
    assert True
'''
"""The whole stopped-session project: one result record and two items the session never reached."""


def _write_project(project: pathlib.Path, name: str, source: str) -> pathlib.Path:
    """Write ``source`` as ``project/name`` and return the project directory.

    The project holds that one module and nothing else, so the run resolves no configuration file
    and every outcome it produces comes from the plugin that activation installed (AAP 0.4.2).
    """
    project.mkdir(parents=True, exist_ok=True)
    (project / name).write_text(source, encoding="utf-8", newline="\n")
    return project


def _run_project(
    run_pytest: Callable[..., Any],
    project: pathlib.Path,
    out: pathlib.Path,
    *arguments: str,
) -> Any:
    """Run ``project`` once, with its three report files redirected into ``out``.

    ``--foundry-json-report`` takes ``results.json`` and ``junit.xml`` and ``report.html`` are
    written beside it (assumption A5), so the project directory keeps only the module written above.
    The completed process is returned untyped for the same reason as in the sharding suite: the
    child's process belongs to the shared fixture, and only its ``returncode`` and output are read.
    """
    out.mkdir(parents=True, exist_ok=True)
    return run_pytest(
        *arguments,
        "-q",
        "--foundry-json-report",
        str(out / "results.json"),
        "--foundry-artifacts.dir",
        str(out / "artifacts"),
        cwd=project,
        timeout=CHILD_TIMEOUT_S,
    )


def _output(result: Any) -> str:
    """Return the child's stdout and stderr together, quoted in every assertion message."""
    return f"\n--- child stdout ---\n{result.stdout}\n--- child stderr ---\n{result.stderr}"


def _records(out: pathlib.Path) -> list[dict[str, Any]]:
    """Return the elements of ``out/results.json``, failing the test when it is absent.

    An absent or malformed document is reported with the path it should hold, because the case that
    produced it -- a lost worker, a stopped session -- also produced no other evidence.
    """
    path = out / "results.json"
    assert path.is_file(), (
        f"the run wrote no {path}: AAP 0.4.6 writes the result document on the controller and "
        "--foundry-json-report moved it here (assumption A5)"
    )
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), (
        f"{path} holds a JSON {type(document).__name__} rather than AAP 0.4.6's array of elements"
    )
    elements = [element for element in document if isinstance(element, dict)]
    assert len(elements) == len(document), (
        f"{path} holds {len(document) - len(elements)} element(s) that are not JSON objects with a "
        f"record_type: AAP 0.4.6 discriminates every element by that field. Document: {document!r}"
    )
    return elements


def _of(records: Sequence[dict[str, Any]], record_type: str) -> tuple[dict[str, Any], ...]:
    """Return the elements whose ``record_type`` is ``record_type``, in document order."""
    return tuple(record for record in records if record.get("record_type") == record_type)


def _positions(records: Sequence[dict[str, Any]], record_type: str) -> tuple[int, ...]:
    """Return the document positions of the elements of ``record_type``, in document order."""
    return tuple(
        index for index, record in enumerate(records) if record.get("record_type") == record_type
    )


def test_a_lost_worker_leaves_its_item_not_run(
    tmp_path: pathlib.Path, run_pytest: Callable[..., Any]
) -> None:
    """``-n 2`` with a worker that kills itself writes one ``not_run`` record for its item."""
    project = _write_project(tmp_path / "worker_loss", "test_crash.py", _CRASH_MODULE)
    out = tmp_path / "worker_loss_out"
    result = _run_project(run_pytest, project, out, "-n", "2")
    output = _output(result)
    assert result.returncode == 1, (
        f"the worker-loss run exited {result.returncode} rather than 1: a lost xdist worker fails "
        f"the run (AAP 0.4.7){output}"
    )
    records = _records(out)
    results = _of(records, RESULT)
    errors = _of(records, RUN_ERROR)
    assert len(results) == WORKER_LOSS_CASES, (
        f"the document holds {len(results)} result record(s) for the {WORKER_LOSS_CASES} collected "
        "items: AAP 0.7.2 fixes one per collected item, so an item with no record was dropped "
        f"instead of being named {NOT_RUN} (assumption A12){output}"
    )
    nodeids = [str(record.get("nodeid", "")) for record in results]
    assert nodeids == sorted(nodeids), (
        f"the result records appear as {nodeids}: AAP 0.4.6 sorts them by node id{output}"
    )
    not_run = [record for record in results if record.get("outcome") == NOT_RUN]
    assert [record.get("nodeid") for record in not_run] == [CRASHED_NODEID], (
        f"the {NOT_RUN} records name {[record.get('nodeid') for record in not_run]} rather than "
        f"exactly [{CRASHED_NODEID!r}]: the worker that killed itself was running that item, and "
        f"every other collected item did run{output}"
    )
    assert errors, (
        "the document holds no run_error element: a worker that died with no report is a failure "
        f"of the run itself, recorded beside the partial results (AAP 0.4.6, 0.4.7){output}"
    )
    last_result = _positions(records, RESULT)[-1]
    first_error = _positions(records, RUN_ERROR)[0]
    assert last_result < first_error, (
        f"a run_error element sits at position {first_error}, before a result record at position "
        f"{last_result}: AAP 0.4.6 lists every run_error after every result record{output}"
    )
    junit = (out / "junit.xml").read_text(encoding="utf-8")
    assert f'tests="{WORKER_LOSS_CASES}"' in junit, (
        f'junit.xml does not report tests="{WORKER_LOSS_CASES}" for the {WORKER_LOSS_CASES} '
        f"collected items: one <testcase> per result record is what AAP 0.4.6 writes{output}"
    )
    skipped = junit.count(f'message="{NOT_RUN_MESSAGE}"')
    assert skipped == 1, (
        f"junit.xml carries {skipped} {NOT_RUN_MESSAGE!r} skipped element(s) rather than one, for "
        f"the single lost item {CRASHED_NODEID}{output}"
    )
    html = (out / "report.html").read_text(encoding="utf-8")
    assert NOT_RUN in html, (
        f"report.html carries no {NOT_RUN} text: the outcome the run recorded never reached the "
        f"rendered report (AAP 0.4.6){output}"
    )


def test_a_stopped_serial_session_names_the_rest_not_run(
    tmp_path: pathlib.Path, run_pytest: Callable[..., Any]
) -> None:
    """A serial ``pytest.exit`` mid-session leaves every item it never reached ``not_run``."""
    project = _write_project(tmp_path / "stopped", "test_stop.py", _STOP_MODULE)
    out = tmp_path / "stopped_out"
    result = _run_project(run_pytest, project, out)
    output = _output(result)
    assert result.returncode == 1, (
        f"the stopped run exited {result.returncode} rather than 1: the test that stopped the "
        f"session named returncode=1{output}"
    )
    records = _records(out)
    results = _of(records, RESULT)
    assert len(results) == len(STOPPED_OUTCOMES), (
        f"the document holds {len(results)} result record(s) for the {len(STOPPED_OUTCOMES)} "
        "collected items: a stopped session still reports the items it collected (AAP 0.4.6, "
        f"assumption A12){output}"
    )
    observed = [(record.get("nodeid"), record.get("outcome")) for record in results]
    assert observed == list(STOPPED_OUTCOMES), (
        f"the records read {observed} rather than {list(STOPPED_OUTCOMES)}: the one test that ran "
        "passed, and the two the session never reached are named "
        f"{NOT_RUN} rather than omitted{output}"
    )
    assert _of(records, RUN_ERROR) == (), (
        "the document holds a run_error element: no worker was lost and no interrupt occurred, so "
        f"only the stopped session's own items are recorded (AAP 0.4.6){output}"
    )
    junit = (out / "junit.xml").read_text(encoding="utf-8")
    assert f'tests="{len(STOPPED_OUTCOMES)}"' in junit, (
        f'junit.xml does not report tests="{len(STOPPED_OUTCOMES)}" for the '
        f"{len(STOPPED_OUTCOMES)} collected items: one <testcase> per result record is what AAP "
        f"0.4.6 writes{output}"
    )
    skipped = junit.count(f'message="{NOT_RUN_MESSAGE}"')
    assert skipped == 2, (
        f"junit.xml carries {skipped} {NOT_RUN_MESSAGE!r} skipped element(s) rather than two, for "
        f"the two items the stopped session never reached{output}"
    )
