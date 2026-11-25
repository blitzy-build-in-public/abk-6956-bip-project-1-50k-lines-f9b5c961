"""End-to-end proof that ``-n 3`` runs every example test exactly once, faster than serially.

Acceptance criterion 7 (AAP 0.14.1, "Sharding consistency") and the sharded budget of AAP 0.14.2:
``pytest examples/checkout`` under ``-n 3`` must report the same node-id set as the serial run --
set equality, no duplicates, no omissions -- and its median wall clock must be at most 60 % of the
serial median. The property observed is node-id stability across workers: xdist workers collect
independently and the controller aborts the run when two collections differ, so a case id or a
collection order that depended on the worker, the host, the directory-listing order or the clock
would surface here rather than in a user's CI (AAP 0.2.1, implicit requirement I6).

Three serial and three sharded runs are made strictly alternately -- serial-0, sharded-0, serial-1,
sharded-1, serial-2, sharded-2 -- so a load spike that reaches one side also reaches the other.
Nothing is normalised away: the run ids are asserted to be six distinct values, and the node ids
are compared as sorted tuples that must be equal for every pair. Only ``returncode``, the node-id
set and the wall clock are read, never ``duration_ms``, ``started_at`` or ``finished_at``.

The wall clock that makes the ratio meaningful comes from ``examples/checkout/_work.py``, the
CPU-bound work that supplies AAP 0.14.2's 12 s across the 24 tests; this module adds no sleep, no
busy loop and no artificial delay, because a manufactured timing would measure its own delay instead
of the sharding. The children run in the module-scoped fixture rather than in the test body, because
``execution.timeout`` -- 60 s, enforced over the call phase only -- would otherwise turn a correct
measurement on a busy host into a spurious timeout; each child is still bounded by ``run_pytest``'s
own per-process timeout and every assertion stays in the single test. Each run redirects its outputs
into its own temporary directory (``--foundry-json-report`` takes ``results.json`` with
``junit.xml`` and ``report.html`` beside it, ``--foundry-artifacts.dir`` the artifact tree), so the
repository root stays untouched -- an assertion the test makes -- and the root-level invocation
whose outputs land there belongs to ``tests/e2e/test_example_project.py`` alone.
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
"""The example project, relative to the repository root every child runs in."""

EXPECTED_TOTAL: Final[int] = 24
"""The tests the example project collects, one result record each (AAP 0.8.1, criterion 7)."""

WORKER_COUNT: Final[int] = 3
"""The ``-n`` the criterion names; the serial side of every pair passes no ``-n`` at all."""

RATIO_BUDGET: Final[float] = 0.60
"""AAP 0.14.2's ceiling on the sharded median as a fraction of the serial median."""

SERIAL_FLOOR_MS: Final[float] = 3_000.0
"""Sanity floor on the serial median, well under the nominal 12 s of aggregate example work."""

RUN_PAIRS: Final[int] = 3
"""Serial and sharded runs per side; the median of three is the statistic the criterion compares."""

REPORT_FILES: Final[tuple[str, ...]] = ("results.json", "junit.xml", "report.html")
"""The three fixed report names AAP 0.4.6 writes, the last two beside the first (assumption A5)."""

# The two element discriminators AAP 0.4.6 gives results.json: a per-test record, and a failure of
# the run itself -- a lost worker or a session stopped early.
_RESULT: Final[str] = "result"
_RUN_ERROR: Final[str] = "run_error"


class _Measurement(NamedTuple):
    """One child run: its label, output directory, wall clock, completed process and results."""

    label: str
    run_dir: Path
    elapsed_ms: float
    result: Any
    records: tuple[dict[str, Any], ...]

    @property
    def output(self) -> str:
        """Return the child's stdout and stderr together, for an assertion message."""
        return f"{self.result.stdout}\n{self.result.stderr}"


class _Measurements(NamedTuple):
    """The six runs, kept apart by kind so the pairing and the medians stay visible."""

    serial: tuple[_Measurement, ...]
    sharded: tuple[_Measurement, ...]

    @property
    def all(self) -> tuple[_Measurement, ...]:
        """Return every run, the serial triple followed by the sharded triple."""
        return (*self.serial, *self.sharded)


def _records(run_dir: Path) -> tuple[dict[str, Any], ...]:
    """Return the elements of ``run_dir/results.json``, or nothing when the run wrote none.

    An absent document yields an empty tuple so the next assertion can report it with the child's
    own output; anything but the JSON array of objects AAP 0.4.6 specifies fails naming the file.
    """
    path = run_dir / "results.json"
    if not path.is_file():
        return ()
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        pytest.fail(f"{path} is not the JSON document AAP 0.4.6 specifies: {exc}")
    if not isinstance(document, list) or not all(isinstance(entry, dict) for entry in document):
        pytest.fail(
            f"{path} holds {type(document).__name__} rather than an array of result elements: "
            f"AAP 0.4.6 discriminates every element by record_type. Document: {document!r}"
        )
    return tuple(document)


def _run(
    run_pytest: Callable[..., Any], repo_root: Path, run_dir: Path, workers: int | None
) -> _Measurement:
    """Run the example project once from the repository root and time the whole child process.

    The argument tuple is assembled here and only here, so every run is provably identical apart
    from ``-n <workers>``: same target, same two output redirections, same ``-q`` for the captured
    output an assertion quotes. ``time.perf_counter`` times the subprocess alone.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    arguments = [EXAMPLE_DIR]
    if workers is not None:
        arguments += ["-n", str(workers)]
    arguments += [
        "-q",
        "--foundry-json-report",
        str(run_dir / "results.json"),
        "--foundry-artifacts.dir",
        str(run_dir / "artifacts"),
    ]
    started = time.perf_counter()
    result = run_pytest(*arguments, cwd=repo_root)
    elapsed_ms = (time.perf_counter() - started) * 1_000.0
    return _Measurement(
        label=run_dir.name,
        run_dir=run_dir,
        elapsed_ms=elapsed_ms,
        result=result,
        records=_records(run_dir),
    )


def _nodeids(records: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """Return the sorted node ids of ``records``' ``record_type == "result"`` elements."""
    return tuple(
        sorted(
            str(record.get("nodeid", ""))
            for record in records
            if record.get("record_type") == _RESULT
        )
    )


def _run_errors(records: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Return the ``record_type == "run_error"`` elements, which no healthy run produces."""
    return tuple(record for record in records if record.get("record_type") == _RUN_ERROR)


def _run_id(measurement: _Measurement) -> str:
    """Return the one run id ``measurement``'s result records carry, naming its artifacts tree."""
    run_ids = {
        str(record.get("run_id", ""))
        for record in measurement.records
        if record.get("record_type") == _RESULT
    }
    assert len(run_ids) == 1, (
        f"run {measurement.label} stamped its result records with {len(run_ids)} distinct run ids "
        f"{sorted(run_ids)}: a run has exactly one, which is also its artifacts/<run_id>/ "
        "directory name (AAP 0.4.6)"
    )
    return run_ids.pop()


def _timings(measurements: Sequence[_Measurement]) -> str:
    """Return each run's label with its wall clock in milliseconds, joined for a message."""
    return ", ".join(
        f"{measurement.label}={measurement.elapsed_ms:.0f} ms" for measurement in measurements
    )


@pytest.fixture(scope="module")
def sharding_runs(
    repo_root: Path,
    example_project: Path,
    run_pytest: Callable[..., Any],
    tmp_path_factory: pytest.TempPathFactory,
) -> _Measurements:
    """Make the three serial and three sharded runs, alternating, and return them.

    Setup, not the call phase, because ``execution.timeout`` (60 s) covers the call phase only: six
    children take about a minute of honest work, and a busy host may push a correct run past it.
    """
    assert (repo_root / EXAMPLE_DIR).resolve() == example_project.resolve(), (
        f"the runs below pass {EXAMPLE_DIR!r} relative to {repo_root}, which resolves to "
        f"{(repo_root / EXAMPLE_DIR).resolve()} rather than to the shipped example project "
        f"{example_project} the session fixture names"
    )
    base = tmp_path_factory.mktemp("sharding")
    serial: list[_Measurement] = []
    sharded: list[_Measurement] = []
    for index in range(RUN_PAIRS):
        # Alternating within each pair, so a load spike that hits one side hits the other too.
        serial.append(_run(run_pytest, repo_root, base / f"serial-{index}", None))
        sharded.append(_run(run_pytest, repo_root, base / f"sharded-{index}", WORKER_COUNT))
    return _Measurements(serial=tuple(serial), sharded=tuple(sharded))


def test_sharded_run_covers_the_same_tests_in_at_most_sixty_percent_of_the_time(
    repo_root: Path, sharding_runs: _Measurements
) -> None:
    """Three serial and three ``-n 3`` runs agree on every node id and beat the time budget."""
    for measurement in sharding_runs.all:
        assert measurement.result.returncode == 0, (
            f"run {measurement.label} exited {measurement.result.returncode} after "
            f"{measurement.elapsed_ms:.0f} ms; a green example suite exits 0 both serially and "
            f"under -n {WORKER_COUNT} (AAP 0.14.1 criterion 7). A controller that aborted on a "
            "collection mismatch reports it in this output, and that message is the diagnostic for "
            f"an unstable case id:\n{measurement.output}"
        )
        nodeids = _nodeids(measurement.records)
        assert len(nodeids) == EXPECTED_TOTAL, (
            f"run {measurement.label} wrote {len(nodeids)} result record(s) for the "
            f"{EXPECTED_TOTAL} tests examples/checkout collects: AAP 0.4.6 fixes one result record "
            f"per collected item, so a lost worker, a skipped module or an uncollected case shows "
            f"up here. Document: {measurement.run_dir / 'results.json'}\n{measurement.output}"
        )
        run_errors = _run_errors(measurement.records)
        assert run_errors == (), (
            f"run {measurement.label} declared the run itself broken with {len(run_errors)} "
            f"run_error element(s): {list(run_errors)}. That element records a lost worker or a "
            f"session stopped early, neither of which a green sharded run may produce "
            f"(AAP 0.4.7)\n{measurement.output}"
        )
        duplicates = sorted({nodeid for nodeid in nodeids if nodeids.count(nodeid) > 1})
        assert duplicates == [], (
            f"run {measurement.label} executed {len(duplicates)} test(s) more than once: "
            f"{duplicates}. Sharding distributes the collected items and never repeats one, so a "
            f"duplicate is a scheduling defect rather than extra coverage\n{measurement.output}"
        )

    every_run = sharding_runs.all
    for first_index, first in enumerate(every_run):
        for second in every_run[first_index + 1 :]:
            first_nodes = _nodeids(first.records)
            second_nodes = _nodeids(second.records)
            if first_nodes == second_nodes:
                continue
            only_first = sorted(set(first_nodes) - set(second_nodes))
            only_second = sorted(set(second_nodes) - set(first_nodes))
            pytest.fail(
                f"runs {first.label} and {second.label} report different node ids, so the "
                "collected set is not worker-independent (criterion 7, implicit requirement I6). "
                f"{first.label} ran {len(first_nodes)} record(s) and {second.label} ran "
                f"{len(second_nodes)}. Only in {first.label}: {only_first}. Only in "
                f"{second.label}: {only_second}. An id one run has and the other lacks is an "
                "omission by the run that lacks it -- a test that never executed -- not a "
                "reordering, because sorted tuples are compared, and a duplicate inside one run "
                "was ruled out above. A serial run against a sharded one isolates this to "
                f"sharding.\n{first.output}\n{second.output}"
            )

    serial_median = statistics.median(
        [measurement.elapsed_ms for measurement in sharding_runs.serial]
    )
    sharded_median = statistics.median(
        [measurement.elapsed_ms for measurement in sharding_runs.sharded]
    )
    ratio = sharded_median / serial_median
    assert sharded_median <= RATIO_BUDGET * serial_median, (
        f"the median wall clock under -n {WORKER_COUNT} was {sharded_median:.0f} ms against "
        f"{serial_median:.0f} ms serial: a ratio of {ratio:.2f}, where AAP 0.14.2 budgets at most "
        f"{RATIO_BUDGET:.2f}. Serial runs: {_timings(sharding_runs.serial)}. Sharded runs: "
        f"{_timings(sharding_runs.sharded)}. The wall clock this ratio needs is the deterministic "
        "CPU-bound work in examples/checkout/_work.py, 12 s of it across the 24 tests; a suite "
        "that spends less, or a host with fewer usable cores than the "
        f"{WORKER_COUNT} workers asked for, moves the ratio without changing whether it works."
    )

    # A suite that does no work satisfies any ratio -- 5 ms against 8 ms -- so this floor keeps the
    # comparison meaningful; 3 s of the nominal 12 s is generous enough that no honest run fails it.
    assert serial_median >= SERIAL_FLOOR_MS, (
        f"the serial median was {serial_median:.0f} ms, below the {SERIAL_FLOOR_MS:.0f} ms floor "
        f"that keeps the ratio above meaningful. That wall clock is supposed to come from "
        f"examples/checkout/_work.py, which spends 12 s across the {EXPECTED_TOTAL} example tests "
        f"(AAP 0.14.2); a suite that does less makes the ratio above trivial and proves nothing "
        f"about sharding. Serial runs: {_timings(sharding_runs.serial)}"
    )

    run_ids = [_run_id(measurement) for measurement in every_run]
    assert len(set(run_ids)) == len(every_run), (
        f"the {len(every_run)} runs produced only {len(set(run_ids))} distinct run ids "
        f"{sorted(run_ids)}: every run owns its artifacts/<run_id>/ directory, so a repeated id "
        "would mean two runs shared one artifact tree (AAP 0.4.6)"
    )
    for measurement, run_id in zip(every_run, run_ids, strict=True):
        for name in REPORT_FILES:
            report = measurement.run_dir / name
            assert report.is_file(), (
                f"run {measurement.label} (run id {run_id}) wrote no {name} in "
                f"{measurement.run_dir}: the three fixed report names belong together, and "
                "--foundry-json-report moves results.json with junit.xml and report.html written "
                f"beside it (AAP 0.4.6, assumption A5)\n{measurement.output}"
            )
        artifacts_root = measurement.run_dir / "artifacts" / run_id
        assert artifacts_root.is_dir(), (
            f"run {measurement.label} (run id {run_id}) captured nothing under {artifacts_root}: "
            "--foundry-artifacts.dir did not take effect, so this run wrote its artifact tree "
            "outside its own output directory instead of inside it (AAP 0.4.6, assumption A5)"
        )
        stray = repo_root / "artifacts" / run_id
        assert not stray.exists(), (
            f"run {measurement.label} (run id {run_id}) also captured under {stray}. The two "
            "redirections exist so this module leaves the repository root untouched, and a run "
            "present in both trees wrote its artifacts twice (AAP 0.4.6, assumption A5)"
        )
