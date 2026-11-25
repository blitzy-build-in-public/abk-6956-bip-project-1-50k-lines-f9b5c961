"""Timing instrument for plugin startup: five interleaved pairs against the AAP 0.14.2 budget.

AAP 0.14.2 budgets plugin startup overhead at "<= 150 ms, median of 5 ``pytest --collect-only`` runs
on a one-test directory, measured against ``-p no:foundry``", and implicit requirement I5 -- every
heavy library imported inside the fixture or hook that first needs it, never at module scope of
``foundry.plugin`` -- is the structural reason that budget is reachable. What is measured is the
*difference between two arms* of the same command: arm A boots pytest with the distribution
installed, so the ``pytest11`` entry point loads :mod:`foundry.plugin` during ``_preparse`` with no
flag and no configuration file (AAP 0.4.2); arm B boots it with the plugin disabled for that
invocation. Interpreter start, pytest's own boot and the collection of the one-test directory are
common to both arms and cancel in the subtraction, so the delta is the plugin's import and
registration cost; reading the raw milliseconds of either arm as the plugin's own cost is the
mistake the subtraction exists to prevent. Neither arm runs a test: ``--collect-only`` is the
cheapest invocation that still boots pytest fully, parses the ``--foundry-*`` option group, resolves
the configuration and reaches session start and session finish.

**Which clock the budget is read from.** The budget is the one AAP 0.14.2 states -- "<= 150 ms,
median of 5 ``pytest --collect-only`` runs on a one-test directory, measured against
``-p no:foundry``" -- so it is read from the wall clock: the median ``elapsed_ms`` of the
with-plugin arm minus the median of the without-plugin arm, over the interleaved pairs the host did
not starve (below), and it fails the build. Each invocation is sampled twice: that wall clock
(``time.perf_counter`` around the child), and the CPU time the child consumed
(``resource.getrusage(RUSAGE_CHILDREN)``, read as the delta across the call). Both describe the same
work, and on an idle host they agree to within a millisecond, because both arms are CPU-bound
processes -- measured here: 462 ms of wall clock against 461 ms of CPU time for arm A. On a shared
host they diverge sharply: the child is descheduled, which adds to wall clock and to nothing else.
Measured on this 2-vCPU host under a parallel batch of agents, the wall-clock delta across identical
runs moved between 126 ms and 1009 ms while the CPU-time delta stayed inside 125-139 ms. So the CPU
time of those very same pairs is printed beside the wall clock on every run and carries a second
assertion of the same 150 ms ceiling: it is host-insensitive, which keeps a figure naming the
plugin's own share of the work readable when the host will not give an honest pair. Neither figure
substitutes for the other -- only the wall clock is the AAP's budget, and only it can show a
regression that waits rather than computes -- and neither assertion is conditional on the other
passing.

The premise the CPU figure rests on -- that nothing on the plugin's startup path waits on I/O -- now
holds: :mod:`foundry` resolves ``__version__`` on first access through a module ``__getattr__``, so
importing the package reads no distribution metadata (AAP 0.4.8), and
:func:`test_importing_the_plugin_imports_no_heavy_library` proves from a fresh interpreter that
``import foundry.plugin`` pulls in none of the heavy libraries either, which leaves the path nothing
to do but import and register. ``tests/unit/test_layering.py`` bounds the same rule statically.

**This module is the repository's single allowlisted user of the plugin-disable flag.** The static
scan in ``tests/unit/test_no_dash_p_flag.py`` fails the build on any other occurrence, because a
suite that disables the plugin proves nothing about it; the flag is spelled once, in
:data:`_PLUGIN_DISABLED_ARGS` (``-p no:foundry``), and reaches only the arm whose cost is subtracted
(AAP 0.14.2). Do not "clean it up".

Four properties keep the measurement honest. The arms are interleaved (A, B, A, B, ...), so a host
that slows down partway through moves both arms rather than one, and each figure is the median of
:data:`SAMPLE_COUNT` samples, so a single descheduled run cannot decide the outcome. The work is
compared before the clock is read: every invocation must exit ``0`` and report the same single node
id, so a delta cannot come from one arm collecting and the other not. The plugin's presence in the
timed directory is proved by a separate ``--trace-config`` run, which is excluded from timing
because it changes the output volume of an invocation. And a negative delta is noise, never a
failure: only the ceiling is asserted, never a floor.

**A pair the host starved is retaken, not measured.** Interleaving cancels a drift that moves both
arms, but a 2-vCPU host shared with a batch of agents also adds wait that no interleaving removes: a
container whose CPU quota is spent freezes every runnable task until the next accounting period,
and a descheduled child spends wall clock on nothing while its CPU time stands still. Such a pair
describes the host, not the plugin, and the CPU time of a starved run does not escape it -- a child
that keeps losing its core also keeps losing its cache and its frequency. The proof that a pair was
starved is read from the pair itself, never from the host's load: an arm whose own wall clock
grossly exceeds its own CPU time spent the difference waiting
(:func:`tests.integration._host_contention.was_preempted`). Up to :data:`PAIR_RETRY_BUDGET` extra
pairs replace the starved ones, and a host that keeps starving every pair spends that budget and
then has the pairs it starved the least measured in their place, so a real regression still fails
the budget. The honest pairs measured, the pairs set aside and the cgroup's own throttling count are
all printed beside the figures.

The fresh-interpreter test is the runtime counterpart of AAP 0.4.8's module-scope import rule: it
asserts what an installed ``import foundry.plugin`` actually pulls in, which no static scan can. No
sleep is spent and no calendar is read -- ``time.perf_counter`` measures the interval and the
kernel's CPU accounting the work (AAP 0.14.3). Every file these tests write is written by a child
process whose working directory is the ``tmp_path`` project they were given.
"""

from __future__ import annotations

import resource
import statistics
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, TypeAlias

from _host_contention import (
    QuotaThrottling,
    describe_quota_throttling,
    read_quota_throttling,
    was_preempted,
)

__all__: Final[tuple[str, ...]] = ("build_one_test_project",)

# --- The budget, the sample and the project both arms share ------------------

STARTUP_BUDGET_MS: Final[float] = 150.0
"""The plugin-startup budget AAP 0.14.2 sets, in milliseconds: neither delta may exceed it.

The wall-clock delta is the AAP's own figure and the assertion that enforces it; the CPU-time delta
of the same pairs is asserted against the same ceiling beside it.
"""

SAMPLE_COUNT: Final[int] = 5
"""How many timed invocations each arm gets: the count AAP 0.14.2 names for the median."""

PAIR_RETRY_BUDGET: Final[int] = 5
"""How many extra pairs may replace pairs the host starved, so a saturated host cannot loop here.

Equal to :data:`SAMPLE_COUNT`, so a host that is contended throughout costs at most twice the ten
boots the budget is read from, and small enough that the whole test stays inside the per-item
timeout Foundry's own ``execution.timeout`` enforces on this suite.
"""

PREEMPTION_FLOOR_MS: Final[float] = 100.0
"""How much wall clock one arm may spend beyond its own CPU time before the host starved it.

Both arms are CPU-bound boots of the same command, so at full speed the two clocks agree to within
a few milliseconds: measured on this 2-vCPU host, an arm that got its CPU stayed inside 44 ms of
wait -- 0.3 ms once the host was calm -- while an arm that was kept off it lost 250 ms or more. The
floor sits between the two, and ``PREEMPTION_RATIO`` in ``_host_contention`` widens it for a longer
run.
"""

PROJECT_FILE_NAME: Final[str] = "test_one.py"
"""The one module the measured directory holds, named by the node id every arm must report."""

PROJECT_FILE_SOURCE: Final[str] = (
    '"""The one test the measured directory collects."""\n'
    "\n"
    "\n"
    "def test_one() -> None:\n"
    "    assert True\n"
)
"""Its content: one trivial passing test, so collection has exactly one node id to report."""

COLLECTED_NODEID: Final[str] = "test_one.py::test_one"
"""The single node id every timed invocation must collect, in either arm."""

_COLLECT_ARGS: Final[tuple[str, ...]] = ("--collect-only", "-q")
"""Arm A: the cheapest invocation that boots pytest with the installed plugin."""

_PLUGIN_DISABLED_ARGS: Final[tuple[str, ...]] = ("-p", "no:foundry")
"""Arm B: the same invocation with Foundry's entry-point plugin disabled (AAP 0.14.2, allowlisted).

This is the repository's documented exception to "no invocation disables the plugin": the arm
exists only to be subtracted from arm A, and ``tests/unit/test_no_dash_p_flag.py`` allowlists this
module by name.
"""

_TRACE_CONFIG_ARGS: Final[tuple[str, ...]] = ("--collect-only", "--trace-config")
"""The untimed plugin-presence proof; excluded from timing because it changes output volume."""

_PLUGIN_MODULE: Final[str] = "foundry.plugin"
"""The module the ``pytest11`` entry point named ``foundry`` targets (AAP 0.4.2)."""

_PLUGIN_REGISTRATION: Final[str] = f"PLUGIN registered: <module '{_PLUGIN_MODULE}'"
"""The ``--trace-config`` line pytest writes when it imports the plugin module, prefix included.

Asserted as a prefix rather than a substring, so the proof is that this module was imported, not
that some line happens to contain its name.
"""

_PLUGIN_BANNER: Final[str] = "foundry: profile="
"""The terminal banner session start writes (AAP 0.7.4): proof the registered plugin really ran."""

_HEAVY_LIBRARIES: Final[tuple[str, ...]] = ("sqlalchemy", "faker", "jsonschema", "rich", "jinja2")
"""The libraries AAP 0.2.1 requires to stay out of the plugin's import path (requirement I5).

Named as import roots, because ``sys.modules`` holds ``rich.console`` rather than ``rich`` and the
guard asks whether the package was touched at all.
"""

_LOADED_PREFIX: Final[str] = "LOADED:"
"""The marker the reported line starts with, so the line is found among any other child output."""

_LAZY_IMPORT_SCRIPT: Final[str] = (
    "import sys\n"
    "import foundry.plugin\n"
    f"loaded = [name for name in {_HEAVY_LIBRARIES!r} if name in sys.modules]\n"
    f"print({_LOADED_PREFIX!r} + ','.join(loaded))\n"
)
"""The program the fresh interpreter runs: import the plugin, then report what came with it.

Built from the constants above so the program and the assertion cannot drift apart, and free of
``importorskip``: the libraries are installed in this environment, so their absence from
``sys.modules`` is evidence rather than a skip.
"""

_CHILD_TIMEOUT_SECONDS: Final[float] = 30.0
"""A ceiling on one child invocation, so a starved or hung interpreter fails rather than stalls.

Five times the ~0.5 s a boot costs on an idle host, and small enough that a boot that has not
finished in that time is a fault. The test's own boots -- ten, or twenty once the retry budget is
spent on a starved host -- stay inside the 60 s per-item limit Foundry's own ``execution.timeout``
enforces on this suite at the measured cost of a boot.
"""

ArmName: TypeAlias = Literal["with-plugin", "without-plugin"]
"""Which arm of the comparison an invocation belongs to."""


class RunPytest(Protocol):
    """The ``run_pytest`` fixture of ``tests/conftest.py``, narrowed to the calls made here.

    The fixture is typed ``Callable[..., CompletedProcess[str]]`` there; this protocol states the
    keyword arguments every call here passes, so a change to the harness signature becomes a type
    error at this module rather than a surprise at run time. Nothing here redefines the fixture or
    calls ``subprocess`` for a pytest invocation.
    """

    def __call__(
        self,
        *args: str,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float = 300.0,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``python -m pytest`` with ``args`` in ``cwd`` and capture its output."""
        ...


@dataclass(frozen=True, slots=True)
class TimedRun:
    """One measured invocation: which arm, what it cost, and what it collected.

    ``elapsed_ms`` is the wall clock AAP 0.14.2 budgets and ``cpu_ms`` the CPU time of the same
    invocation, reported beside it; both are kept beside the exit code and the collected node ids so
    the caller can establish that the invocation did the work before a clock is allowed to say
    anything. ``throttled`` is the CPU-quota throttling this cgroup recorded while the invocation
    ran, or ``None`` on a host that keeps no such counters.
    """

    arm: ArmName
    elapsed_ms: float
    cpu_ms: float
    exit_code: int
    nodeids: tuple[str, ...]
    stdout: str
    stderr: str
    throttled: QuotaThrottling | None


def _child_cpu_ms() -> float:
    """Return the CPU time every reaped child of this process has consumed so far, in milliseconds.

    ``ru_utime + ru_stime`` of ``RUSAGE_CHILDREN`` accumulates in the parent when a child is reaped,
    so the difference across one :func:`_timed` call is exactly that child's own CPU time -- the
    plugin's work without the time the host kept the process off a CPU core. POSIX-only, like the
    product (AAP 0.4.1).
    """
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (usage.ru_utime + usage.ru_stime) * 1000.0


def build_one_test_project(directory: Path) -> Path:
    """Write the one-test project into ``directory`` and return it.

    Deliberately free of ``pytest.ini``, ``pyproject.toml``, ``tox.ini``, ``setup.cfg`` and
    ``conftest.py``: an ini file would make the directory its own rootdir and apply that file's
    ``addopts`` to both arms, adding work the budget was never measured against, and a ``conftest``
    would add an import to both arms. One module with one test is the whole project.
    """
    path = directory / PROJECT_FILE_NAME
    path.write_text(PROJECT_FILE_SOURCE, encoding="utf-8", newline="\n")
    written = path.read_text(encoding="utf-8")
    assert written == PROJECT_FILE_SOURCE, f"{path} does not hold the source it was given"
    return directory


def _collected_nodeids(stdout: str) -> tuple[str, ...]:
    """Return the node ids pytest collected, read off a ``-q --collect-only`` stream.

    A collected node id is the only line of such a stream that carries the ``::`` separator: the
    summary line counts tests without naming one, and Foundry's own terminal lines name a run id and
    an artifacts root. Blank lines and separator rules are skipped for the same reason.
    """
    return tuple(
        line.strip()
        for line in stdout.splitlines()
        if "::" in line and not line.lstrip().startswith("=")
    )


def _timed(
    run_pytest: RunPytest,
    *,
    arm: ArmName,
    cwd: Path,
    args: Sequence[str],
) -> TimedRun:
    """Run one invocation through the harness, sampling it with the two clocks read here.

    The cgroup's throttling counters are read just outside the timed window, so they cost nothing
    the budget pays for and their delta describes the invocation the wall clock and the CPU time
    describe. A host that keeps no such counters leaves :attr:`TimedRun.throttled` as ``None``.
    """
    throttled_before = read_quota_throttling()
    cpu_before = _child_cpu_ms()
    started = time.perf_counter()
    completed = run_pytest(*args, cwd=cwd, timeout=_CHILD_TIMEOUT_SECONDS)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    cpu_ms = _child_cpu_ms() - cpu_before
    throttled_after = read_quota_throttling()
    return TimedRun(
        arm=arm,
        elapsed_ms=elapsed_ms,
        cpu_ms=cpu_ms,
        exit_code=completed.returncode,
        nodeids=_collected_nodeids(completed.stdout),
        stdout=completed.stdout,
        stderr=completed.stderr,
        throttled=(
            throttled_after.since(throttled_before)
            if throttled_before is not None and throttled_after is not None
            else None
        ),
    )


def _pair_was_preempted(pair: Sequence[TimedRun]) -> bool:
    """Return whether the host starved either arm of a pair, so its cost is not the plugin's.

    An arm that spent far more wall clock than CPU time was kept off a core for the difference, and
    the CPU time of such a run is not the cost the budget describes either: a child that keeps
    losing its core also loses its cache and its frequency. Only that evidence counts here, so a
    pair that took its CPU time at full speed and still came out slow is measured, never retaken.
    """
    return any(
        was_preempted(elapsed_ms=run.elapsed_ms, cpu_ms=run.cpu_ms, floor_ms=PREEMPTION_FLOOR_MS)
        for run in pair
    )


def _pair_wait_ms(pair: Sequence[TimedRun]) -> float:
    """Return the wall clock the host added to the longer-waiting arm of a pair."""
    return max(run.elapsed_ms - run.cpu_ms for run in pair)


def _median(samples: Sequence[float]) -> float:
    """Return the median of ``samples`` as a float: the figure AAP 0.14.2 budgets.

    The median rather than the mean, so one outlying run among five cannot drag the figure over the
    ceiling and one unusually fast run cannot hide a regression.
    """
    return float(statistics.median(samples))


def _assert_arms_did_the_work(runs: Sequence[TimedRun]) -> None:
    """Assert every invocation exited ``0`` and collected the one node id both arms share.

    This runs before any elapsed time is compared, so the delta can only ever describe two arms that
    did the same work: an arm that collected nothing would be cheap for the wrong reason
    (AAP 0.14.2).
    """
    for run in runs:
        assert run.exit_code == 0, (
            f"{run.arm} exited {run.exit_code}, so its timing describes a failed invocation.\n"
            f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
        )
        assert run.nodeids == (COLLECTED_NODEID,), (
            f"{run.arm} collected {run.nodeids!r} instead of the single node id "
            f"{COLLECTED_NODEID!r}; the arms no longer do the same work.\n"
            f"stdout:\n{run.stdout}\nstderr:\n{run.stderr}"
        )


# --- The budget -------------------------------------------------------------


def _timed_pair(run_pytest: RunPytest, project: Path) -> tuple[TimedRun, TimedRun]:
    """Run one interleaved with-plugin then without-plugin pair and return both invocations.

    Interleaved in this order for every pair, so a host that gets slower or busier while the loop
    runs moves both arms together and the difference stays the plugin's own cost.
    """
    with_plugin = _timed(run_pytest, arm="with-plugin", cwd=project, args=_COLLECT_ARGS)
    without_plugin = _timed(
        run_pytest,
        arm="without-plugin",
        cwd=project,
        args=(*_COLLECT_ARGS, *_PLUGIN_DISABLED_ARGS),
    )
    return with_plugin, without_plugin


def test_plugin_startup_stays_inside_the_budget(run_pytest: RunPytest, tmp_path: Path) -> None:
    """The plugin's startup cost stays inside 150 ms on both clocks, median of five pairs.

    The budget AAP 0.14.2 states is the wall clock, so that is the figure asserted first; the CPU
    time of the same pairs follows it.
    """
    project = build_one_test_project(tmp_path)
    honest: list[tuple[TimedRun, TimedRun]] = []
    starved: list[tuple[TimedRun, TimedRun]] = []
    for _ in range(SAMPLE_COUNT + PAIR_RETRY_BUDGET):
        pair = _timed_pair(run_pytest, project)
        if _pair_was_preempted(pair):
            starved.append(pair)
        else:
            honest.append(pair)
        if len(honest) == SAMPLE_COUNT:
            break

    attempts = (*honest, *starved)
    _assert_arms_did_the_work(tuple(run for pair in attempts for run in pair))

    # The budget is read from the pairs the host did not starve. A host that never let up spends the
    # retry budget instead of looping, and the pairs it starved the least are then measured in place
    # of nothing at all, so a real regression still reaches the ceiling below while the figures stay
    # as close to the plugin's own cost as such a host allows.
    least_starved = sorted(starved, key=_pair_wait_ms)
    measured = (*honest, *least_starved[: SAMPLE_COUNT - len(honest)])
    with_plugin = [pair[0] for pair in measured]
    without_plugin = [pair[1] for pair in measured]

    wall_samples_a = tuple(run.elapsed_ms for run in with_plugin)
    wall_samples_b = tuple(run.elapsed_ms for run in without_plugin)
    wall_a = _median(wall_samples_a)
    wall_b = _median(wall_samples_b)
    wall_delta = wall_a - wall_b
    cpu_samples_a = tuple(run.cpu_ms for run in with_plugin)
    cpu_samples_b = tuple(run.cpu_ms for run in without_plugin)
    cpu_a = _median(cpu_samples_a)
    cpu_b = _median(cpu_samples_b)
    cpu_delta = cpu_a - cpu_b
    substituted = len(measured) - len(honest)
    throttling = describe_quota_throttling(
        [run.throttled for pair in attempts for run in pair if run.throttled is not None]
    )
    print(
        f"plugin startup: wall clock with plugin {wall_a:.1f} ms, without {wall_b:.1f} ms, "
        f"delta {wall_delta:.1f} ms (AAP 0.14.2 budget {STARTUP_BUDGET_MS:.0f} ms over "
        f"{SAMPLE_COUNT} pairs); CPU time with plugin {cpu_a:.1f} ms against {cpu_b:.1f} ms, "
        f"delta {cpu_delta:.1f} ms; pairs the host starved {len(starved)} set aside, "
        f"{substituted} of the least starved measured after {SAMPLE_COUNT + PAIR_RETRY_BUDGET} "
        f"pairs; {throttling}"
    )

    # Only the ceiling is asserted, on either clock: the two arms share the interpreter's start and
    # pytest's boot, so the difference carries noise of its own and a negative delta is a fast
    # sample, never a fault.
    assert wall_delta <= STARTUP_BUDGET_MS, (
        f"plugin startup overhead {wall_delta:.2f} ms of wall clock exceeds the 150 ms budget "
        f"AAP 0.14.2 sets (median of {SAMPLE_COUNT} pytest --collect-only runs against "
        f"-p no:foundry): with plugin {wall_a:.2f} ms, without {wall_b:.2f} ms, "
        f"samples A: {sorted(wall_samples_a)}, samples B: {sorted(wall_samples_b)}; "
        f"CPU time: with plugin {cpu_a:.2f} ms, without {cpu_b:.2f} ms, "
        f"delta {cpu_delta:.2f} ms; {len(starved)} starved pairs set aside, "
        f"{substituted} of the least starved measured; {throttling})"
    )
    assert cpu_delta <= STARTUP_BUDGET_MS, (
        f"the plugin's own CPU time, {cpu_delta:.2f} ms, exceeds the 150 ms budget AAP 0.14.2 "
        f"sets, which no amount of host contention explains (with plugin {cpu_a:.2f} ms, "
        f"without {cpu_b:.2f} ms, samples A: {sorted(cpu_samples_a)}, "
        f"samples B: {sorted(cpu_samples_b)}); wall clock: with plugin {wall_a:.2f} ms, "
        f"without {wall_b:.2f} ms, delta {wall_delta:.2f} ms; "
        f"{len(starved)} starved pairs set aside, {substituted} of the least starved measured; "
        f"{throttling})"
    )


def test_the_timed_arm_loads_the_plugin_from_the_entry_point(
    run_pytest: RunPytest, tmp_path: Path
) -> None:
    """Arm A has the plugin because the ``pytest11`` entry point loaded it, with no flag.

    Untimed on purpose: ``--trace-config`` changes an invocation's output volume, so a run carrying
    it is not the invocation the budget describes. It is run against the same kind of directory as
    the timed arms, with no flag beyond ``--collect-only``, which is what makes the presence proof
    apply to them rather than to some other directory.
    """
    project = build_one_test_project(tmp_path)
    completed = run_pytest(*_TRACE_CONFIG_ARGS, cwd=project, timeout=_CHILD_TIMEOUT_SECONDS)
    output = f"{completed.stdout}\n{completed.stderr}"

    assert completed.returncode == 0, (
        f"the --trace-config run exited {completed.returncode}, so the plugin's registration "
        f"cannot be read from it.\noutput:\n{output}"
    )
    assert _PLUGIN_REGISTRATION in output, (
        f"pytest never imported {_PLUGIN_MODULE}, so arm A does not carry the plugin its timing is "
        f"credited with: the pytest11 entry point declared in pyproject.toml did not activate.\n"
        f"output:\n{output}"
    )
    assert _PLUGIN_BANNER in output, (
        "the plugin registered but wrote no profile banner, so the run was not Foundry's.\n"
        f"output:\n{output}"
    )


def test_importing_the_plugin_imports_no_heavy_library(
    foundry_python: str,
    subprocess_env: Callable[..., dict[str, str]],
    tmp_path: Path,
) -> None:
    """A fresh interpreter that imports the installed plugin touches none of the heavy libraries.

    This is the structural reason the budget above is reachable (AAP 0.4.8's module-scope import
    rule, requirement I5): the plugin may import ``pytest``, ``typing``, ``foundry.core`` and
    ``foundry.execution.state``, and everything heavier -- ``SQLAlchemy``, ``Faker``,
    ``jsonschema``, ``rich``, ``Jinja2`` -- belongs inside the fixture, CLI command or hook body
    that first needs it.

    The check runs in a child interpreter rather than in this one because this one has already
    imported pytest and Foundry's plugin, which would hide exactly what is being asked.

    The harness cannot express this one: ``run_pytest`` always invokes ``python -m pytest``, and the
    question here is what a plain ``import foundry.plugin`` does. So the child is built from the
    harness's own two building blocks -- the interpreter fixture and the environment builder -- and
    launched directly; no pytest invocation in this module bypasses ``run_pytest``.
    """
    completed = subprocess.run(
        [foundry_python, "-c", _LAZY_IMPORT_SCRIPT],
        cwd=str(tmp_path),
        env=subprocess_env(),
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, (
        f"importing {_PLUGIN_MODULE} in a fresh interpreter failed with "
        f"{completed.returncode}.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )

    reported = [line for line in completed.stdout.splitlines() if line.startswith(_LOADED_PREFIX)]
    assert len(reported) == 1, (
        f"the guard program reported {len(reported)} lines instead of one, so its answer cannot be "
        f"read.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    loaded = tuple(name for name in reported[0][len(_LOADED_PREFIX) :].split(",") if name)
    assert loaded == (), (
        f"importing {_PLUGIN_MODULE} also imported {loaded}, so the startup budget rests on "
        f"libraries that no longer load lazily (AAP 0.4.8, requirement I5)"
    )
