"""Plugin startup: 150 ms median of 5 interleaved pairs, the budget AAP 0.14.2 sets.

Each sample is two ``--collect-only`` boots of a one-test project -- with the plugin loaded and with
it disabled -- and a starved pair is retaken, never excused. The budget is read from the wall clock:
the median ``elapsed_ms`` of the with-plugin arm minus the median of the without-plugin arm. The CPU
time of the same pairs carries a second assertion of the same ceiling, because only the wall clock
shows a regression that waits rather than computes and only the CPU time stays honest on a host that
deschedules a child; neither substitutes for the other.
"""

from __future__ import annotations

import resource
import statistics
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeAlias

from _host_contention import (
    QuotaThrottling,
    describe_quota_throttling,
    read_quota_throttling,
    was_preempted,
)

STARTUP_BUDGET_MS: Final[float] = 150.0
SAMPLE_COUNT: Final[int] = 5
PAIR_RETRY_BUDGET: Final[int] = 5
PREEMPTION_FLOOR_MS: Final[float] = 100.0
PROJECT_FILE_NAME: Final[str] = "test_one.py"
PROJECT_FILE_SOURCE: Final[str] = (
    '"""The one test the measured directory collects."""\n'
    "\n"
    "\n"
    "def test_one() -> None:\n"
    "    assert True\n"
)
COLLECTED_NODEID: Final[str] = "test_one.py::test_one"

_COLLECT_ARGS: Final[tuple[str, ...]] = ("--collect-only", "-q")
# The repository's one allowlisted spelling of "-p no:foundry" reaches only arm B, whose cost is
# subtracted from arm A; tests/unit/test_no_dash_p_flag.py fails the build on any other occurrence.
# Do not "clean it up".
_PLUGIN_DISABLED_ARGS: Final[tuple[str, ...]] = ("-p", "no:foundry")
_TRACE_CONFIG_ARGS: Final[tuple[str, ...]] = ("--collect-only", "--trace-config")
_PLUGIN_MODULE: Final[str] = "foundry.plugin"
_PLUGIN_REGISTRATION: Final[str] = f"PLUGIN registered: <module '{_PLUGIN_MODULE}'"
_PLUGIN_BANNER: Final[str] = "foundry: profile="
_HEAVY_LIBRARIES: Final[tuple[str, ...]] = ("sqlalchemy", "faker", "jsonschema", "rich", "jinja2")
_LOADED_PREFIX: Final[str] = "LOADED:"

_LAZY_IMPORT_SCRIPT: Final[str] = (
    "import sys\n"
    "import foundry.plugin\n"
    f"loaded = [name for name in {_HEAVY_LIBRARIES!r} if name in sys.modules]\n"
    f"print({_LOADED_PREFIX!r} + ','.join(loaded))\n"
)
_CHILD_TIMEOUT_SECONDS: Final[float] = 30.0

ArmName: TypeAlias = Literal["with-plugin", "without-plugin"]
RunPytest: TypeAlias = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class TimedRun:
    arm: ArmName
    elapsed_ms: float
    cpu_ms: float
    exit_code: int
    nodeids: tuple[str, ...]
    stdout: str
    stderr: str
    throttled: QuotaThrottling | None


def _child_cpu_ms() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (usage.ru_utime + usage.ru_stime) * 1000.0


def build_one_test_project(directory: Path) -> Path:
    path = directory / PROJECT_FILE_NAME
    path.write_text(PROJECT_FILE_SOURCE, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == PROJECT_FILE_SOURCE, f"{path} lost its source"
    return directory


def _timed(run_pytest: RunPytest, *, arm: ArmName, cwd: Path, args: Sequence[str]) -> TimedRun:
    throttled_before = read_quota_throttling()
    cpu_before = _child_cpu_ms()
    started = time.perf_counter()
    completed = run_pytest(*args, cwd=cwd, timeout=_CHILD_TIMEOUT_SECONDS)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    cpu_ms = _child_cpu_ms() - cpu_before
    throttled_after = read_quota_throttling()
    # A collected node id is the only line of a -q --collect-only stream carrying the separator.
    nodeids = tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and not line.lstrip().startswith("=")
    )
    return TimedRun(
        arm=arm,
        elapsed_ms=elapsed_ms,
        cpu_ms=cpu_ms,
        exit_code=completed.returncode,
        nodeids=nodeids,
        stdout=completed.stdout,
        stderr=completed.stderr,
        throttled=(
            throttled_after.since(throttled_before)
            if throttled_before is not None and throttled_after is not None
            else None
        ),
    )


def _preempted(run: TimedRun) -> bool:
    return was_preempted(elapsed_ms=run.elapsed_ms, cpu_ms=run.cpu_ms, floor_ms=PREEMPTION_FLOOR_MS)


def test_plugin_startup_stays_inside_the_budget(run_pytest: RunPytest, tmp_path: Path) -> None:
    project = build_one_test_project(tmp_path)
    honest: list[tuple[TimedRun, TimedRun]] = []
    starved: list[tuple[TimedRun, TimedRun]] = []
    for _ in range(SAMPLE_COUNT + PAIR_RETRY_BUDGET):
        pair = (
            _timed(run_pytest, arm="with-plugin", cwd=project, args=_COLLECT_ARGS),
            _timed(
                run_pytest,
                arm="without-plugin",
                cwd=project,
                args=(*_COLLECT_ARGS, *_PLUGIN_DISABLED_ARGS),
            ),
        )
        if any(_preempted(run) for run in pair):
            starved.append(pair)
        else:
            honest.append(pair)
        if len(honest) == SAMPLE_COUNT:
            break
    attempts = (*honest, *starved)
    for run in (run for pair in attempts for run in pair):
        assert run.exit_code == 0, f"{run.arm} exited {run.exit_code}: {run.stdout}{run.stderr}"
        assert run.nodeids == (COLLECTED_NODEID,), f"{run.arm} collected {run.nodeids!r}"
    # A host that never let up spends the retry budget, and the pairs it starved the least are then
    # measured, so a real regression still reaches the ceiling below.
    starved.sort(key=lambda pair: max(run.elapsed_ms - run.cpu_ms for run in pair))
    measured = (*honest, *starved[: SAMPLE_COUNT - len(honest)])
    samples_a = [pair[0].cpu_ms for pair in measured]
    samples_b = [pair[1].cpu_ms for pair in measured]
    median_a, median_b = statistics.median(samples_a), statistics.median(samples_b)
    delta = median_a - median_b
    wall_a = statistics.median([pair[0].elapsed_ms for pair in measured])
    wall_b = statistics.median([pair[1].elapsed_ms for pair in measured])
    wall_delta = wall_a - wall_b
    substituted = len(measured) - len(honest)
    throttling = describe_quota_throttling(
        [run.throttled for pair in attempts for run in pair if run.throttled is not None]
    )
    print(
        f"plugin startup: wall clock with plugin {wall_a:.1f} ms, without {wall_b:.1f} ms, delta "
        f"{wall_delta:.1f} ms (AAP 0.14.2 budget {STARTUP_BUDGET_MS:.0f} ms, {SAMPLE_COUNT} "
        f"pairs); CPU time with plugin {median_a:.1f} ms, without {median_b:.1f} ms, delta "
        f"{delta:.1f} ms; {len(starved)} starved pairs set aside, {substituted} measured in "
        f"their place; {throttling}"
    )
    assert wall_delta <= STARTUP_BUDGET_MS, (
        f"plugin startup overhead {wall_delta:.2f} ms of wall clock exceeds the "
        f"{STARTUP_BUDGET_MS:.0f} ms budget AAP 0.14.2 sets (with plugin {wall_a:.2f} ms, "
        f"without {wall_b:.2f} ms, delta {wall_delta:.2f} ms; CPU time {median_a:.2f} vs "
        f"{median_b:.2f} ms, delta {delta:.2f} ms; {len(starved)} starved pairs set aside, "
        f"{substituted} measured in their place; {throttling})"
    )
    assert delta <= STARTUP_BUDGET_MS, (
        f"the plugin's CPU time, {delta:.2f} ms, exceeds the {STARTUP_BUDGET_MS:.0f} ms budget "
        f"AAP 0.14.2 sets (with plugin {median_a:.2f} ms, without {median_b:.2f} ms, delta "
        f"{delta:.2f} ms); wall clock {wall_a:.2f} vs {wall_b:.2f} ms, delta "
        f"{wall_delta:.2f} ms; {len(starved)} starved pairs set aside; {throttling})"
    )


def test_the_timed_arm_loads_the_plugin_from_the_entry_point(
    run_pytest: RunPytest, tmp_path: Path
) -> None:
    project = build_one_test_project(tmp_path)
    completed = run_pytest(*_TRACE_CONFIG_ARGS, cwd=project, timeout=_CHILD_TIMEOUT_SECONDS)
    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, f"the --trace-config run exited {completed.returncode}"
    assert _PLUGIN_REGISTRATION in output, f"pytest never imported {_PLUGIN_MODULE}: {output}"
    assert _PLUGIN_BANNER in output, f"the plugin wrote no profile banner: {output}"


def test_importing_the_plugin_imports_no_heavy_library(
    foundry_python: str, subprocess_env: Callable[..., dict[str, str]], tmp_path: Path
) -> None:
    completed = subprocess.run(
        [foundry_python, "-c", _LAZY_IMPORT_SCRIPT],
        cwd=str(tmp_path),
        env=subprocess_env(),
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, f"importing {_PLUGIN_MODULE} failed: {completed.stderr}"
    reported = [line for line in completed.stdout.splitlines() if line.startswith(_LOADED_PREFIX)]
    assert len(reported) == 1, f"the guard program printed {len(reported)} lines"
    loaded = tuple(name for name in reported[0][len(_LOADED_PREFIX) :].split(",") if name)
    assert loaded == (), f"importing {_PLUGIN_MODULE} also imported {loaded} (AAP 0.4.8)"
