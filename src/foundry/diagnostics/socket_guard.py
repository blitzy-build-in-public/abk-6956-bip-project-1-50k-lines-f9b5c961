"""Read-only observation of the offline socket guard a host project publishes.

A guard announces :data:`GUARD_ATTRIBUTE` and publishes :data:`ATTEMPTS_ATTRIBUTE`;
:func:`detect_socket_guard` reads that state and :func:`check_socket_guard` renders the
``socket_guard`` row of AAP 0.7.1. Nothing patches :mod:`socket` and nothing opens a connection, not
even a loopback one (AAP 0.9.2); discovery is sorted and read-only (AAP 0.13.1); importing the
module detects nothing (AAP 0.4.8).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from foundry.diagnostics.report import ProbeResult, ProbeStatus

__all__ = [
    "ATTEMPTS_ATTRIBUTE",
    "GUARD_ATTRIBUTE",
    "PROBE_NAME",
    "SocketGuardState",
    "check_socket_guard",
    "detect_socket_guard",
]


GUARD_ATTRIBUTE: Final[str] = "FOUNDRY_SOCKET_GUARD"
"""The attribute a guard publishes to announce itself; any truthy value will do."""

ATTEMPTS_ATTRIBUTE: Final[str] = "FOUNDRY_NON_LOOPBACK_ATTEMPTS"
"""The count of attempted non-loopback connections, published by the announcing module or the guard.

A guard that publishes no count is reported as installed and unmeasured, which is a warning rather
than a pass: the count is the offline proof.
"""

PROBE_NAME: Final[str] = "socket_guard"
"""The ``name`` of the doctor probe; the runner imports it instead of repeating the string."""


@dataclass(frozen=True, slots=True)
class SocketGuardState:
    """What the host project's socket guard reported about itself.

    ``installed`` is true only when a module published :data:`GUARD_ATTRIBUTE`, ``source`` is that
    module's dotted name (``None`` when nothing published a guard) and ``non_loopback_attempts`` the
    published count, or ``None`` when the guard publishes none. Nothing is rejected, nothing raised.
    """

    installed: bool
    non_loopback_attempts: int | None
    source: str | None

    @classmethod
    def absent(cls) -> SocketGuardState:
        """Return the state of a run in which no module published a guard."""
        return cls(installed=False, non_loopback_attempts=None, source=None)

    def describe(self) -> str:
        """Return the one-line detail the doctor row shows for this state.

        The ``(s)`` spelling avoids a pluralisation branch, so the text depends on nothing but the
        module name and the integer (AAP 0.13.1).
        """
        if not self.installed:
            return "no socket guard installed"
        source = self.source if self.source is not None else "an unnamed module"
        if self.non_loopback_attempts is None:
            return f"socket guard installed in {source}"
        return (
            f"socket guard installed in {source}; "
            f"{self.non_loopback_attempts} non-loopback connection attempt(s)"
        )


def _published_attempt_count(holder: object) -> int | None:
    """Return the count ``holder`` publishes, or ``None`` when it publishes none.

    Only a genuine :class:`int` counts, since ``bool`` is an ``int`` and ``True`` would otherwise
    read as one attempt; an attribute access that raises means nothing published here (AAP 0.1.2).
    """
    try:
        published = getattr(holder, ATTEMPTS_ATTRIBUTE, None)
    except Exception:
        return None
    if isinstance(published, bool) or not isinstance(published, int):
        return None
    return published


def detect_socket_guard(modules: Mapping[str, object] | None = None) -> SocketGuardState:
    """Return the socket-guard state published anywhere in ``modules``.

    Names are visited in sorted order and the search ends at the first publisher, so the answer
    never depends on the insertion order of :data:`sys.modules`; the sorted key list is materialised
    first, so an attribute access that imports something cannot disturb the iteration. ``modules``
    defaults to :data:`sys.modules` read at call time. Returns the first published state, or
    :meth:`SocketGuardState.absent` when no module published one.
    """
    namespace = sys.modules if modules is None else modules
    for name in sorted(key for key in namespace if isinstance(key, str)):
        if name == __name__:
            continue
        module = namespace.get(name)
        if module is None:
            continue
        try:
            published = getattr(module, GUARD_ATTRIBUTE, None)
            announced = bool(published)
        except Exception:
            continue
        if not announced:
            continue
        attempts = _published_attempt_count(module)
        if attempts is None:
            attempts = _published_attempt_count(published)
        return SocketGuardState(installed=True, non_loopback_attempts=attempts, source=name)
    return SocketGuardState.absent()


def check_socket_guard(state: SocketGuardState | None = None) -> ProbeResult:
    """Return the ``socket_guard`` probe row of one doctor report (AAP 0.7.1).

    ``warn`` when no guard is installed or the guard publishes no count, ``pass`` on a count of zero
    (the offline proof), ``fail`` on a positive count. Every row is ``required=False``, so no state
    moves the exit code (AAP 0.4.4). ``state`` defaults to ``None``, meaning "detect now", which
    keeps importing the module free of work (AAP 0.4.8). Raises nothing.
    """
    resolved = detect_socket_guard() if state is None else state
    status: ProbeStatus
    if not resolved.installed or resolved.non_loopback_attempts is None:
        status = ProbeStatus.WARN
    elif resolved.non_loopback_attempts == 0:
        status = ProbeStatus.PASS
    else:
        status = ProbeStatus.FAIL
    return ProbeResult(name=PROBE_NAME, status=status, detail=resolved.describe(), required=False)
