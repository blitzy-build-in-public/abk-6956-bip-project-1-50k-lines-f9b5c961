"""Polling helper: call a function until it succeeds, or until the deadline passes.

``eventually`` is Foundry's public polling primitive (AAP 0.4.3). It calls a zero-argument
callable repeatedly and stops on the first *truthy* value, which it returns unchanged:

* a truthy return value ends the wait and is handed straight back to the caller;
* a falsy return value (``False``, ``0``, ``""``, ``[]``, ``None``) keeps polling;
* an exception raised by the callable keeps polling too, and is remembered as the last
  outcome instead of being propagated.

``None`` therefore never satisfies this helper. A polled function whose success signal is
``None`` must return something truthy instead: the value it waited for, or ``True``.

Deadline
    Elapsed time is measured from a single baseline taken when the call starts, on the
    monotonic clock (``time.monotonic``) by default or on a supplied clock implementing
    :class:`foundry.core.types.ClockLike`. A wall-clock read is never taken, so a run does not
    depend on the machine's date, on daylight saving or on a clock adjustment, and a deadline
    in seconds means the same thing on every host. Measuring from one baseline rather than
    accumulating intervals keeps the deadline exact even when a sleep is cut short.

Injected sleep
    Every wait goes through the ``sleep`` argument (AAP 0.4.3, AAP 0.7.3). This module never
    sleeps itself -- ``time.sleep`` appears only as the parameter's default -- so a test or the
    ``foundry_poll`` fixture passes a recording fake and asserts on the delays instead of
    paying them. That is what keeps every sleep in Foundry's suite under the 100 ms cap
    (AAP 0.14.3) and what lets the example project spend its wall clock on deterministic work
    rather than on waiting.

Interrupts
    An ``Exception`` raised by the polled callable is an outcome; a ``BaseException`` is not.
    ``KeyboardInterrupt`` and ``SystemExit`` leave the poller immediately and unwind through
    the test, so the host runner reports the interrupt with its own exit code (AAP 0.4.7).

Poll budget
    A clock that never advances combined with a no-op sleep would otherwise poll forever, so
    the loop counts consecutive polls that saw no progress on the clock and stops once
    ``ceil(timeout / interval) + 2`` of them have happened. The budget is a safety net for a
    stalled clock rather than a second deadline: a clock that does advance always reaches the
    timeout first, so a caller who injects a no-op sleep and a real clock still gets the
    documented ``timed out after <timeout>s`` message. When the budget is what ended the loop,
    the message says that instead.

Rendered outcomes
    The last outcome is quoted in the failure message, which is why it is scrubbed through
    :func:`foundry.obs.redaction.default_redactor` and bounded by
    :data:`foundry.obs.redaction.MAX_EXCERPT_BYTES` before interpolation: a polled value is
    frequently a response body, a header set or a token-bearing payload, and the message
    reaches the terminal, ``results.json``, ``junit.xml`` and ``report.html``. Every message is
    a single line and never carries a traceback.

The ``clock`` keyword
    ``clock`` is keyword-only and defaults to ``None``, so the signature published in AAP
    0.4.3 keeps working unchanged: the stability statement permits additions and forbids
    renames and removals (AAP 0.4.9). It exists because AAP 0.7.1 requires a deterministic
    deadline computed from a supplied clock, which is how a test asserts deadline behaviour
    without waiting for it. Its ``now()`` must return a ``datetime`` on every call, the
    baseline included: a reader that is callable but returns anything else -- a count of
    seconds, a string, ``None`` -- raises this module's own ``AssertionError`` naming the
    clock's type, instead of leaking an ``AttributeError`` or ``TypeError`` out of the poll
    loop (AAP 0.13.1).

Importing this module defines names only: the function, its helpers, the public name listed
in ``__all__`` and the constants below. It performs no I/O, reads no environment variable,
configures no logger and parses no arguments (AAP 0.4.8).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime
from typing import Final, TypeVar

from foundry.core.types import ClockLike, Sleep
from foundry.obs.redaction import MAX_EXCERPT_BYTES, default_redactor

__all__ = ["eventually"]

T = TypeVar("T")
"""The value the polled callable returns, preserved unchanged through :func:`eventually`."""

_PREFIX: Final[str] = "foundry.assertions.eventually"
"""Prefix of every message this module produces, which names the helper a reader must fix."""

_POLL_BUDGET_MARGIN: Final[int] = 2
"""Polls the safety net allows beyond ``timeout / interval`` before the clock is called stalled.

Two is enough for the poll whose own duration carries the clock past the deadline and for the
one that observes it, so a healthy run never touches the budget and a frozen clock still stops
promptly.
"""


class _Missing:
    """Type of the sentinel that marks "no value was ever returned by the polled callable".

    A private sentinel is used instead of ``None`` because ``None`` is a legitimate falsy
    result: the failure message has to be able to say ``last value: None`` and a sentinel of
    ``None`` could not tell that apart from "the callable only ever raised".
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Return the sentinel's spelling.

        Returns:
            ``"<missing>"``. It is never interpolated into a user-facing message -- the
            renderer checks the identity first -- and exists so a debugger shows something
            readable.
        """
        return "<missing>"


_MISSING: Final[_Missing] = _Missing()
"""The one sentinel instance; compared with ``is`` and never with ``==``."""


def _misuse(detail: str) -> AssertionError:
    """Build the assertion raised when :func:`eventually` is called with an unusable argument.

    Args:
        detail: The requirement the argument broke together with the value that broke it.

    Returns:
        A fresh :class:`AssertionError` carrying the module prefix, which the caller raises.
    """
    return AssertionError(f"{_PREFIX}: {detail}")


def _format_seconds(value: float) -> str:
    """Render a number of seconds the way every message in this module renders it.

    ``%g`` style is used so a deadline reads ``0.5`` rather than ``0.5000000000000001``: the
    figure comes from configuration or from a monotonic difference, and neither should leak
    floating-point noise into a message a person reads or a test matches.

    Args:
        value: A number of seconds.

    Returns:
        The shortest faithful decimal spelling of ``value``, without a unit suffix.
    """
    return f"{value:g}"


def _poll_word(polls: int) -> str:
    """Render a poll count with the noun agreeing with it.

    The count reaches a message a person reads and a test matches, and a single poll is a
    common outcome -- ``timeout=0`` polls exactly once -- so a fixed plural would read
    ``1 polls`` on the most easily reproduced failure there is.

    Args:
        polls: How many times the polled callable has been called.

    Returns:
        ``"1 poll"`` for a single poll, and ``"<n> polls"`` for every other count.
    """
    return f"{polls} poll" if polls == 1 else f"{polls} polls"


def _as_finite_seconds(value: object) -> float | None:
    """Return ``value`` as a finite ``float``, or ``None`` when it is not a usable duration.

    Args:
        value: The value to coerce. Only ``int`` and ``float`` are accepted; ``bool`` is
            rejected even though it subclasses ``int``, because ``timeout=True`` is a mistake
            rather than a one-second timeout. ``NaN`` and the infinities are rejected too, as
            they would poison every comparison the loop makes.

    Returns:
        ``value`` as a finite ``float``, or ``None`` when it is not a finite real number.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _require_timeout(timeout: object) -> float:
    """Validate the deadline argument.

    Args:
        timeout: The caller's ``timeout``, which must be a finite number of seconds of zero
            or more. Zero is accepted and means "call the function once, then fail".

    Returns:
        The deadline as a ``float``.

    Raises:
        AssertionError: When ``timeout`` is not a finite real number or is negative.
    """
    number = _as_finite_seconds(timeout)
    if number is None or number < 0.0:
        raise _misuse(
            f"timeout must be a finite number of seconds >= 0, got {_excerpt(repr(timeout))}"
        )
    return number


def _require_interval(interval: object) -> float:
    """Validate the poll interval argument.

    Args:
        interval: The caller's ``interval``, which must be a finite number of seconds greater
            than zero. A zero interval would spin the loop without ever yielding, and a
            negative one would make the sleep clamp meaningless.

    Returns:
        The interval as a ``float``.

    Raises:
        AssertionError: When ``interval`` is not a finite real number or is not positive.
    """
    number = _as_finite_seconds(interval)
    if number is None or number <= 0.0:
        raise _misuse(
            f"interval must be a finite number of seconds > 0, got {_excerpt(repr(interval))}"
        )
    return number


def _require_pollable(fn: Callable[[], T]) -> Callable[[], T]:
    """Validate the polled callable.

    Args:
        fn: The zero-argument callable to poll.

    Returns:
        ``fn`` itself, so the caller can use the validated value.

    Raises:
        AssertionError: When ``fn`` is not callable.
    """
    if not callable(fn):
        raise _misuse(f"fn must be callable, got {type(fn).__name__}")
    return fn


def _require_sleep(sleep: Sleep) -> Sleep:
    """Validate the injected sleeper.

    The value is checked here rather than at the first wait so that a mistake in a test's
    fixture fails at the top of the call, with the same ``AssertionError`` type as every other
    misuse, instead of surfacing as a ``TypeError`` from inside the loop.

    Args:
        sleep: The callable that performs a wait of the given number of seconds.

    Returns:
        ``sleep`` itself, so the caller can use the validated value.

    Raises:
        AssertionError: When ``sleep`` is not callable.
    """
    if not callable(sleep):
        raise _misuse(f"sleep must be callable, got {type(sleep).__name__}")
    return sleep


def _clock_misuse(clock: object) -> AssertionError:
    """Build the assertion raised when the supplied clock breaks the time-source contract.

    One builder serves both halves of the contract -- a reader that is not callable and a
    reader whose result is not a ``datetime`` -- so the message has a single source.

    Args:
        clock: The value the caller passed as ``clock``. Its type is named in the message,
            because that is the object the caller has to fix.

    Returns:
        A fresh :class:`AssertionError` carrying the module prefix, which the caller raises.
    """
    return _misuse(f"clock must provide now() returning a datetime, got {type(clock).__name__}")


def _require_clock(clock: ClockLike | None) -> ClockLike | None:
    """Validate the supplied time source.

    Args:
        clock: ``None`` to use the monotonic clock, or an object implementing
            :class:`foundry.core.types.ClockLike`.

    Returns:
        ``clock`` unchanged, or ``None`` when the monotonic clock is to be used.

    Raises:
        AssertionError: When ``clock`` is neither ``None`` nor an object exposing a callable
            ``now()`` that returns a ``datetime``. This function checks that the reader is
            present; :func:`_read_instant` enforces the returned type on every read, and
            raises the same message when it does not hold.
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

    :class:`foundry.core.types.ClockLike` exposes ``now() -> datetime`` rather than a plain
    seconds counter, so the baseline is captured here as the instant the call started and each
    later reading is turned into a difference. ``time.monotonic`` is the default source
    because it cannot go backwards when the machine's wall clock is adjusted.

    Every reading is taken through :func:`_read_instant`, so a clock that stops returning a
    ``datetime`` raises this module's own :class:`AssertionError` instead of a builtin one.

    Args:
        clock: The supplied time source, or ``None`` to measure on the monotonic clock.

    Returns:
        A zero-argument callable returning the seconds elapsed since this function was called,
        as a ``float``.

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
    exception's ``str`` may be a formatted block and a polled payload may be pretty-printed
    JSON.

    Args:
        text: The text to flatten.

    Returns:
        The flattened text, without leading or trailing whitespace.
    """
    return " ".join(text.split())


def _excerpt(rendered: str) -> str:
    """Redact, bound and flatten a rendered outcome for inclusion in a message.

    Redaction runs before truncation, inside :meth:`foundry.obs.redaction.Redactor.text`, so a
    secret straddling the cut cannot leave its prefix behind; the excerpt is then collapsed to
    one line.

    Args:
        rendered: The text to quote, already converted to ``str``.

    Returns:
        The excerpt, bounded by :data:`foundry.obs.redaction.MAX_EXCERPT_BYTES` plus its
        truncation marker, containing no newline.
    """
    excerpt = default_redactor().body_excerpt(rendered, limit=MAX_EXCERPT_BYTES)
    return _one_line(excerpt)


def _render_value(value: object) -> str:
    """Render a falsy polled value for the failure message.

    Args:
        value: The last value the polled callable returned. ``repr`` is used rather than
            ``str`` so that ``None``, ``False``, ``0`` and ``""`` stay distinguishable in the
            message.

    Returns:
        The value's ``repr``, or a placeholder naming its type when ``repr`` itself fails.
    """
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _outcome_text(last_error: Exception | None, last_value: object) -> str:
    """Render the trailing clause of a failure message, naming the last poll outcome.

    Exactly one of the two outcomes is always set once the loop has polled at least once, and
    the two are mutually exclusive because a poll either returns or raises.

    Args:
        last_error: The exception the last poll raised, or ``None`` when it returned.
        last_value: The value the last poll returned, or the sentinel when it raised.

    Returns:
        ``last exception <Type>(<message>)`` or ``last value: <repr>``, redacted and bounded.
    """
    if last_error is not None:
        rendered = f"{type(last_error).__name__}({last_error!s})"
        return f"last exception {_excerpt(rendered)}"
    if last_value is _MISSING:
        return "no poll completed"
    return f"last value: {_excerpt(_render_value(last_value))}"


def eventually(
    fn: Callable[[], T],
    *,
    timeout: float,
    interval: float,
    sleep: Sleep = time.sleep,
    clock: ClockLike | None = None,
) -> T:
    """Poll ``fn`` until it returns a truthy value, or raise once the deadline passes.

    Each poll calls ``fn`` with no arguments. A truthy return value ends the wait and is
    returned unchanged; a falsy one is remembered and polling continues; an exception raised by
    ``fn`` is remembered as the last outcome and polling continues too. Once the deadline has
    passed the helper raises :class:`AssertionError` naming the last value or exception and the
    number of polls, so a failing wait says what it last saw rather than only that it gave up.

    Every wait goes through ``sleep``, so a caller that injects a recording fake never spends
    real time. ``KeyboardInterrupt`` and ``SystemExit`` are not caught and leave the helper
    immediately.

    Args:
        fn: The zero-argument callable to poll. It receives nothing and its return value is
            judged by truthiness alone, so a function that signals success by returning
            ``None`` never satisfies this helper.
        timeout: How long to keep polling, in seconds. Must be a finite number greater than or
            equal to zero; ``0`` polls exactly once and fails if that poll is not a success.
        interval: How long to wait between polls, in seconds. Must be a finite number greater
            than zero, since a zero-length wait would spin without yielding.
        sleep: The wait used between polls, called with the number of seconds to wait. Defaults
            to ``time.sleep``; tests and the ``foundry_poll`` fixture pass a fake.
        clock: Optional time source used instead of the monotonic clock. It must implement
            :class:`foundry.core.types.ClockLike`, whose ``now()`` returns the current
            ``datetime``; the deadline is then measured from the instant the call started.
            Additive over AAP 0.4.3 and permitted by AAP 0.4.9.

    Returns:
        The first truthy value ``fn`` returned, unchanged.

    Raises:
        AssertionError: When the deadline passes, when the poll budget is exhausted because a
            supplied clock never advanced, or when an argument is unusable (``fn`` not
            callable, ``timeout`` or ``interval`` outside its accepted range, ``sleep`` not
            callable, ``clock`` without a callable ``now()`` returning a ``datetime``). The
            message names the last outcome and the poll count, and quotes it redacted and
            bounded.

    Example:
        Poll an endpoint until the order it describes is ready, without spending real time::

            def order_is_ready() -> bool:
                return client.get("/orders/7").json()["status"] == "ready"

            order = eventually(order_is_ready, timeout=5.0, interval=0.25, sleep=fake_sleep)
    """
    poll = _require_pollable(fn)
    limit = _require_timeout(timeout)
    period = _require_interval(interval)
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
                f"{_outcome_text(last_error, last_value)}"
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
                    f"{_outcome_text(last_error, last_value)}"
                )
        else:
            stalled_polls = 0
            last_elapsed = elapsed

        # Clamped to the time left, so the poller never waits past its own deadline.
        wait(min(period, max(limit - elapsed, 0.0)))
