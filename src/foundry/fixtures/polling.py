"""The ``foundry_poll`` fixture: polling with the wait chosen by the test.

``foundry_poll`` is one of Foundry's twelve public fixtures (AAP 0.4.3). It is function scoped,
returns a :class:`FoundryPoll`, and publishes the polling primitive AAP 0.4.3 names::

    eventually(fn, *, timeout, interval, sleep=time.sleep)

so a test waits for a live dependency without assembling a loop of its own::

    def test_order_becomes_ready(foundry_poll: FoundryPoll) -> None:
        foundry_poll.eventually(
            lambda: client.get("/orders/7").json()["status"] == "ready",
            timeout=5.0,
            interval=0.25,
        )

Delegation
    :meth:`FoundryPoll.eventually` is a thin adapter over
    :func:`foundry.assertions.eventually` and holds no polling logic of its own -- no deadline
    arithmetic, no poll counter, no failure message. That module takes one baseline on the
    monotonic clock (or on a supplied clock), stops on the first truthy value, treats an
    exception raised by the polled callable as an outcome rather than a fault, lets
    ``KeyboardInterrupt`` and ``SystemExit`` through, and raises ``AssertionError`` naming the
    last value or exception and the poll count once the deadline passes. Keeping the adapter
    empty is what makes the helper and the fixture produce one message shape (AAP 0.7.3).

Why the default is the real sleeper
    ``sleep`` defaults to :func:`time.sleep` (AAP 0.4.3), because a suite that polls a live
    service still has to wait for it. A test that cannot afford the wait passes its own callable
    per call -- a recording fake, or a frozen clock's ``advance`` -- which is how Foundry's own
    suite keeps every sleep under the 100 ms cap (AAP 0.14.3) and how the example project spends
    its wall clock on deterministic work rather than on waiting.

Why the clock is injectable but never injected
    ``clock`` is accepted per call and defaults to ``None``, meaning the monotonic clock, so this
    fixture never requests ``foundry_clock``: a frozen clock wired in by default would make a
    suite that polls a real service give up on its first poll. Determinism stays something a test
    asks for explicitly, and requesting ``foundry_clock`` in one test can never change how
    another test waits::

        ready = foundry_poll.eventually(
            ping,
            timeout=1.0,
            interval=0.1,
            clock=foundry_clock,
            sleep=lambda seconds: foundry_clock.advance(seconds=seconds),
        )

    A supplied clock is also what keeps such a test terminating: the helper stops on its
    ``ceil(timeout / interval) + 2`` poll budget when the clock never advances, so a frozen clock
    paired with a no-op sleep fails deterministically instead of hanging the run.

Import cost and state
    Importing this module defines names only: it imports :mod:`pytest`, :mod:`time`,
    :mod:`dataclasses`, :mod:`typing` and, for annotations alone,
    :class:`foundry.core.types.Sleep` and :class:`foundry.core.types.ClockLike`. The
    :mod:`foundry.assertions` import sits inside :meth:`FoundryPoll.eventually`, so registering
    the fixture during ``pytest_configure`` pulls none of the assertion helpers -- and neither
    ``jsonschema`` nor the redaction engine they load -- into the plugin's import graph (implicit
    requirement I5). There is no module-level state, no cache and no background thread: a
    :class:`FoundryPoll` is immutable, the fixture returns a fresh one per test and nothing is
    scheduled, so a poll can never outlive the test that asked for it (AAP 0.4.8).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

import pytest

if TYPE_CHECKING:
    from foundry.core.types import ClockLike, Sleep

__all__ = ["FoundryPoll", "foundry_poll"]

T = TypeVar("T")
"""The value the polled callable returns, preserved through :meth:`FoundryPoll.eventually`.

A module-level :class:`typing.TypeVar` rather than PEP 695 syntax because the declared floor is
CPython 3.11 and ``mypy --strict`` resolves the classic form identically on every supported
interpreter.
"""


@dataclass(frozen=True, slots=True)
class FoundryPoll:
    """The poller ``foundry_poll`` hands a test: one ``eventually`` call, two carried defaults.

    The object exists so a test can hold the polling primitive as a fixture instead of importing
    :func:`foundry.assertions.eventually` directly, which is what keeps the fixture names
    stable while the helper underneath stays free to change. Beyond that it is deliberately
    empty: the two fields are the per-call defaults, and :meth:`eventually` forwards everything
    to the helper.

    Frozen and slotted because an instance crosses nothing and mutates nothing -- a test that
    wants a different sleeper or a frozen clock passes it to the call rather than reassigning a
    field, so one test can never change how a later one waits even when both hold the same
    fixture value under ``pytest``'s caching.

    Attributes:
        sleep: The wait used between polls when the call does not name one. Defaults to
            :func:`time.sleep`, the behaviour AAP 0.4.3 publishes for the fixture.
        clock: The time source used to measure the deadline when the call does not name one, or
            ``None`` -- the default -- to measure on the monotonic clock and ignore the wall
            clock entirely.
    """

    sleep: Sleep = time.sleep
    clock: ClockLike | None = None

    def eventually(
        self,
        fn: Callable[[], T],
        *,
        timeout: float,
        interval: float,
        sleep: Sleep | None = None,
        clock: ClockLike | None = None,
    ) -> T:
        """Poll ``fn`` until it returns a truthy value, or fail once the deadline passes.

        The arguments, the waiting and the failure are :func:`foundry.assertions.eventually`'s
        own; this method only supplies the defaults the instance carries. Each poll calls ``fn``
        with no arguments, so a truthy return value ends the wait and is returned unchanged
        while a falsy one -- ``False``, ``0``, ``""``, ``None`` -- keeps polling. An exception
        raised by ``fn`` is remembered as the last outcome and polling continues too, which is
        what lets a test poll a dependency that is still refusing connections.

        Args:
            fn: The zero-argument callable to poll. Judged by truthiness alone, so a function
                that signals success by returning ``None`` never satisfies it.
            timeout: How long to keep polling, in seconds. Required and keyword-only (AAP 0.4.3);
                a finite number greater than or equal to zero, where ``0`` polls exactly once.
            interval: How long to wait between polls, in seconds. Required and keyword-only; a
                finite number greater than zero.
            sleep: The wait used between polls, called with the number of seconds to wait.
                ``None`` uses the instance's :attr:`sleep`, so a test can override it for one
                call -- a recording fake never spends the time it is asked to wait.
            clock: The time source the deadline is measured on, or ``None`` to use the
                instance's :attr:`clock` and, through it, the monotonic clock. It must implement
                ``now() -> datetime``; the deadline is measured from the instant the call
                started, and a clock that never advances ends the wait on the helper's poll
                budget rather than hanging.

        Returns:
            The first truthy value ``fn`` returned, unchanged.

        Raises:
            AssertionError: When the deadline passes, when the poll budget is exhausted because a
                supplied clock never advanced, or when an argument is unusable. The message names
                the last value or the last exception together with the poll count and the
                timeout, and quotes that outcome redacted and bounded. The run's configured
                ``secrets.redact_keys`` are applied when the caller needs them by calling
                :func:`foundry.assertions.eventually` directly with its ``redactor`` argument.
        """
        from foundry.assertions import eventually as _eventually

        return _eventually(
            fn,
            timeout=timeout,
            interval=interval,
            sleep=self.sleep if sleep is None else sleep,
            clock=self.clock if clock is None else clock,
        )


@pytest.fixture
def foundry_poll() -> FoundryPoll:
    """Return a poller for this test, carrying :func:`time.sleep` and the monotonic clock.

    Function scoped (AAP 0.4.3), so no poller state, no sleeper and no clock is shared between
    items or between xdist workers. The returned value waits for real by default, because a
    suite polling a live service has to; a test that wants its wait to be instantaneous passes
    the sleeper and the clock per call, as the example below does.

    The fixture intentionally requests nothing -- in particular not ``foundry_clock`` -- so
    installing Foundry never changes how a suite waits and requesting a clock stays a decision
    each test makes for itself.

    Returns:
        A :class:`FoundryPoll` whose :attr:`~FoundryPoll.sleep` is :func:`time.sleep` and whose
        :attr:`~FoundryPoll.clock` is ``None``.

    Example:
        Poll without spending real time, by advancing a frozen clock in place of the wait::

            def test_order_becomes_ready(
                foundry_poll: FoundryPoll, foundry_clock: FoundryClock
            ) -> None:
                answers = iter([False, False, True])
                assert foundry_poll.eventually(
                    lambda: next(answers),
                    timeout=1.0,
                    interval=0.1,
                    clock=foundry_clock,
                    sleep=lambda seconds: foundry_clock.advance(seconds=seconds),
                )
    """
    return FoundryPoll()
