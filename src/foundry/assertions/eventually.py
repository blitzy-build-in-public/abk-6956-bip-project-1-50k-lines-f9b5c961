"""Polling helper: call a function until it succeeds, or until the deadline passes.

:func:`eventually` calls ``fn`` until it returns a truthy value, which it returns unchanged; falsy
values and raised exceptions keep it polling, and an expired deadline raises ``AssertionError``
naming the last outcome and the poll count (AAP 0.4.3). Time comes from the monotonic clock or an
injected clock, and every wait goes through the injected ``sleep`` (AAP 0.7.1, AAP 0.14.3).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import Final, TypeVar

from foundry.core.types import ClockLike, Sleep
from foundry.obs.redaction import MAX_EXCERPT_BYTES, Redactor, default_redactor

__all__ = ["eventually"]

T = TypeVar("T")
"""The value the polled callable returns, preserved unchanged through :func:`eventually`."""

_PREFIX: Final[str] = "foundry.assertions.eventually"
"""Prefix of every message this module produces, which names the helper a reader must fix."""

_POLL_BUDGET_MARGIN: Final[int] = 2
"""Polls the safety net allows beyond ``timeout / interval`` before the clock is called stalled.

Two covers the poll whose own duration carries the clock past the deadline and the poll that
observes it, so a healthy run never touches the budget and a frozen clock still stops promptly.
"""


class _Missing:
    """Type of the sentinel that marks "no value was ever returned by the polled callable".

    A private sentinel, not ``None``, because ``None`` is a legitimate falsy result the failure
    message has to be able to report as ``last value: None``.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Return the sentinel's spelling; no user-facing message interpolates it."""
        return "<missing>"


_MISSING: Final[_Missing] = _Missing()
"""The one sentinel instance; compared with ``is`` and never with ``==``."""


def _misuse(detail: str) -> AssertionError:
    """Build the assertion raised when :func:`eventually` is called with an unusable argument."""
    return AssertionError(f"{_PREFIX}: {detail}")


def _format_seconds(value: float) -> str:
    """Render a number of seconds the way every message in this module renders it.

    ``%g`` style, so a deadline reads ``0.5`` rather than ``0.5000000000000001``: the figure comes
    from configuration or a monotonic difference, and neither may leak noise into a message.
    """
    return f"{value:g}"


def _poll_word(polls: int) -> str:
    """Render a poll count with the noun agreeing with it.

    ``timeout=0`` polls exactly once, so a fixed plural would read ``1 polls`` on the most easily
    reproduced failure there is, in a message a person reads and a test matches.
    """
    return f"{polls} poll" if polls == 1 else f"{polls} polls"


def _as_finite_seconds(value: object) -> float | None:
    """Return ``value`` as a finite ``float``, or ``None`` when it is not a usable duration.

    Only ``int`` and ``float`` are accepted: ``bool`` is rejected although it subclasses ``int``,
    and so are ``NaN`` and the infinities, as they would poison every comparison the loop makes.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _require_timeout(timeout: object, redactor: Redactor | None = None) -> float:
    """Validate the deadline argument and return it as a ``float``.

    ``timeout`` must be a finite number of seconds of zero or more; zero is accepted and means
    "call the function once, then fail". ``redactor`` scrubs the rejected value the message quotes.

    Raises:
        AssertionError: When ``timeout`` is not a finite real number or is negative.
    """
    number = _as_finite_seconds(timeout)
    if number is None or number < 0.0:
        quoted = _excerpt(repr(timeout), redactor)
        raise _misuse(f"timeout must be a finite number of seconds >= 0, got {quoted}")
    return number


def _require_interval(interval: object, redactor: Redactor | None = None) -> float:
    """Validate the poll interval argument and return it as a ``float``.

    ``interval`` must be a finite number of seconds greater than zero: a zero interval would spin
    the loop without ever yielding, and a negative one would make the sleep clamp meaningless.

    Raises:
        AssertionError: When ``interval`` is not a finite real number or is not positive.
    """
    number = _as_finite_seconds(interval)
    if number is None or number <= 0.0:
        quoted = _excerpt(repr(interval), redactor)
        raise _misuse(f"interval must be a finite number of seconds > 0, got {quoted}")
    return number


def _require_pollable(fn: Callable[[], T]) -> Callable[[], T]:
    """Validate the polled callable, returning ``fn`` itself for the caller to use.

    Raises:
        AssertionError: When ``fn`` is not callable.
    """
    if not callable(fn):
        raise _misuse(f"fn must be callable, got {type(fn).__name__}")
    return fn


def _require_sleep(sleep: Sleep) -> Sleep:
    """Validate the injected sleeper, returning it for the caller to use.

    Checked here rather than at the first wait, so a mistake in a test's fixture fails at the top
    of the call with the same ``AssertionError`` as every other misuse.

    Raises:
        AssertionError: When ``sleep`` is not callable.
    """
    if not callable(sleep):
        raise _misuse(f"sleep must be callable, got {type(sleep).__name__}")
    return sleep


def _clock_misuse(clock: object) -> AssertionError:
    """Build the assertion raised when the supplied clock breaks the time-source contract.

    One builder serves both halves -- a reader that is not callable and a reading that is not a
    ``datetime`` -- so the message has a single source. The message names the caller's type.
    """
    return _misuse(f"clock must provide now() returning a datetime, got {type(clock).__name__}")


def _require_clock(clock: ClockLike | None) -> ClockLike | None:
    """Validate the supplied time source, returning it or ``None`` for the monotonic clock.

    Raises:
        AssertionError: When ``clock`` is neither ``None`` nor an object exposing a callable
            ``now()`` that returns a ``datetime``. This function checks that the reader is
            present; :func:`_read_instant` enforces the returned type on every read.
    """
    if clock is None:
        return None
    if not callable(getattr(clock, "now", None)):
        raise _clock_misuse(clock)
    return clock


def _read_instant(clock: ClockLike) -> datetime:
    """Read the supplied clock once and require the reading to be a ``datetime``.

    The reading is validated here rather than once at the top of :func:`eventually` because a
    clock can change what it returns between calls: one that answers with a ``datetime`` for
    the baseline and a number afterwards would otherwise leak an ``AttributeError`` or a
    ``TypeError`` from inside the poll loop, breaking the rule that :class:`AssertionError` is
    the only type this module raises (AAP 0.13.1).

    Args:
        clock: The supplied time source, already validated as exposing a callable ``now()``.

    Returns:
        The instant ``clock.now()`` returned.

    Raises:
        AssertionError: When ``now()`` returns anything other than a :class:`datetime.datetime`.
    """
    reading = clock.now()
    if not isinstance(reading, datetime):
        raise _clock_misuse(clock)
    return reading


def _seconds_from_now(clock: ClockLike | None) -> Callable[[], float]:
    """Take the baseline and return a callable measuring seconds elapsed since it.

    :class:`foundry.core.types.ClockLike` exposes ``now() -> datetime`` rather than a plain seconds
    counter, so the baseline is captured here as the instant the call started and each later reading
    is turned into a difference. ``time.monotonic`` is the default source because it cannot go
    backwards when the machine's wall clock is adjusted. Every reading is taken through
    :func:`_read_instant`, so a clock that stops returning a ``datetime`` raises this module's own
    :class:`AssertionError` instead of a builtin one.

    Args:
        clock: The supplied time source, or ``None`` to measure on the monotonic clock.

    Returns:
        A zero-argument callable returning the seconds elapsed since the call, as a ``float``.

    Raises:
        AssertionError: When ``clock.now()`` returns anything other than a ``datetime``, at
            the baseline or on any later read.
    """
    if clock is None:
        monotonic_origin = time.monotonic()
        return lambda: time.monotonic() - monotonic_origin
    instant_origin = _read_instant(clock)
    return lambda: (_read_instant(clock) - instant_origin).total_seconds()


def _one_line(text: str) -> str:
    """Collapse ``text`` to a single line with runs of whitespace reduced to one space.

    A failure message is one line by contract, while the value it quotes is arbitrary: an
    exception's ``str`` may be a formatted block and a polled payload may be pretty-printed JSON.

    Args:
        text: The text to flatten.

    Returns:
        The flattened text, without leading or trailing whitespace.
    """
    return " ".join(text.split())


def _excerpt(rendered: str, redactor: Redactor | None = None) -> str:
    """Redact, bound and flatten a rendered outcome for inclusion in a message.

    Redaction runs before truncation, inside :meth:`foundry.obs.redaction.Redactor.text`, so a
    secret straddling the cut cannot leave its prefix behind; the result is one line, bounded by
    :data:`foundry.obs.redaction.MAX_EXCERPT_BYTES` plus its truncation marker.
    """
    engine = default_redactor() if redactor is None else redactor
    excerpt = engine.body_excerpt(rendered, limit=MAX_EXCERPT_BYTES)
    return _one_line(excerpt)


def _render_value(value: object) -> str:
    """Render a falsy polled value for the failure message.

    ``repr`` rather than ``str``, so ``None``, ``False``, ``0`` and ``""`` stay distinguishable;
    a value whose ``repr`` raises is named by its type.
    """
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _outcome_text(
    last_error: Exception | None, last_value: object, redactor: Redactor | None = None
) -> str:
    """Render the trailing clause of a failure message, naming the last poll outcome.

    Exactly one of the two outcomes is set once the loop has polled once, and the two are mutually
    exclusive because a poll either returns or raises. The result is ``last exception
    <Type>(<message>)`` or ``last value: <repr>``, redacted and bounded.
    """
    if last_error is not None:
        rendered = f"{type(last_error).__name__}({last_error!s})"
        return f"last exception {_excerpt(rendered, redactor)}"
    if last_value is _MISSING:
        return "no poll completed"
    return f"last value: {_excerpt(_render_value(last_value), redactor)}"


def eventually(
    fn: Callable[[], T],
    *,
    timeout: float,
    interval: float,
    sleep: Sleep = time.sleep,
    clock: ClockLike | None = None,
    redactor: Redactor | None = None,
) -> T:
    """Poll ``fn`` until it returns a truthy value, or raise once the deadline passes.

    Each poll calls ``fn`` with no arguments. When the deadline passes, or the poll budget ends a
    stalled clock, ``AssertionError`` names the last outcome and the poll count (AAP 0.4.3);
    ``clock`` and ``redactor`` are additive over AAP 0.4.3 (AAP 0.4.9).
    """
    poll = _require_pollable(fn)
    limit = _require_timeout(timeout, redactor)
    period = _require_interval(interval, redactor)
    wait = _require_sleep(sleep)
    source = _require_clock(clock)

    elapsed_seconds = _seconds_from_now(source)
    poll_budget = math.ceil(limit / period) + _POLL_BUDGET_MARGIN

    polls = 0
    stalled_polls = 0
    last_elapsed = 0.0
    last_value: object = _MISSING
    last_error: Exception | None = None

    while True:
        polls += 1
        try:
            value = poll()
        except Exception as error:
            # A polled exception is an outcome to report, not a fault of this helper: the wait
            # continues, and the failure message quotes the last one if it never succeeds.
            last_error = error
            last_value = _MISSING
        else:
            if value:
                return value
            last_error = None
            last_value = value

        elapsed = elapsed_seconds()
        if elapsed >= limit:
            raise AssertionError(
                f"{_PREFIX}: timed out after {_format_seconds(limit)}s and {_poll_word(polls)} "
                f"(interval={_format_seconds(period)}s): "
                f"{_outcome_text(last_error, last_value, redactor)}"
            )
        if elapsed <= last_elapsed:
            # The clock made no progress since the previous poll, which is what a frozen clock
            # paired with a no-op sleep looks like. Counting these polls is the safety net that
            # turns that combination into a deterministic failure instead of a hang.
            stalled_polls += 1
            if stalled_polls >= poll_budget:
                raise AssertionError(
                    f"{_PREFIX}: poll budget of {poll_budget} exhausted after "
                    f"{_format_seconds(elapsed)}s and {_poll_word(polls)} "
                    f"(interval={_format_seconds(period)}s): "
                    f"{_outcome_text(last_error, last_value, redactor)}"
                )
        else:
            stalled_polls = 0
            last_elapsed = elapsed

        # Clamped to the time left, so the poller never waits past its own deadline.
        wait(min(period, max(limit - elapsed, 0.0)))
