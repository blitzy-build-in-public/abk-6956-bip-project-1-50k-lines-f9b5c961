"""Bounded per-test log capture for Foundry: a log-record ring and a redacting terminal writer.

Foundry retains at most ``logging.capture_lines`` records per test, so a failed test carries a
readable tail into ``log_tail.log`` and the failure panel (AAP 0.4.6, AAP 0.14.2). :class:`LogRing`
and :func:`format_record` store and format the ring, :func:`scrub_terminal_writer` is the terminal
sink AAP 0.14.3 names, and formatting happens on read, so two runs write identical tails.
"""

import contextlib
import logging
from collections import deque
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Final, Protocol, cast

from foundry.core.limits import CAPTURE_LINES_DEFAULT, CAPTURE_LINES_MAX, CAPTURE_LINES_MIN
from foundry.obs.events import emit_plumbing_failure
from foundry.obs.logging import LOGGER_NAME, attach_handler, detach_handler, get_logger
from foundry.obs.redaction import Redactor

__all__ = [
    "DEFAULT_CAPTURE_LINES",
    "LogRing",
    "MAX_CAPTURE_LINES",
    "MAX_TAIL_LINE_LENGTH",
    "MIN_CAPTURE_LINES",
    "TRUNCATION_MARKER",
    "format_record",
    "scrub_terminal_writer",
    "tail_lines",
]

DEFAULT_CAPTURE_LINES: Final[int] = CAPTURE_LINES_DEFAULT
"""Default ring size: :data:`foundry.core.limits.CAPTURE_LINES_DEFAULT`, the single definition site.

A second copy could drift from the surface AAP 0.4.5 publishes, and this module may import
:mod:`foundry.core` only (AAP 0.4.8).
"""

MIN_CAPTURE_LINES: Final[int] = CAPTURE_LINES_MIN
"""Smallest accepted ring size of ``logging.capture_lines``; ``0`` disables capture."""

MAX_CAPTURE_LINES: Final[int] = CAPTURE_LINES_MAX
"""Largest accepted ring size: the upper bound of ``logging.capture_lines``."""

MAX_TAIL_LINE_LENGTH: Final[int] = 2_000
"""Cap on one formatted line, in characters, the truncation marker included."""

TRUNCATION_MARKER: Final[str] = "... [truncated]"
"""ASCII suffix terminating a line that was cut at :data:`MAX_TAIL_LINE_LENGTH`."""

_MESSAGE_SEPARATOR: Final[str] = " | "
"""Substituted for every newline, so one record always occupies exactly one line."""

_UNFORMATTABLE_MESSAGE: Final[str] = "<unformattable log record>"
"""Message substituted when a record's ``%``-arguments cannot be interpolated."""

_EVENT_PREFIX: Final[str] = LOGGER_NAME + ": "
"""What a lifecycle line from :func:`foundry.obs.events.event_line` already begins with.

The prefix is the logger name plus its separator, built here from
:data:`foundry.obs.logging.LOGGER_NAME` so the two cannot drift apart, and no import outside
``foundry.obs`` and ``foundry.core`` is added to recognise it (AAP 0.4.8).
"""

_COMPONENT: Final[str] = "obs.capture"
"""The component name a plumbing warning from :func:`scrub_terminal_writer` carries, as its path."""


def _clamp_capacity(capacity: int) -> int:
    """Clamp a requested ring size into the range ``logging.capture_lines`` accepts.

    A size outside it is clamped and a non-integer gives the default, never an error.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int):
        return DEFAULT_CAPTURE_LINES
    return max(MIN_CAPTURE_LINES, min(MAX_CAPTURE_LINES, capacity))


def _join_parts(levelname: str, logger_name: str, message: str) -> str:
    """Join a record's three parts into one line, and never render the event prefix twice.

    A message that already begins with :data:`_EVENT_PREFIX` -- every lifecycle line, since
    :func:`foundry.obs.events.event_line` renders that prefix itself -- is joined as
    ``"<LEVELNAME> <message>"``; any other keeps ``"<LEVELNAME> <logger name>: <message>"``.

    Returns:
        The single, unnormalised line, which :func:`_normalise_line` then bounds.
    """
    if message.startswith(_EVENT_PREFIX):
        return f"{levelname} {message}"
    return f"{levelname} {logger_name}: {message}"


def format_record(record: logging.LogRecord, *, limit: int = MAX_TAIL_LINE_LENGTH) -> str:
    """Format one log record as a single deterministic line.

    The two shapes are :func:`_join_parts`'s, never both prefixes at once. Newlines become
    ``" | "``, past ``limit`` characters :data:`TRUNCATION_MARKER` is appended, and an
    uninterpolatable record yields a placeholder, so a capture fault never changes an outcome.
    """
    levelname, logger_name, message = _record_parts(record)
    return _normalise_line(_join_parts(levelname, logger_name, message), limit)


def _record_parts(record: logging.LogRecord) -> tuple[str, str, str]:
    """Return ``record``'s level, name and message, falling back together."""
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
    """A bounded ring holding the most recent :class:`logging.LogRecord` objects, per test.

    Its capacity is clamped rather than validated, as it may come from a hand-edited file, and
    ``level`` defaults to ``NOTSET`` so the ``foundry`` logger's level -- set by
    :func:`foundry.obs.logging.configure` before the first attach -- filters. The caller attaches it
    per test and detaches at teardown; ``BufferingHandler``'s ``flush`` would lose the tail.
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
        """Append ``record`` to the ring, discarding the oldest once it is full.

        Nothing here may raise or log, which would become a logging error on a passing test or
        re-enter this handler, so a malformed record leaves the ring as it was (AAP 0.1.2).
        """
        try:
            if record.levelno < self.level:
                return
            self._records.append(record)
        except (AttributeError, TypeError, ValueError):
            return

    def tail(self, n: int | None = None) -> tuple[str, ...]:
        """Return up to ``n`` formatted lines, oldest first.

        Returns:
            One string per selected record, newest last; ``0`` and negative values ``()``.
        """
        records = tuple(self._records)
        if n is None:
            return tuple(format_record(record) for record in records)
        return tail_lines(records, n)

    def clear(self) -> None:
        """Drop every retained record, releasing the references the ring still holds."""
        self._records.clear()

    def flush(self) -> None:
        """Retain the ring's contents: emptying it would discard the tail a failed test needs."""

    def close(self) -> None:
        """Release the ring's records and deregister it; the deque empties first."""
        self.clear()
        super().close()

    def attach(self, logger: logging.Logger | None = None) -> None:
        """Attach this ring as a handler, on the ``foundry`` logger by default.

        ``None`` means the ``foundry`` logger, whose handler list only
        :func:`foundry.obs.logging.attach_handler` may touch; any other logger is the caller's own.
        """
        if logger is None or logger is get_logger():
            attach_handler(self)
            return
        if self not in logger.handlers:
            logger.addHandler(self)

    def detach(self, logger: logging.Logger | None = None) -> None:
        """Detach this ring from the logger :meth:`attach` installed it on.

        The retained records survive detaching, so a caller can still read the tail into
        ``log_tail.log``; :meth:`clear` or :meth:`close` releases them.
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

    One string per selected record from ``format_record``, newest last; ``0`` and negative values
    yield ``()``. Shared by :meth:`LogRing.tail` and the ``user_properties`` ferry, so both agree.
    """
    if n <= 0:
        return ()
    return tuple(format_record(record) for record in records[-n:])


class _Writable(Protocol):
    write: Callable[..., None]


def scrub_terminal_writer(writer: object, redactor: Redactor) -> bool:
    """Route every ``write`` on ``writer`` through ``redactor``, so no secret reaches the terminal.

    pytest prints failures, capture sections, live log records and summaries through one
    ``TerminalWriter``, so wrapping its ``write`` puts the single engine of AAP 0.3.2 ahead of the
    sink AAP 0.14.3 names. A redaction fault writes the chunk as it stands, after one ``WARNING``,
    so it changes neither an outcome nor an exit code (AAP 0.1.2).

    Returns:
        Whether a ``write`` was found and wrapped; a value without one answers ``False``.
    """
    write = getattr(writer, "write", None)
    if not callable(write):
        return False
    faulted = False

    def scrubbed(text: str, *, flush: bool = False, **markup: bool) -> None:
        """Write one terminal chunk with the secrets the engine resolves masked."""
        nonlocal faulted
        if not faulted:
            try:
                text = redactor.text(text)
            except Exception as exc:
                # Failure-isolation rule (AAP 0.1.2): one WARNING, then the chunk as it stands, and
                # no second attempt: a warning written here would re-enter the failed engine.
                faulted = True
                with contextlib.suppress(Exception):
                    emit_plumbing_failure(
                        _COMPONENT, "could not redact a line of terminal output", exc=exc
                    )
        write(text, flush=flush, **markup)

    cast("_Writable", writer).write = scrubbed
    return True
