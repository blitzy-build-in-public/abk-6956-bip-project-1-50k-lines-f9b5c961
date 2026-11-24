"""The ``foundry doctor`` result model and the two renderings it feeds.

:mod:`foundry.diagnostics.checks` probes the environment and
:mod:`foundry.diagnostics.socket_guard` reports whether an offline guard is installed; this module
is where a probe's outcome is modelled and where the command's two published shapes are defined.
:meth:`DoctorReport.as_rows` yields the three-column ``Probe`` / ``Status`` / ``Detail`` table of
AAP 0.7.4, :meth:`DoctorReport.as_document` yields the single ``{"interpreter": ...,
"checks": [...], "ok": bool}`` document of AAP 0.7.3, and :attr:`DoctorReport.ok` is the value
``foundry doctor`` maps to exit ``0`` or ``2`` under AAP 0.4.4.

Nothing here performs I/O, reads the environment, configures logging or decides pass/warn/fail: a
probe arrives as a :class:`ProbeResult` from the caller, and the supported-interpreter policy of
AAP 0.4.1 is evaluated in exactly one place, :meth:`InterpreterInfo.current`. Importing this module
is therefore free of side effects, which is what the import-time discipline of AAP 0.4.8 requires.
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

    The values are the serialised ``status`` of the ``foundry doctor --json`` document (AAP 0.7.3)
    and the ``Status`` column of the table (AAP 0.7.4), so they are exactly these three lowercase
    words and belong to the additive-only public surface of AAP 0.4.9: they may never be renamed.
    ``WARN`` records something the operator should know about -- an unsupported platform, a missing
    socket guard -- and never changes ``foundry doctor``'s exit code (AAP 0.4.4); only ``FAIL`` on a
    required probe does.

    A ``StrEnum`` member is itself the string that is printed and serialised, so no consumer needs a
    custom JSON encoder and ``str(ProbeStatus.PASS) == "pass"`` -- the ``StrEnum`` spelling rather
    than the ``str, Enum`` one is the Python 3.11 idiom for exactly that.
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


# Ascending severity, for callers that need to compare or rank probe states. It exists to answer
# questions such as "is this worse than what I have?"; it must never be used to reorder a report,
# whose row and document order is always the order the probe runner supplied.
PROBE_STATUS_ORDER: Final[tuple[ProbeStatus, ...]] = (
    ProbeStatus.PASS,
    ProbeStatus.WARN,
    ProbeStatus.FAIL,
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """One probe's outcome inside a doctor report.

    ``name`` is the probe's stable identifier and doubles as the table's ``Probe`` cell; ``status``
    is the state the probe reached; ``detail`` is the one-line explanation printed beside it and
    serialised verbatim, so it carries no timestamp, duration or object ``repr`` and two runs on an
    unchanged machine produce identical text. ``required`` records whether a ``FAIL`` here may set
    ``foundry doctor``'s exit code to ``2`` (AAP 0.4.4); it is deliberately absent from
    :meth:`as_dict`, because a ``checks`` element of AAP 0.7.3 carries exactly ``name``, ``status``
    and ``detail``.

    The fields are trusted as given: a probe that reports nothing useful is the runner's problem,
    and this library type raises no builtin exception to reject it.
    """

    name: str
    status: ProbeStatus
    detail: str
    required: bool = True

    @property
    def is_failure(self) -> bool:
        """Return whether this probe reported ``FAIL``.

        The comparison is by identity rather than by value, so a bare ``"fail"`` string is never
        mistaken for the member.
        """
        return self.status is ProbeStatus.FAIL

    def as_row(self) -> tuple[str, str, str]:
        """Return the table's ``(Probe, Status, Detail)`` cells (AAP 0.7.4)."""
        return (self.name, self.status.value, self.detail)

    def as_dict(self) -> dict[str, str]:
        """Return this probe's ``checks`` element for the doctor document (AAP 0.7.3).

        The keys are exactly ``name``, ``status`` and ``detail``, and ``status`` is the plain string
        value rather than the enum member, so the document serialises with ``json.dumps`` and no
        custom encoder.
        """
        return {"name": self.name, "status": self.status.value, "detail": self.detail}


# AAP 0.3.1 pins ``requires-python = ">=3.11,<3.14"`` and AAP 0.4.1 supports CPython 3.11 through
# 3.13 on Linux and macOS with Windows declared unsupported. The policy is stated once here and
# evaluated once in ``InterpreterInfo.current``.
SUPPORTED_IMPLEMENTATION: Final[str] = "CPython"
MIN_SUPPORTED_VERSION: Final[tuple[int, int]] = (3, 11)
MAX_SUPPORTED_VERSION: Final[tuple[int, int]] = (3, 13)


@dataclass(frozen=True, slots=True)
class InterpreterInfo:
    """The interpreter a doctor run executes on, and whether Foundry supports it.

    ``implementation`` is the interpreter's name as reported by :mod:`platform`, ``version`` its
    ``major.minor.micro`` string, ``platform`` the :data:`sys.platform` identifier and ``supported``
    the verdict of the policy in :meth:`current`. ``supported`` is the only expression of that
    verdict: this model never decides pass/warn/fail, so an unsupported interpreter surfaces as a
    probe ``WARN`` rather than a ``FAIL`` (AAP 0.4.1), and Windows -- declared unsupported -- cannot
    fail an otherwise healthy run.
    """

    implementation: str
    version: str
    platform: str
    supported: bool

    @classmethod
    def current(cls) -> InterpreterInfo:
        """Return the running interpreter's :class:`InterpreterInfo`.

        This is the single place the supported-version policy is evaluated: ``supported`` is true
        only for ``CPython`` on ``(3, 11)`` through ``(3, 13)`` inclusive, the range AAP 0.3.1 pins
        in ``requires-python`` and AAP 0.4.1 supports. The method reads :mod:`sys` and
        :mod:`platform` only -- no subprocess, environment, filesystem or socket -- so a doctor run
        reports on the interpreter without touching the machine around it.
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
        """Return the ``interpreter`` section of the doctor document (AAP 0.7.3).

        The section carries exactly ``implementation``, ``version``, ``platform`` and ``supported``
        in that order. AAP 0.7.3 fixes the top-level ``interpreter`` key but not its contents, so
        this four-field shape is the definition and stays stable across releases.
        """
        return {
            "implementation": self.implementation,
            "version": self.version,
            "platform": self.platform,
            "supported": self.supported,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Every probe of one ``foundry doctor`` run, in the order the runner supplied it.

    ``checks`` keeps that fixed order -- it is never sorted or grouped by status -- because the
    terminal table and the JSON array are read side by side and must list the same probes in the
    same sequence. Both renderings come from this model alone: :meth:`as_rows` for the ``Probe`` /
    ``Status`` / ``Detail`` table of AAP 0.7.4 and :meth:`as_document` for the
    ``foundry doctor --json`` document of AAP 0.7.3.
    """

    interpreter: InterpreterInfo
    checks: tuple[ProbeResult, ...]

    @property
    def failed(self) -> tuple[ProbeResult, ...]:
        """Return the required probes that failed, in the order they were supplied.

        A required probe that reported ``WARN`` is not a failure, and neither is an optional probe
        that reported ``FAIL``: only ``required`` and ``FAIL`` together reach this tuple.
        """
        return tuple(check for check in self.checks if check.required and check.is_failure)

    @property
    def ok(self) -> bool:
        """Return whether no required probe failed, the value that maps to the command's exit code.

        ``True`` yields exit ``0`` and ``False`` exit ``2`` (AAP 0.4.4). A ``WARN`` -- required or
        not -- never clears this property, which is why an unsupported platform or a missing socket
        guard reports a warning without failing ``foundry doctor``.
        """
        return not self.failed

    def as_rows(self) -> tuple[tuple[str, str, str], ...]:
        """Return one ``(Probe, Status, Detail)`` row per probe, in the order supplied."""
        return tuple(check.as_row() for check in self.checks)

    def as_document(self) -> dict[str, object]:
        """Return the single ``foundry doctor --json`` document (AAP 0.7.3).

        The top-level keys appear in the order AAP 0.7.3 writes them -- ``interpreter``,
        ``checks``, ``ok`` -- and there are no others: no timestamp, no duration and no
        ``required`` flag, so two runs on an unchanged machine serialise identically.
        """
        return {
            "interpreter": self.interpreter.as_dict(),
            "checks": [check.as_dict() for check in self.checks],
            "ok": self.ok,
        }
