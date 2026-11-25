"""AAP 0.4.6's ninth outcome as real runs produce it: a worker that kills itself under ``-n 2``, and
a serial session stopped by ``pytest.exit``. Only a real process produces ``not_run`` (A12)."""

import json
import pathlib
from collections.abc import Callable, Sequence
from typing import Any, Final

WORKER_LOSS_CASES: Final[int] = 6
CRASHED_INDEX: Final[int] = 3
CRASHED_NODEID: Final[str] = f"test_crash.py::test_item[{CRASHED_INDEX}]"
NOT_RUN_MESSAGE: Final[str] = "foundry: not run"
CHILD_TIMEOUT_S: Final[float] = 120.0
STOPPED_OUTCOMES: Final[tuple[tuple[str, str], ...]] = (
    ("test_stop.py::test_a", "passed"),
    ("test_stop.py::test_b", "not_run"),
    ("test_stop.py::test_c", "not_run"),
)

# The kill only fires under xdist, so a serial collection of the same file still succeeds.
_CRASH_MODULE: Final[str] = f"""import os
import signal

import pytest


@pytest.mark.parametrize("index", range({WORKER_LOSS_CASES}))
def test_item(index: int) -> None:
    if index == {CRASHED_INDEX} and os.environ.get("PYTEST_XDIST_WORKER"):
        os.kill(os.getpid(), signal.SIGKILL)
"""

_STOP_MODULE: Final[str] = """import pytest
def test_a() -> None:
    assert True
def test_b() -> None:
    pytest.exit("stop here", returncode=1)
def test_c() -> None:
    assert True
"""


def _project(
    tmp_path: pathlib.Path, name: str, module: str, source: str
) -> tuple[pathlib.Path, pathlib.Path]:
    # The project holds that one module and nothing else, so the run resolves no configuration file
    # and every outcome it produces comes from the activation the entry point installed (AAP 0.4.2).
    project = tmp_path / name
    project.mkdir(parents=True, exist_ok=True)
    (project / module).write_text(source, encoding="utf-8", newline="\n")
    out = tmp_path / f"{name}_out"
    out.mkdir(parents=True, exist_ok=True)
    return project, out


def _run_project(
    run_pytest: Callable[..., Any], project: pathlib.Path, out: pathlib.Path, *arguments: str
) -> Any:
    # --foundry-json-report takes results.json, and junit.xml and report.html are written beside it
    # (assumption A5), so the project directory keeps only the module written above.
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
    return f"\n--- child stdout ---\n{result.stdout}\n--- child stderr ---\n{result.stderr}"


def _records(out: pathlib.Path) -> list[dict[str, Any]]:
    path = out / "results.json"
    assert path.is_file(), f"the run wrote no {path}"
    document: Any = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, list), f"{path} is a JSON {type(document).__name__}"
    elements = [element for element in document if isinstance(element, dict)]
    assert len(elements) == len(document), (
        f"{path} holds {len(document) - len(elements)} non-objects"
    )
    return elements


def _of(records: Sequence[dict[str, Any]], record_type: str) -> tuple[dict[str, Any], ...]:
    return tuple(record for record in records if record.get("record_type") == record_type)


def test_a_lost_worker_leaves_its_item_not_run(
    tmp_path: pathlib.Path, run_pytest: Callable[..., Any]
) -> None:
    project, out = _project(tmp_path, "worker_loss", "test_crash.py", _CRASH_MODULE)
    result = _run_project(run_pytest, project, out, "-n", "2")
    output = _output(result)
    assert result.returncode == 1, f"a lost worker fails the run (AAP 0.4.7){output}"
    records = _records(out)
    results = _of(records, "result")
    assert len(results) == WORKER_LOSS_CASES, (
        f"{len(results)} result records for {WORKER_LOSS_CASES} collected items{output}"
    )
    nodeids = [str(record.get("nodeid", "")) for record in results]
    assert nodeids == sorted(nodeids), f"the records are not sorted by node id: {nodeids}{output}"
    not_run = [record.get("nodeid") for record in results if record.get("outcome") == "not_run"]
    assert not_run == [CRASHED_NODEID], (
        f"the not_run records name {not_run} rather than [{CRASHED_NODEID!r}]{output}"
    )
    assert _of(records, "run_error"), f"no run_error beside the partial results{output}"
    kinds = [record.get("record_type") for record in records]
    assert "result" not in kinds[kinds.index("run_error") :], "a result record follows a run_error"
    junit = (out / "junit.xml").read_text(encoding="utf-8")
    assert f'tests="{WORKER_LOSS_CASES}"' in junit, f"junit.xml test count{output}"
    skipped = junit.count(f'message="{NOT_RUN_MESSAGE}"')
    assert skipped == 1, f"junit.xml carries {skipped} not_run skips for {CRASHED_NODEID}{output}"
    assert "not_run" in (out / "report.html").read_text(encoding="utf-8"), f"report.html{output}"


def test_a_stopped_serial_session_names_the_rest_not_run(
    tmp_path: pathlib.Path, run_pytest: Callable[..., Any]
) -> None:
    project, out = _project(tmp_path, "stopped", "test_stop.py", _STOP_MODULE)
    result = _run_project(run_pytest, project, out)
    output = _output(result)
    assert result.returncode == 1, f"the stopping test named returncode=1{output}"
    records = _records(out)
    results = _of(records, "result")
    assert len(results) == len(STOPPED_OUTCOMES), (
        f"{len(results)} result records for {len(STOPPED_OUTCOMES)} collected items{output}"
    )
    observed = [(record.get("nodeid"), record.get("outcome")) for record in results]
    assert observed == list(STOPPED_OUTCOMES), f"the records read {observed}{output}"
    assert _of(records, "run_error") == (), f"a run_error is present{output}"
    junit = (out / "junit.xml").read_text(encoding="utf-8")
    assert f'tests="{len(STOPPED_OUTCOMES)}"' in junit, f"junit.xml test count{output}"
    skipped = junit.count(f'message="{NOT_RUN_MESSAGE}"')
    assert skipped == 2, f"junit.xml carries {skipped} not_run skips rather than two{output}"
