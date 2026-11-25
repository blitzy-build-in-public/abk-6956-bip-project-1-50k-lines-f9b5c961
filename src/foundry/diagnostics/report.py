"""The ``foundry doctor`` result model and the two renderings it feeds.

:meth:`DoctorReport.as_rows` yields the table of AAP 0.7.4, :meth:`DoctorReport.as_document` the
document of AAP 0.7.3 and :attr:`DoctorReport.ok` the exit-code verdict of AAP 0.4.4. No I/O, no
environment read, no logging: importing this module stays free of side effects (AAP 0.4.8).
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "DoctorReport",
    "InterpreterInfo",
    "MAX_SUPPORTED_VERSION",
    "MIN_SUPPORTED_VERSION",
    "PROBE_STATUS_ORDER",
    "ProbeResult",
    "ProbeStatus",
    "SUPPORTED_IMPLEMENTATION",
]


class ProbeStatus(StrEnum):
    """The three states a doctor probe can report.

    The values are the serialised ``status`` of the AAP 0.7.3 document and the ``Status`` column of
    the AAP 0.7.4 table, so they are fixed names under AAP 0.4.9. ``WARN`` never changes the exit
    code; only ``FAIL`` on a required probe does (AAP 0.4.4).
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


# Ascending severity, for ranking or comparing probe states. Never used to reorder a report, whose
# row and document order is always the order the probe runner supplied.
PROBE_STATUS_ORDER: Final[tuple[ProbeStatus, ...]] = (
    ProbeStatus.PASS,
    ProbeStatus.WARN,
    ProbeStatus.FAIL,
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One probe's outcome inside a doctor report.

    ``name``, ``status`` and ``detail`` are both the table row of AAP 0.7.4 and the ``checks``
    element of AAP 0.7.3; ``detail`` is serialised verbatim, so it carries no timestamp, duration or
    ``repr``. ``required`` decides whether a ``FAIL`` reaches the exit code (AAP 0.4.4) and is
    deliberately absent from :meth:`as_dict`.
    """

    name: str
    status: ProbeStatus
    detail: str
    required: bool = True

    @property
    def is_failure(self) -> bool:
        """Return whether this probe reported ``FAIL``, compared by identity."""
        return self.status is ProbeStatus.FAIL

    def as_row(self) -> tuple[str, str, str]:
        """Return the table's ``(Probe, Status, Detail)`` cells (AAP 0.7.4)."""
        return (self.name, self.status.value, self.detail)

    def as_dict(self) -> dict[str, str]:
        """Return this probe's ``checks`` element for AAP 0.7.3."""
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


# AAP 0.3.1 pins ``requires-python`` and AAP 0.4.1 supports CPython 3.11-3.13 on Linux and macOS,
# Windows excepted. The policy is stated here and evaluated once in ``InterpreterInfo.current``.
SUPPORTED_IMPLEMENTATION: Final[str] = "CPython"
MIN_SUPPORTED_VERSION: Final[tuple[int, int]] = (3, 11)
MAX_SUPPORTED_VERSION: Final[tuple[int, int]] = (3, 13)


@dataclass(frozen=True, slots=True)
class InterpreterInfo:
    """The interpreter a doctor run executes on, and whether Foundry supports it.

    ``supported`` carries the policy verdict of :meth:`current` and is its only expression, so an
    unsupported interpreter surfaces as a probe ``WARN`` and never a ``FAIL`` (AAP 0.4.1).
    """

    implementation: str
    version: str
    platform: str
    supported: bool

    @classmethod
    def current(cls) -> InterpreterInfo:
        """Return the running interpreter's :class:`InterpreterInfo`.

        ``supported`` is true only for CPython ``(3, 11)`` through ``(3, 13)`` — the range AAP 0.3.1
        pins and AAP 0.4.1 supports. Only :mod:`sys` and :mod:`platform` are read: no subprocess,
        environment, filesystem or socket.
        """
        implementation = platform.python_implementation()
        version_pair = (sys.version_info.major, sys.version_info.minor)
        return cls(
            implementation=implementation,
            version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            platform=sys.platform,
            supported=(
                implementation == SUPPORTED_IMPLEMENTATION
                and MIN_SUPPORTED_VERSION <= version_pair <= MAX_SUPPORTED_VERSION
            ),
        )

    def as_dict(self) -> dict[str, object]:
        """Return the ``interpreter`` section: four fields, in this order (AAP 0.7.3)."""
        return {
            "implementation": self.implementation,
            "version": self.version,
            "platform": self.platform,
            "supported": self.supported,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Every probe of one ``foundry doctor`` run, in the order the runner supplied it.

    ``checks`` is never sorted or grouped by status: the table of AAP 0.7.4 and the document of
    AAP 0.7.3 are read side by side and must list the same probes in the same sequence.
    """

    interpreter: InterpreterInfo
    checks: tuple[ProbeResult, ...]

    @property
    def failed(self) -> tuple[ProbeResult, ...]:
        """Return the required probes that failed, in the order they were supplied."""
        return tuple(check for check in self.checks if check.required and check.is_failure)

    @property
    def ok(self) -> bool:
        """Return whether no required probe failed; ``False`` means exit ``2`` (AAP 0.4.4)."""
        return not self.failed

    def as_rows(self) -> tuple[tuple[str, str, str], ...]:
        """Return one ``(Probe, Status, Detail)`` row per probe, in the order supplied."""
        return tuple(check.as_row() for check in self.checks)

    def as_document(self) -> dict[str, object]:
        """Return the ``foundry doctor --json`` document (AAP 0.7.3)."""
        return {
            "interpreter": self.interpreter.as_dict(),
            "checks": [check.as_dict() for check in self.checks],
            "ok": self.ok,
        }
