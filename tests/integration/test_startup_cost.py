"""Timing instrument for plugin startup: ten boots of one command against the AAP 0.14.2 budget.

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

**Which clock the budget is read from.** Each invocation is sampled twice: the wall clock
(``time.perf_counter``) around the child, and the CPU time the child consumed
(``resource.getrusage(RUSAGE_CHILDREN)``, read as the delta across the call). Both describe the same
work, and on an idle host they agree to within a millisecond, because both arms are CPU-bound
processes -- measured here: 466 ms of wall clock against 465 ms of CPU time for arm A. On a shared
host they diverge sharply: the child is descheduled, which adds to wall clock and to nothing else.
Measured on this 2-vCPU host under a parallel batch of agents, the wall-clock delta across identical
runs moved between 126 ms and 1009 ms while the CPU-time delta stayed inside 125-139 ms, so a budget
read from wall clock fails for reasons that have nothing to do with the plugin. The budget is
therefore asserted on the CPU-time delta -- the plugin's own share of the work -- and the wall-clock
figures are printed beside it on every run so the reader can see how contended the host was. AAP
0.4.8 forbids import-time I/O, so CPU time is a faithful measure of everything the plugin may do on
this path; ``tests/unit/test_layering.py`` and the fresh-interpreter guard below bound it statically
and at run time.

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

The fresh-interpreter test is the runtime counterpart of AAP 0.4.8's module-scope import rule: it
asserts what an installed ``import foundry.plugin`` actually pulls in, which no static scan can. No
sleep is spent and no wall-clock reading is taken -- ``time.perf_counter`` and the kernel's CPU
accounting are the only clocks (AAP 0.14.3). Every file these tests write is written by a child
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

__all__: Final[tuple[str, ...]] = ("build_one_test_project",)

# --- The budget, the sample and the project both arms share ------------------

STARTUP_BUDGET_MS: Final[float] = 150.0
"""The plugin-startup budget AAP 0.14.2 sets, in milliseconds: the delta may not exceed it."""

SAMPLE_COUNT: Final[int] = 5
"""How many timed invocations each arm gets: the count AAP 0.14.2 names for the median."""

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
"""The single node id each of the ten timed invocations must collect, in either arm."""

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

Five times the ~0.5 s a boot costs on an idle host, and small enough that the ten timed invocations
plus the two auxiliary ones stay inside the per-test limit Foundry's own ``execution.timeout``
enforces on this suite.
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

    ``cpu_ms`` is what the budget is read from and ``elapsed_ms`` is the wall clock a user waits;
    both are kept beside the exit code and the collected node ids so the caller can establish that
    the invocation did the work before the clock is allowed to say anything.
    """

    arm: ArmName
    elapsed_ms: float
    cpu_ms: float
    exit_code: int
    nodeids: tuple[str, ...]
    stdout: str
    stderr: str


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
    """Run one invocation through the harness, sampling it with the only two clocks read here."""
    cpu_before = _child_cpu_ms()
    started = time.perf_counter()
    completed = run_pytest(*args, cwd=cwd, timeout=_CHILD_TIMEOUT_SECONDS)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    cpu_ms = _child_cpu_ms() - cpu_before
    return TimedRun(
        arm=arm,
        elapsed_ms=elapsed_ms,
        cpu_ms=cpu_ms,
        exit_code=completed.returncode,
        nodeids=_collected_nodeids(completed.stdout),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


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


def test_plugin_startup_stays_inside_the_budget(run_pytest: RunPytest, tmp_path: Path) -> None:
    """The plugin's startup cost, median of five interleaved pairs, stays inside 150 ms."""
    project = build_one_test_project(tmp_path)
    with_plugin: list[TimedRun] = []
    without_plugin: list[TimedRun] = []
    for _ in range(SAMPLE_COUNT):
        # Interleaved in this order for every pair, so a host that gets slower or busier while the
        # loop runs moves both arms together and the difference stays the plugin's own cost.
        with_plugin.append(_timed(run_pytest, arm="with-plugin", cwd=project, args=_COLLECT_ARGS))
        without_plugin.append(
            _timed(
                run_pytest,
                arm="without-plugin",
                cwd=project,
                args=(*_COLLECT_ARGS, *_PLUGIN_DISABLED_ARGS),
            )
        )

    _assert_arms_did_the_work((*with_plugin, *without_plugin))

    samples_a = tuple(run.cpu_ms for run in with_plugin)
    samples_b = tuple(run.cpu_ms for run in without_plugin)
    median_a = _median(samples_a)
    median_b = _median(samples_b)
    delta = median_a - median_b
    wall_a = _median(tuple(run.elapsed_ms for run in with_plugin))
    wall_b = _median(tuple(run.elapsed_ms for run in without_plugin))
    print(
        f"plugin startup: with plugin {median_a:.1f} ms CPU, without {median_b:.1f} ms CPU, "
        f"delta {delta:.1f} ms (budget {STARTUP_BUDGET_MS:.0f} ms, {SAMPLE_COUNT} pairs); "
        f"wall clock {wall_a:.1f} ms against {wall_b:.1f} ms, delta {wall_a - wall_b:.1f} ms"
    )

    # Only the ceiling is asserted: the two arms share the interpreter's start and pytest's boot, so
    # the difference carries noise of its own and a negative delta is a fast sample, never a fault.
    assert delta <= STARTUP_BUDGET_MS, (
        f"plugin startup overhead {delta:.2f} ms exceeds the 150 ms budget "
        f"(with plugin: {median_a:.2f} ms, without: {median_b:.2f} ms, "
        f"samples A: {sorted(samples_a)}, samples B: {sorted(samples_b)}; "
        f"wall clock: with plugin {wall_a:.2f} ms, without {wall_b:.2f} ms)"
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
