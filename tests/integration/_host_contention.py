"""Host-contention evidence the two timing budgets of AAP 0.14.2 are read through.

A sample is set aside only on its own wall clock exceeding its own CPU time, never on host load.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

# Wall clock a sample may spend beyond its own CPU time before the host, not the product, is blamed.
PREEMPTION_RATIO: Final[float] = 0.25


def was_preempted(*, elapsed_ms: float, cpu_ms: float, floor_ms: float) -> bool:
    return elapsed_ms - cpu_ms > max(floor_ms, cpu_ms * PREEMPTION_RATIO)


@dataclass(frozen=True, slots=True)
class QuotaThrottling:
    periods: int
    microseconds: int

    def since(self, earlier: QuotaThrottling) -> QuotaThrottling:
        return QuotaThrottling(
            periods=self.periods - earlier.periods,
            microseconds=self.microseconds - earlier.microseconds,
        )


def read_quota_throttling() -> QuotaThrottling | None:
    try:
        membership = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    member = next((row for row in membership.splitlines() if row.startswith("0::")), None)
    if member is None:
        return None
    stat_path = Path("/sys/fs/cgroup", member[3:].lstrip("/"), "cpu.stat")
    try:
        text = stat_path.read_text(encoding="utf-8")
        pairs = [row.split() for row in text.splitlines() if row.strip()]
        counters = {name: int(value) for name, value in pairs}
    except (OSError, ValueError):
        return None
    try:
        return QuotaThrottling(counters["nr_throttled"], counters["throttled_usec"])
    except KeyError:
        return None


def describe_quota_throttling(readings: Sequence[QuotaThrottling]) -> str:
    if not readings:
        return "cpu quota throttling not reported by this host"
    periods = sum(reading.periods for reading in readings)
    frozen_ms = sum(reading.microseconds for reading in readings) / 1000.0
    return f"cpu quota throttled {frozen_ms:.0f} ms over {periods} periods"
