"""End-to-end proof that ``-n 3`` runs every example test exactly once, faster than serially.

Acceptance criterion 7 and AAP 0.14.2: the sharded median wall clock is at most 60 % of the serial
median over the same node-id set, from three alternating runs per side. The children run in a
module-scoped fixture because ``execution.timeout`` (60 s) covers the call phase only.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Final, NamedTuple

import pytest

EXAMPLE_DIR: Final[str] = "examples/checkout"
EXPECTED_TOTAL: Final[int] = 24
WORKER_COUNT: Final[int] = 3
RATIO_BUDGET: Final[float] = 0.60
SERIAL_FLOOR_MS: Final[float] = 3_000.0
RUN_PAIRS: Final[int] = 3
REPORT_FILES: Final[tuple[str, ...]] = ("results.json", "junit.xml", "report.html")
RESULTS, ARTIFACTS = "results.json", "artifacts"
RESULT, RUN_ERROR = "result", "run_error"
# The crash project: four plain tests, the second killing the process running it, one xdist worker.
CRASH_MODULE: Final[str] = "test_worker_crash.py"
CRASH_PROJECT: Final[str] = (
    "import os\nimport signal\n\n\n"
    "def test_alpha() -> None:\n    pass\n\n\n"
    "def test_beta() -> None:\n    os.kill(os.getpid(), signal.SIGKILL)\n\n\n"
    "def test_gamma() -> None:\n    pass\n\n\n"
    "def test_delta() -> None:\n    pass\n"
)


class _Run(NamedTuple):
    label: str
    run_dir: Path
    elapsed_ms: float
    result: Any
    records: tuple[dict[str, Any], ...]

    @property
    def output(self) -> str:
        return f"{self.result.stdout}\n{self.result.stderr}"


def _records(run_dir: Path) -> tuple[dict[str, Any], ...]:
    path = run_dir / RESULTS
    if not path.is_file():
        return ()
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        pytest.fail(f"{path} is not the JSON document AAP 0.4.6 specifies: {exc}")
    if not isinstance(document, list) or not all(isinstance(item, dict) for item in document):
        pytest.fail(f"{path} holds {type(document).__name__}, not an array of result elements")
    return tuple(document)


def _run(
    run_pytest: Callable[..., Any], repo_root: Path, run_dir: Path, workers: int | None
) -> _Run:
    run_dir.mkdir(parents=True, exist_ok=True)
    arguments = [EXAMPLE_DIR]
    if workers is not None:
        arguments += ["-n", str(workers)]
    arguments += ["-q", "--foundry-json-report", str(run_dir / RESULTS)]
    arguments += ["--foundry-artifacts.dir", str(run_dir / ARTIFACTS)]
    started = time.perf_counter()
    result = run_pytest(*arguments, cwd=repo_root)
    elapsed_ms = (time.perf_counter() - started) * 1_000.0
    return _Run(run_dir.name, run_dir, elapsed_ms, result, _records(run_dir))


def _nodeids(records: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    names = [
        str(record.get("nodeid", "")) for record in records if record.get("record_type") == RESULT
    ]
    return tuple(sorted(names))


def _run_id(run: _Run) -> str:
    results = [record for record in run.records if record.get("record_type") == RESULT]
    ids = {str(record.get("run_id", "")) for record in results}
    assert len(ids) == 1, f"run {run.label} stamped {len(ids)} run ids: {sorted(ids)}"
    return ids.pop()


def _timings(runs: Sequence[_Run]) -> str:
    return ", ".join(f"{run.label}={run.elapsed_ms:.0f} ms" for run in runs)


@pytest.fixture(scope="module")
def sharding_runs(
    repo_root: Path,
    example_project: Path,
    run_pytest: Callable[..., Any],
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[_Run, ...]:
    assert (repo_root / EXAMPLE_DIR).resolve() == example_project.resolve(), f"{EXAMPLE_DIR} moved"
    base = tmp_path_factory.mktemp("sharding")
    runs: list[_Run] = []
    for index in range(RUN_PAIRS):
        # Alternating within each pair, so a load spike that hits one side hits the other too.
        runs.append(_run(run_pytest, repo_root, base / f"serial-{index}", None))
        runs.append(_run(run_pytest, repo_root, base / f"sharded-{index}", WORKER_COUNT))
    return tuple(runs)


def test_sharded_run_covers_the_same_tests_in_at_most_sixty_percent_of_the_time(
    repo_root: Path, sharding_runs: tuple[_Run, ...]
) -> None:
    for run in sharding_runs:
        assert run.result.returncode == 0, f"run {run.label} exited {run.result.returncode}"
        nodeids = _nodeids(run.records)
        assert len(nodeids) == EXPECTED_TOTAL, f"run {run.label} wrote {len(nodeids)} record(s)"
        errors = [item for item in run.records if item.get("record_type") == RUN_ERROR]
        assert errors == [], f"run {run.label} declared the run itself broken: {errors}"
        duplicates = sorted({nodeid for nodeid in nodeids if nodeids.count(nodeid) > 1})
        assert duplicates == [], f"run {run.label} repeated {duplicates}"
    for index, first in enumerate(sharding_runs):
        for second in sharding_runs[index + 1 :]:
            left, right = _nodeids(first.records), _nodeids(second.records)
            assert left == right, (
                f"runs {first.label} and {second.label} report different node ids: "
                f"{sorted(set(left) ^ set(right))}"
            )
    serial, sharded = sharding_runs[0::2], sharding_runs[1::2]
    serial_median = statistics.median([run.elapsed_ms for run in serial])
    sharded_median = statistics.median([run.elapsed_ms for run in sharded])
    assert sharded_median <= RATIO_BUDGET * serial_median, (
        f"the median under -n {WORKER_COUNT} was {sharded_median:.0f} ms against "
        f"{serial_median:.0f} ms serial: a ratio of {sharded_median / serial_median:.2f}, where "
        f"AAP 0.14.2 budgets {RATIO_BUDGET:.2f}. Serial: {_timings(serial)}. "
        f"Sharded: {_timings(sharded)}"
    )
    assert serial_median >= SERIAL_FLOOR_MS, (
        f"the serial median was {serial_median:.0f} ms, under the {SERIAL_FLOOR_MS:.0f} ms floor "
        f"that keeps the ratio above meaningful: {_timings(serial)}"
    )
    run_ids = [_run_id(run) for run in sharding_runs]
    assert len(set(run_ids)) == len(sharding_runs), f"repeated run ids: {sorted(run_ids)}"
    for run, run_id in zip(sharding_runs, run_ids, strict=True):
        for name in REPORT_FILES:
            assert (run.run_dir / name).is_file(), f"run {run.label} wrote no {name}"
        assert (run.run_dir / ARTIFACTS / run_id).is_dir(), f"run {run.label} captured nothing"
        assert not (repo_root / ARTIFACTS / run_id).exists(), f"{run.label} hit the repo root"


def test_a_killed_worker_leaves_a_run_error_and_a_not_run_record(
    run_pytest: Callable[..., Any], tmp_path_factory: pytest.TempPathFactory
) -> None:
    # AAP 0.4.7's worker-crash row and assumption A12: partial results, one run_error, not_run.
    project = tmp_path_factory.mktemp("worker-crash")
    (project / CRASH_MODULE).write_text(CRASH_PROJECT, encoding="utf-8")
    result = run_pytest("-q", "-n", "2", cwd=project)
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 1, f"the killed run exited {result.returncode}, not AAP 0.4.7's 1"
    records = _records(project)
    errors = [item for item in records if item.get("record_type") == RUN_ERROR]
    assert len(errors) == 1, f"the killed run wrote {len(errors)} run_error element(s)\n{output}"
    worker = str(errors[0].get("worker_id", ""))
    assert worker.startswith("gw"), f"run_error names worker_id {worker!r}, not xdist's gw<N>"
    results = [item for item in records if item.get("record_type") == RESULT]
    outcomes = {str(item.get("nodeid", "")): str(item.get("outcome", "")) for item in results}
    killed = f"{CRASH_MODULE}::test_beta"
    assert outcomes.get(killed) == "not_run", f"{killed} is {outcomes.get(killed)!r}\n{output}"
    survivors = {f"{CRASH_MODULE}::test_{name}": "passed" for name in ("alpha", "gamma", "delta")}
    assert {nodeid: outcomes.get(nodeid) for nodeid in survivors} == survivors
    nodeids = _nodeids(records)
    assert len(nodeids) == len(set(nodeids)), f"the killed run repeated a test: {nodeids}\n{output}"
