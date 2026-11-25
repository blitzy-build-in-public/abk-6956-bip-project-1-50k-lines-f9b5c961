"""The host-contention evidence the two timing budgets of AAP 0.14.2 are read through.

Both budgets are stated as a cost the product pays: 150 ms of plugin startup and 50 ms of
configuration resolution. A shared host adds wait to a sample that no product work explains -- a
container whose CPU quota is spent freezes every runnable task until the next accounting period,
and a machine at load 5 deschedules a child for tens of milliseconds at a time -- so a budget read
on such a host measures the host. The two probes here are what let a measurement tell the two
apart.

:func:`was_preempted` is the portable one and the only one that may set a sample aside: a sample
whose own wall clock grossly exceeds its own CPU time spent the difference waiting, which no
full-speed sample does however slow the product made it. :func:`read_quota_throttling` is the
Linux-specific corroboration: cgroup v2 counts the periods in which a cgroup exhausted its quota
and the time its tasks spent frozen, and the delta across one sample is the contention that sample
ran under.

Both answer ``None`` on a host that exposes neither -- macOS, which the product supports, has no
``cgroup`` files -- so a measurement there behaves exactly as it did before these probes existed.
No clock is read and no time is spent here (AAP 0.14.3): the counters are the kernel's own.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__: Final[tuple[str, ...]] = (
    "PREEMPTION_RATIO",
    "QuotaThrottling",
    "describe_quota_throttling",
    "read_quota_throttling",
    "was_preempted",
)

PREEMPTION_RATIO: Final[float] = 0.25
"""How much wall clock one sample may spend beyond its own CPU time before it counts as wait.

A sample that ran at full speed spends its wall clock doing work, so the two clocks agree to within
a few milliseconds; a sample the host kept off a CPU core spends the difference waiting. The
allowance grows with the sample, because a longer run has more chances to be descheduled, and each
caller adds a floor of its own so that a sample of one millisecond is not judged on scheduling
jitter.
"""

_PROCESS_CGROUP_PATH: Final[Path] = Path("/proc/self/cgroup")
"""Where a process reads the cgroup it belongs to; cgroup v2 names it on a ``0::`` line."""

_CGROUP_ROOT: Final[Path] = Path("/sys/fs/cgroup")
"""Where cgroup v2 mounts the hierarchy that file names."""

_CPU_STAT_NAME: Final[str] = "cpu.stat"
"""The per-cgroup document carrying its CPU-quota throttling counters."""

_V2_MEMBERSHIP_PREFIX: Final[str] = "0::"
"""The hierarchy line cgroup v2 writes; a cgroup v1 host writes numbered controllers instead."""

_THROTTLED_PERIODS_KEY: Final[str] = "nr_throttled"
"""The counter naming how many accounting periods the cgroup exhausted its quota in."""

_THROTTLED_TIME_KEY: Final[str] = "throttled_usec"
"""The counter holding, in microseconds, how long the cgroup's tasks were frozen by that."""


def was_preempted(*, elapsed_ms: float, cpu_ms: float, floor_ms: float) -> bool:
    """Return whether one sample's own wall clock grossly exceeded the CPU time it was given.

    This is the proof a sample describes the host rather than the product. It needs nothing but the
    two clocks every supported platform has, and it can never set aside a sample that took its CPU
    time at full speed and still came out slow, which is what a real regression looks like.
    """
    return elapsed_ms - cpu_ms > max(floor_ms, cpu_ms * PREEMPTION_RATIO)


@dataclass(frozen=True, slots=True)
class QuotaThrottling:
    """CPU-quota throttling as one reading of a cgroup's ``cpu.stat`` reports it.

    ``periods`` counts the accounting periods in which the cgroup exhausted its quota and was
    frozen until the next one, and ``microseconds`` the total time its tasks spent frozen. Two
    readings taken around a sample are subtracted, so what that sample saw is what the second
    reports beyond the first.
    """

    periods: int
    microseconds: int

    def since(self, earlier: QuotaThrottling) -> QuotaThrottling:
        """Return what this reading reports beyond ``earlier``: the throttling one sample saw."""
        return QuotaThrottling(
            periods=self.periods - earlier.periods,
            microseconds=self.microseconds - earlier.microseconds,
        )

    @property
    def milliseconds(self) -> float:
        """Return the frozen time in the unit both budgets of AAP 0.14.2 are written in."""
        return self.microseconds / 1000.0


def read_quota_throttling() -> QuotaThrottling | None:
    """Return this process's CPU-quota throttling counters, or ``None`` where the host keeps none.

    ``None`` is the answer on every host without cgroup v2 -- macOS among them -- and on a Linux
    host whose files are unreadable or silent about these two counters. A caller then has no
    corroboration to report and no evidence to retake a sample on beyond the portable wall-clock
    test, which is exactly how that host was measured before.
    """
    try:
        membership = _PROCESS_CGROUP_PATH.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in membership.splitlines():
        if line.startswith(_V2_MEMBERSHIP_PREFIX):
            relative = line[len(_V2_MEMBERSHIP_PREFIX) :].lstrip("/")
            stat_path = _CGROUP_ROOT / relative / _CPU_STAT_NAME
            break
    else:
        return None
    try:
        counters = {
            name: int(value)
            for name, value in (
                row.split()
                for row in stat_path.read_text(encoding="utf-8").splitlines()
                if row.strip()
            )
        }
    except (OSError, ValueError):
        return None
    periods = counters.get(_THROTTLED_PERIODS_KEY)
    microseconds = counters.get(_THROTTLED_TIME_KEY)
    if periods is None or microseconds is None:
        return None
    return QuotaThrottling(periods=periods, microseconds=microseconds)


def describe_quota_throttling(readings: Sequence[QuotaThrottling]) -> str:
    """Return one clause naming the quota throttling a set of samples observed.

    An empty ``readings`` is said out loud rather than omitted, so a reader knows that a measurement
    reporting no contention either ran on a quiet host or on one that keeps no such counters.
    """
    if not readings:
        return "cpu quota throttling not reported by this host"
    periods = sum(reading.periods for reading in readings)
    milliseconds = sum(reading.milliseconds for reading in readings)
    return f"cpu quota throttled {milliseconds:.0f} ms over {periods} periods"
