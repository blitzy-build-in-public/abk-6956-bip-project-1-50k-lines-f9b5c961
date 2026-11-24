"""Bounded per-test log capture for Foundry: a ring of the last N ``logging`` records.

Foundry retains at most ``logging.capture_lines`` records per test so that a failed test can
carry a readable log tail into ``log_tail.log``, into the result record's ``artifacts`` list and
into the report's failure panel (AAP 0.4.6; AAP 0.14.2 budgets the tail at "200 lines, from
``logging.capture_lines``"). :class:`LogRing` is the storage -- a :class:`logging.Handler` over a
:class:`collections.deque` bounded at construction -- and :func:`format_record` is the one
formatter, so the artifact, the report panel and the ``user_properties`` ferry cannot render the
same record three different ways.

Bounded on both axes
    The ring keeps at most ``capacity`` records, dropping the oldest first, and
    :func:`format_record` caps one line at :data:`MAX_TAIL_LINE_LENGTH` characters. A pathological
    record therefore cannot make an artifact unbounded, which is the bounded-memory requirement
    this subtree is held to.

Formatted lazily, never timestamped
    Records are stored as :class:`logging.LogRecord` objects and formatted only when read, so a
    run that never inspects the tail pays nothing for it and a message built from ``%``-arguments
    is rendered with its final values. :func:`format_record` emits ``"<LEVEL> <name>: <message>"``
    -- the shape of :data:`foundry.obs.logging.LOG_FORMAT` -- with no timestamp, no millisecond
    field, no host name, no thread name and no memory address, so two runs of the same suite
    produce byte-identical tails (AAP 0.14.3, "Deterministic output"). A multi-line message is
    collapsed onto one line, so the tail's line count equals its record count.

The capacity belongs to the caller, never to this module
    ``logging.capture_lines`` is resolved by ``foundry.execution.session``, which constructs
    ``LogRing(capacity=...)``. This module reads no environment variable and imports no
    configuration, because ``foundry.obs`` may import ``foundry.core`` only (AAP 0.4.8); the default
    and the bounds it clamps into come from :mod:`foundry.core.limits`, the single definition site
    of the ``logging.capture_lines`` numbers. A ``capacity`` of ``0`` is a supported configuration:
    the ring retains nothing, :meth:`LogRing.tail` returns ``()`` and no ``log_tail.log`` artifact
    is produced.

What this module deliberately does not do
    * It does not capture stdout or stderr. pytest's own capture machinery already does, and
      ``foundry.obs.artifacts`` persists ``report.capstdout`` and ``report.capstderr``. Swapping
      ``sys.stdout`` or ``sys.stderr`` here would break that capture, which is why
      :class:`LogRing` touches neither stream.
    * It starts no thread, installs no global filter and reconfigures no logger other than the
      one :meth:`LogRing.attach` is given. Importing this module attaches nothing: the single
      import-time handler in this package is the ``NullHandler`` that ``foundry.obs.logging``
      owns.
    * It holds no module-level state and no module-level :class:`contextvars.ContextVar`.
      Correlation travels as the nodeid a caller passes to ``foundry.obs.events``, and the ring
      itself is the per-test context, owned by ``item.stash`` behind a typed
      ``pytest.StashKey`` because AAP 0.4.8 forbids module-level state.
    * It never raises out of :meth:`LogRing.emit`. A capture fault is silent and changes neither a
      test's outcome nor an exit code (failure isolation, AAP 0.1.2); in particular ``emit`` does
      not call :meth:`logging.Handler.handleError`, whose traceback on ``stderr`` would pollute a
      test's captured output.

Typical use::

    ring = LogRing(capacity=capture_lines)
    with ring:
        ...  # run the test
    lines = ring.tail(capture_lines)
"""

import logging
from collections import deque
from collections.abc import Sequence
from types import TracebackType
from typing import Final

from foundry.core.limits import CAPTURE_LINES_DEFAULT, CAPTURE_LINES_MAX, CAPTURE_LINES_MIN
from foundry.obs.logging import attach_handler, detach_handler, get_logger

__all__ = [
    "DEFAULT_CAPTURE_LINES",
    "LogRing",
    "MAX_CAPTURE_LINES",
    "MAX_TAIL_LINE_LENGTH",
    "MIN_CAPTURE_LINES",
    "TRUNCATION_MARKER",
    "format_record",
    "tail_lines",
]

DEFAULT_CAPTURE_LINES: Final[int] = CAPTURE_LINES_DEFAULT
"""Default ring size: the default of the ``logging.capture_lines`` configuration key.

The number is defined once, in :data:`foundry.core.limits.CAPTURE_LINES_DEFAULT`, because this
module may not import :mod:`foundry.config` (AAP 0.4.8) and a second copy is what lets a default
drift from the surface AAP 0.4.5 publishes.
"""

MIN_CAPTURE_LINES: Final[int] = CAPTURE_LINES_MIN
"""Smallest accepted ring size: the lower bound of ``logging.capture_lines``.

``0`` is a clean no-capture state rather than an error. Defined once, in
:data:`foundry.core.limits.CAPTURE_LINES_MIN`.
"""

MAX_CAPTURE_LINES: Final[int] = CAPTURE_LINES_MAX
"""Largest accepted ring size: the upper bound of ``logging.capture_lines``.

Defined once, in :data:`foundry.core.limits.CAPTURE_LINES_MAX`.
"""

MAX_TAIL_LINE_LENGTH: Final[int] = 2_000
"""Cap on one formatted line, in characters, the truncation marker included."""

TRUNCATION_MARKER: Final[str] = "... [truncated]"
"""ASCII suffix terminating a line that was cut at :data:`MAX_TAIL_LINE_LENGTH`."""

_MESSAGE_SEPARATOR: Final[str] = " | "
"""Substituted for every newline, so one record always occupies exactly one line."""

_UNFORMATTABLE_MESSAGE: Final[str] = "<unformattable log record>"
"""Message substituted when a record's ``%``-arguments cannot be interpolated."""


def _clamp_capacity(capacity: int) -> int:
    """Clamp a requested ring size into the range ``logging.capture_lines`` accepts.

    Args:
        capacity: The requested size. It arrives from a hand-editable configuration file, so it
            may be negative, larger than :data:`MAX_CAPTURE_LINES`, or not an integer at all.

    Returns:
        A size between :data:`MIN_CAPTURE_LINES` and :data:`MAX_CAPTURE_LINES`, inclusive. A
        value that is not an integer falls back to :data:`DEFAULT_CAPTURE_LINES` rather than
        raising, because a malformed capture setting must never fail a run.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        return DEFAULT_CAPTURE_LINES
    return max(MIN_CAPTURE_LINES, min(MAX_CAPTURE_LINES, capacity))


def format_record(record: logging.LogRecord, *, limit: int = MAX_TAIL_LINE_LENGTH) -> str:
    """Format one log record as a single deterministic line.

    Args:
        record: The record to format. Formatting happens here, not at capture time, so a message
            assembled from ``%``-arguments shows the values its arguments hold now.
        limit: Maximum length of the returned line in characters, the truncation marker included.
            A non-positive value disables truncation.

    Returns:
        ``"<LEVELNAME> <logger name>: <message>"``, the shape of
        :data:`foundry.obs.logging.LOG_FORMAT` without the timestamp and millisecond fields a
        :class:`logging.Formatter` would add, so two runs of the same suite format a given record
        identically. Every newline is replaced by ``" | "`` so one record occupies one line, and a
        line longer than ``limit`` is cut and terminated with :data:`TRUNCATION_MARKER`.

    A record whose ``%``-arguments cannot be interpolated -- a hand-built record rather than one
    the :mod:`logging` machinery created -- yields a placeholder message naming its level and
    logger instead of raising, so a capture fault can never change a test's outcome.
    """
    levelname, logger_name, message = _record_parts(record)
    return _normalise_line(f"{levelname} {logger_name}: {message}", limit)


def _record_parts(record: logging.LogRecord) -> tuple[str, str, str]:
    """Return ``record``'s level name, logger name and interpolated message.

    The three are extracted together because the failure mode is shared: a record whose ``msg``
    or ``args`` cannot be interpolated has to fall back as a whole, and the fallback still names
    the record so the tail says something useful about where it came from.
    """
    try:
        return str(record.levelname), str(record.name), record.getMessage()
    except (AttributeError, TypeError, ValueError):
        levelname = str(getattr(record, "levelname", "") or "LEVEL")
        logger_name = str(getattr(record, "name", "") or "foundry")
        return levelname, logger_name, _UNFORMATTABLE_MESSAGE


def _normalise_line(line: str, limit: int) -> str:
    """Collapse ``line`` onto one physical line and truncate it to ``limit`` characters."""
    collapsed = line.replace("\r\n", _MESSAGE_SEPARATOR)
    collapsed = collapsed.replace("\n", _MESSAGE_SEPARATOR).replace("\r", _MESSAGE_SEPARATOR)
    if limit <= 0 or len(collapsed) <= limit:
        return collapsed
    if limit <= len(TRUNCATION_MARKER):
        return collapsed[:limit]
    return collapsed[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


class LogRing(logging.Handler):
    """A bounded ring holding the most recent :class:`logging.LogRecord` objects.

    Args:
        capacity: How many records to retain. The value is clamped into
            ``[MIN_CAPTURE_LINES, MAX_CAPTURE_LINES]`` rather than validated, because it arrives
            from a hand-editable configuration file and an out-of-range ring must not raise;
            ``0`` retains nothing.
        level: The handler level. Records below it are dropped by :meth:`emit`; the default
            ``NOTSET`` lets the ``foundry`` logger's own level -- set by
            ``foundry.obs.logging.configure`` -- be the effective filter, which is why that call
            precedes the first attach in the session bootstrap.

    The ring is a :class:`logging.Handler` so it receives records by being attached to a logger,
    normally the ``foundry`` logger through :func:`foundry.obs.logging.attach_handler`, and it
    holds nothing beyond a :class:`collections.deque` bounded at construction. It is a per-test
    object: a caller attaches it before the test and calls :meth:`detach` and :meth:`clear` at
    teardown, so neither a record nor a large object a record's ``args`` points at outlives the
    test that produced it.

    ``logging.handlers.BufferingHandler`` is the same shape but the wrong class here: its
    ``flush`` empties the buffer wherever a handler decides to flush, which would silently lose
    the tail this class exists to keep.
    """

    def __init__(
        self, capacity: int = DEFAULT_CAPTURE_LINES, *, level: int = logging.NOTSET
    ) -> None:
        """Build a ring that retains at most ``capacity`` of the newest records."""
        super().__init__(level=level)
        self._capacity = _clamp_capacity(capacity)
        self._records: deque[logging.LogRecord] = deque(maxlen=self._capacity)

    @property
    def capacity(self) -> int:
        """Return how many records the ring retains; ``0`` means capture is disabled."""
        return self._capacity

    @property
    def records(self) -> tuple[logging.LogRecord, ...]:
        """Return the retained records, oldest first."""
        return tuple(self._records)

    def __len__(self) -> int:
        """Return how many records the ring currently holds."""
        return len(self._records)

    def emit(self, record: logging.LogRecord) -> None:
        """Append ``record`` to the ring, discarding the oldest record once it is full.

        Nothing here may raise, and nothing here may log: an exception escaping ``emit`` would
        surface as a logging error on a test that may otherwise pass, and logging from inside a
        handler attached to the ``foundry`` logger would re-enter this same handler. A malformed
        record -- one missing ``levelno``, or an ``args`` the deque cannot reference -- leaves the
        ring exactly as it was and returns.
        """
        try:
            if record.levelno < self.level:
                return
            self._records.append(record)
        except (AttributeError, TypeError, ValueError):
            return

    def tail(self, n: int | None = None) -> tuple[str, ...]:
        """Return up to ``n`` formatted lines, oldest first.

        Args:
            n: How many of the newest lines to return. ``None`` returns every retained line, ``0``
                and any negative value return ``()``, and a value larger than the ring returns
                what is there without raising.

        Returns:
            One string per record, formatted by :func:`format_record`, newest last so a report
            panel reads chronologically.
        """
        records = tuple(self._records)
        if n is None:
            return tuple(format_record(record) for record in records)
        return tail_lines(records, n)

    def clear(self) -> None:
        """Drop every retained record, releasing the references the ring still holds."""
        self._records.clear()

    def flush(self) -> None:
        """Retain the ring's contents: this handler has nothing to flush.

        The base contract exists for buffered handlers that push bytes to a stream. The ring
        pushes nothing, and emptying it here -- as ``logging.handlers.BufferingHandler.flush``
        does -- would discard exactly the tail a failed test needs, so
        ``logging.shutdown``'s flush leaves the ring untouched.
        """

    def close(self) -> None:
        """Release the ring's records and deregister it from the logging machinery.

        The deque is emptied first so that a closed ring references no ``LogRecord`` even if the
        base implementation raises while removing this handler from the module-level handler
        registry.
        """
        self.clear()
        super().close()

    def attach(self, logger: logging.Logger | None = None) -> None:
        """Attach this ring as a handler, on the ``foundry`` logger by default.

        Args:
            logger: The logger to attach to. ``None`` means the ``foundry`` logger, whose handler
                list is modified only through :func:`foundry.obs.logging.attach_handler` so that
                it has exactly one owner. Any other logger is a caller's own -- a unit test
                capturing a private logger, for instance -- and is attached directly, since
                ``foundry.obs.logging`` deliberately governs the ``foundry`` logger alone.

        Attaching twice changes nothing, and no other logger is touched in either case.
        """
        if logger is None or logger is get_logger():
            attach_handler(self)
            return
        if self not in logger.handlers:
            logger.addHandler(self)

    def detach(self, logger: logging.Logger | None = None) -> None:
        """Detach this ring from the logger :meth:`attach` installed it on.

        Args:
            logger: The logger to detach from, resolving to the same target :meth:`attach` uses.
                Detaching from a logger that never had this ring, or detaching twice, is a no-op.

        The retained records survive detaching, so a caller can detach and still read the tail
        into ``log_tail.log`` afterwards; :meth:`clear` or :meth:`close` is what releases them.
        """
        if logger is None or logger is get_logger():
            detach_handler(self)
            return
        if self in logger.handlers:
            logger.removeHandler(self)

    def __enter__(self) -> "LogRing":
        """Attach the ring to the ``foundry`` logger and return it."""
        self.attach()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Detach the ring, leaving any exception raised inside the block to propagate."""
        self.detach()


def tail_lines(records: Sequence[logging.LogRecord], n: int) -> tuple[str, ...]:
    """Format the last ``n`` of ``records``, oldest first.

    Args:
        records: The records to format, in the order they were logged.
        n: How many of the newest records to format. ``0`` and negative values yield ``()``, and
            a value larger than ``len(records)`` formats every record without raising.

    Returns:
        One string per selected record, produced by :func:`format_record`, the newest last.

    This is the pure helper :meth:`LogRing.tail` and the ``user_properties`` ferry in
    ``foundry.execution.state`` share, so the format a worker ships across the xdist boundary and
    the tail a report panel renders are produced by the same code and cannot drift.
    """
    if n <= 0:
        return ()
    return tuple(format_record(record) for record in records[-n:])
