"""The ``foundry_log_capture`` fixture: one bounded ``foundry`` log ring per test.

``foundry_log_capture`` is function scoped and yields a :class:`FoundryLogCapture` over a
:class:`foundry.obs.capture.LogRing` attached to the ``foundry`` logger for the length of one test,
so a test asserts on the lines its own code logged and on nothing another test logged::

    logging.getLogger("foundry.http").warning("retrying")
    assert foundry_log_capture.tail(1) == ("WARNING foundry.http: retrying",)

**Three readers of one ring.** :meth:`FoundryLogCapture.tail` is the fixture's view of the same
bounded ring the report's last-N-lines failure panel and the ``log_tail.log`` artifact read, and
:func:`foundry.obs.capture.format_record` is the only formatter, so a test asserting on
``capture.tail(2)`` asserts on the text a failing run would have persisted. A line carries the
level, the logger name and the message only -- no timestamp, thread, host or address -- which is
what makes two runs' tails identical.

**Capacity and attachment.** The ring retains exactly ``logging.capture_lines`` records (default
``200``, range ``0..10000``) read through ``foundry_config``, and no second list is kept beside it;
``capture_lines = 0`` attaches no handler at all, so the capture reports no records. Setup attaches
the ring through :func:`foundry.obs.logging.attach_handler` and teardown detaches it; ``propagate``
is never touched, which is why pytest's ``caplog`` keeps seeing every record.

**Failure isolation.** A fault while attaching or detaching changes neither the test's outcome nor
the run's exit code: setup yields a usable, empty capture, teardown never raises, and the fault is
one ``WARNING`` through :func:`foundry.obs.events.emit_plumbing_failure` after the ring is
detached. Module scope imports :mod:`pytest`, the standard library and :mod:`foundry.core`; the ring
and the emitter are imported inside the fixture body, outside the plugin's 150 ms startup budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import pytest

from foundry.core.exceptions import FoundryError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from foundry.config.models import FoundryConfig
    from foundry.obs.capture import LogRing

__all__ = ["FoundryLogCapture", "foundry_log_capture"]

_PREFIX: Final[str] = "foundry.fixtures.logs"
"""Prefix of every message this module raises, naming the module a reader has to correct."""

_COMPONENT: Final[str] = "log_capture"
"""Component label the plumbing-failure lines carry, so a reader sees which fixture failed."""

_ATTACH_FAULT: Final[str] = (
    "could not attach the log-capture ring; this test captured no foundry log records"
)
"""Reported at ``WARNING`` when the ring could not be attached; the test still runs to the end."""

_DETACH_FAULT: Final[str] = (
    "could not detach the log-capture ring; it may keep capturing records for later tests"
)
"""Reported at ``WARNING`` when the ring could not be detached; the run's exit code is unchanged."""


@dataclass(frozen=True, slots=True)
class FoundryLogCapture:
    """The bounded ``foundry`` log ring one test reads, and the operations it offers.

    An instance belongs to one test: the fixture builds a fresh ring per item and detaches it at
    teardown, so a test that logged cannot affect any other test or xdist worker. The class reads
    the ``foundry`` logger only -- never root logging, and never pytest's ``caplog``, which an
    ordinary test keeps using beside it. The ring holds :class:`logging.LogRecord` objects exactly
    as they were emitted; formatting happens when :meth:`tail` reads, through the one formatter the
    ``log_tail.log`` artifact and the report's failure panel also use, so an assertion here and a
    persisted tail cannot disagree. Frozen and slotted: the capture is a handle onto the ring, so
    nothing about it is assignable, and a test that wants to forget what it has seen calls
    :meth:`clear`.
    """

    ring: LogRing
    """The ring the fixture attached and detaches; exposed so a test can read its ``capacity``."""

    @property
    def capacity(self) -> int:
        """Return how many records the ring retains, which is ``logging.capture_lines``.

        Returns:
            The resolved capacity, ``0`` when capture is disabled and ``10000`` at most.
        """
        return self.ring.capacity

    @property
    def records(self) -> tuple[logging.LogRecord, ...]:
        """Return the retained records, oldest first, as an immutable snapshot.

        A tuple rather than the ring's own storage, so a caller cannot append to or reorder what the
        ring holds and a record emitted after the call never changes what the caller read.

        Returns:
            Zero or more records, in the order they were emitted; ``()`` when nothing has been
            logged yet or when ``logging.capture_lines`` is ``0``.
        """
        return tuple(self.ring.records)

    def tail(self, n: int) -> tuple[str, ...]:
        """Return up to ``n`` of the newest records, formatted one line each, oldest first.

        The lines are the ones ``log_tail.log`` would hold and the report's failure panel would
        show: level, logger name and message, with a multi-line message collapsed onto one line and
        a very long line truncated. They carry no timestamp, so two runs of one test produce the
        same tuple.

        Args:
            n: How many of the newest records to format; ``0`` asks for nothing, which is a question
                with an empty answer rather than a fault.

        Returns:
            Up to ``n`` strings, the newest last; ``()`` when nothing has been captured.

        Raises:
            FoundryError: If ``n`` is not a non-negative integer. The message names the argument,
                because a negative count is a caller's mistake rather than a capture that found no
                records, and the fixture's contract does not let a ``ValueError`` cross the module
                boundary.
        """
        if not isinstance(n, int) or n < 0:
            raise FoundryError(
                f"{_PREFIX}: tail() requires a non-negative record count, got n={n!r}",
                hint="pass how many of the newest lines to read, for example tail(20)",
            )
        return self.ring.tail(n)

    def clear(self) -> None:
        """Drop every record the ring holds, so a later assertion sees only what follows it.

        The next phase of a test -- a step whose own log lines are what it asserts on -- calls this
        and then reads :attr:`records` or :meth:`tail` without the noise the setup emitted. The
        ring keeps its capacity and its attachment: clearing is not a detach.
        """
        self.ring.clear()

    def __len__(self) -> int:
        """Return how many records the ring currently holds.

        Returns:
            The record count, never more than :attr:`capacity`.
        """
        return len(self.ring)


def _attach(ring: LogRing) -> BaseException | None:
    """Attach ``ring`` to the ``foundry`` logger, returning the fault instead of raising it.

    A capture fault must change neither a test's outcome nor the run's exit code, so the caller
    decides what to report and when, rather than the attach path deciding by unwinding through
    pytest's fixtures. The fault is returned whole; only :func:`_report` decides how much of it to
    publish.

    Returns:
        ``None`` once the ring is attached, otherwise the fault the attempt raised.
    """
    try:
        ring.attach()
    except Exception as error:  # a plumbing fault is reported at WARNING, never raised (AAP 0.1.2)
        return error
    return None


def _detach(ring: LogRing) -> BaseException | None:
    """Detach ``ring`` from the ``foundry`` logger, returning the fault instead of raising it.

    Teardown runs on the failing path and the interrupted path as well as the passing one, and a
    remove-handler fault there would replace a test's real failure with a capture failure, so the
    fault is returned for :func:`_report` rather than raised.

    Returns:
        ``None`` when the ring is no longer attached, otherwise the fault the attempt raised.
    """
    try:
        ring.detach()
    except Exception as error:  # teardown never raises; the run's exit code stays as it was
        return error
    return None


def _report(message: str, fault: BaseException) -> None:
    """Report ``fault`` as one ``WARNING`` line, called only once the ring is detached.

    :func:`foundry.obs.events.emit_plumbing_failure` is the single emitter for a fault in Foundry's
    own plumbing: it caps the level at ``WARNING``, sorts the line's fields and swallows its own
    faults, so a capture that has already failed once cannot fail the run on the way out.

    Args:
        message: What could not be done, phrased for the reader who has to fix it.
        fault: The fault being reported. Only its class name reaches the line -- never its message,
            which could carry a path or a header value.
    """
    from foundry.obs.events import emit_plumbing_failure

    emit_plumbing_failure(_COMPONENT, message, exc=fault)


@pytest.fixture
def foundry_log_capture(foundry_config: FoundryConfig) -> Iterator[FoundryLogCapture]:
    """Yield the bounded ``foundry`` log ring for this test, attached for the test's duration.

    Function scoped: every test that requests the fixture gets a fresh ring, sized from
    ``logging.capture_lines`` of the resolved configuration, and the ring is detached at teardown so
    nothing one test logged reaches the next one or another xdist worker. The capture window is the
    test's own body together with its fixtures' setup and teardown, which is what makes
    ``capture.tail(n)`` a complete answer to "what did the code under test log".
    ``logging.capture_lines = 0`` disables capture: the ring retains nothing and is not attached at
    all, so the yielded capture reports no records and the ``foundry`` logger's handler list is left
    exactly as Foundry's own wiring made it.

    Yields:
        A :class:`FoundryLogCapture` over the attached ring. When the ring could not be attached the
        yielded capture reads an empty ring -- ``records`` and ``tail(n)`` answer ``()`` -- and the
        fault is reported at ``WARNING`` as teardown ends, leaving the test's outcome untouched.
    """
    # Imported inside the fixture rather than at module scope: pytest imports `foundry.plugin` at
    # every start and the ring is needed only by the tests that ask for it (I5, AAP 0.14.2).
    from foundry.obs.capture import LogRing

    ring = LogRing(capacity=foundry_config.logging.capture_lines)
    # A ring of capacity 0 retains nothing, so attaching it would install a handler that can never
    # answer a test: capture is disabled and the logger's handler list stays exactly as it was.
    attach_fault: BaseException | None = None
    if ring.capacity > 0:
        attach_fault = _attach(ring)
    attached = ring.capacity > 0 and attach_fault is None

    try:
        yield FoundryLogCapture(ring)
    finally:
        # Detach first and report afterwards: a record emitted while the ring was still attached
        # would be captured by the very ring that just failed, and the WARNING belongs in front of
        # the reader rather than at the bottom of the tail it explains.
        detach_fault = _detach(ring) if attached else None
        if attach_fault is not None:
            _report(_ATTACH_FAULT, attach_fault)
        if detach_fault is not None:
            _report(_DETACH_FAULT, detach_fault)
