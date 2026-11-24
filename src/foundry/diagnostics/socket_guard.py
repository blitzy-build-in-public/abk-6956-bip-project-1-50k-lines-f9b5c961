"""Read-only observation of the offline socket guard a host project publishes.

Foundry installs no socket guard and depends on none (AAP 0.2.1, AAP 0.5.1): the loopback-only
guard belongs to the project, it lives in ``examples/checkout/conftest.py`` and
``tests/conftest.py``, and the counter it keeps is what ``tests/e2e/test_offline.py`` asserts is
zero (AAP 0.14.3). What ``foundry doctor`` reports is the guard's verdict rather than its
implementation, so this module fixes the small convention a guard satisfies without importing
Foundry:

* a module in :data:`sys.modules` publishes :data:`GUARD_ATTRIBUTE` -- any truthy object, normally
  the guard object itself -- to announce that a guard is installed for the current run;
* that module, or the object published under :data:`GUARD_ATTRIBUTE`, publishes
  :data:`ATTEMPTS_ATTRIBUTE` as an integer count of non-loopback connection attempts made since the
  guard was installed.

:func:`detect_socket_guard` reads that state and :func:`check_socket_guard` renders it as the
``socket_guard`` row of ``foundry doctor`` (AAP 0.7.1). Nothing here patches :mod:`socket` and
nothing here opens a connection -- not even a loopback one -- to test connectivity, because the
suite and the runtime must open no non-loopback socket at all (AAP 0.9.2).

Discovery is read-only and deterministic: module names are visited in sorted order, the first
publisher wins, and the detail strings of :meth:`SocketGuardState.describe` carry only a dotted
module name and an integer count, so two doctor runs on an unchanged machine produce identical
text (AAP 0.13.1). A project without the example guard is an ordinary project, so its absence is a
non-required ``warn`` that leaves ``foundry doctor``'s exit code alone (AAP 0.4.4).

Importing this module detects nothing and loads nothing heavy: :func:`detect_socket_guard` runs only
when a caller asks for it, and :func:`check_socket_guard` computes its default state inside its own
body, which is the import-time discipline AAP 0.4.8 requires of the diagnostics package.
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
"""The attribute a guard publishes to announce itself.

Any truthy value will do -- a guard object, :data:`True`, a module-level placeholder -- and the
name carries Foundry's prefix only so that a reader can recognise the convention; a guard never
imports Foundry to satisfy it.
"""

ATTEMPTS_ATTRIBUTE: Final[str] = "FOUNDRY_NON_LOOPBACK_ATTEMPTS"
"""The attribute carrying the count of attempted non-loopback connections.

Published by the announcing module or by the guard object published under :data:`GUARD_ATTRIBUTE`,
whichever the guard chose. A guard that announces itself without publishing a count is reported as
installed and unmeasured, which is a warning rather than a pass: the count is the offline proof.
"""

PROBE_NAME: Final[str] = "socket_guard"
"""The ``name`` of the doctor probe these functions render.

Defined once here so that the probe runner in :mod:`foundry.diagnostics.checks` imports the stable
identifier instead of repeating the string, which is what keeps the table row and the JSON
``checks`` element of AAP 0.7.3 in agreement.
"""


@dataclass(frozen=True, slots=True)
class SocketGuardState:
    """What the host project's socket guard reported about itself.

    ``installed`` is true only when some module published :data:`GUARD_ATTRIBUTE`, and ``source``
    is that module's dotted name -- ``None`` when nothing published a guard at all.
    ``non_loopback_attempts`` is the published count of non-loopback connection attempts, or
    ``None`` when the guard publishes no count.

    The fields are trusted as given: this library type rejects nothing and raises nothing, so a
    caller can hand-build a state to render a report on a machine where no guard exists.
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

        A guard that published no count is described without one, and its sentence ends at the
        module name; the ``(s)`` spelling of the counted sentence avoids a pluralisation branch so
        the text depends on nothing but the module name and the integer (AAP 0.13.1).
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

    Only a genuine :class:`int` counts: ``bool`` is an ``int`` subclass, so a guard publishing
    ``True`` would otherwise read as one attempt. Any attribute access that raises -- a
    module-level ``__getattr__``, a property on a broken guard object -- is treated as "nothing
    published here" instead of reaching the CLI (AAP 0.1.2).
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

    Names are visited in sorted order and the search ends at the first module that publishes a
    truthy :data:`GUARD_ATTRIBUTE`, so the answer never depends on the insertion order of
    :data:`sys.modules` and a run holding two guards reports the same one every time. The sorted
    name list is materialised before any module is inspected, so an attribute access that imports
    something -- and therefore mutates :data:`sys.modules` -- cannot disturb the iteration.

    Args:
        modules: the namespace to search. It defaults to ``None``, meaning :data:`sys.modules` read
            at call time; a unit test passes a fake mapping instead and never touches the real
            module table.

    Returns:
        The first published state, or :meth:`SocketGuardState.absent` when no module published one.
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
    """Return the ``socket_guard`` probe row of one doctor report.

    The row reports what AAP 0.7.1 asks of this package: whether a guard is installed, and whether
    it saw a non-loopback connection attempt. No guard, and a guard that publishes no count, both
    report ``warn``; a count of zero is the offline proof ``tests/e2e/test_offline.py`` asserts and
    reports ``pass``; a positive count reports ``fail``.

    Every row is ``required=False``, so none of those states moves ``foundry doctor``'s exit code
    (AAP 0.4.4): the row appears in the table and in the JSON document, and a project without a
    guard still exits ``0``. This function raises nothing.

    Args:
        state: the state to render. It defaults to ``None``, meaning "detect now" -- the detection
            runs inside this call rather than at import time, which keeps importing the module free
            of work (AAP 0.4.8).

    Returns:
        The row named :data:`PROBE_NAME`, always with ``required=False``.
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
