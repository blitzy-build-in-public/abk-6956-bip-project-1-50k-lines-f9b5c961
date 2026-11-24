"""The ``foundry_poll`` fixture: polling with the wait chosen by the test.

``foundry_poll`` is one of Foundry's twelve public fixtures (AAP 0.4.3). It is function scoped,
returns a :class:`FoundryPoll`, and publishes the polling primitive AAP 0.4.3 names,
``eventually(fn, *, timeout, interval, sleep=time.sleep)``, so a test waits for a live dependency
without assembling a loop of its own.

Delegation
    :meth:`FoundryPoll.eventually` is a thin adapter over
    :func:`foundry.assertions.eventually` and holds no polling logic of its own -- no deadline
    arithmetic, no poll counter, no failure message. Keeping the adapter empty is what makes the
    helper and the fixture produce one message shape (AAP 0.7.3).

Why the default is the real sleeper
    ``sleep`` defaults to :func:`time.sleep` (AAP 0.4.3), because a suite that polls a live service
    still has to wait for it. A test that cannot afford the wait passes its own callable per call --
    a recording fake, or a frozen clock's ``advance`` -- which keeps every sleep in Foundry's suite
    under the 100 ms cap (AAP 0.14.3).

Why the clock is injectable but never injected
    ``clock`` is accepted per call and defaults to ``None``, meaning the monotonic clock, so this
    fixture never requests ``foundry_clock``: a frozen clock wired in by default would make a suite
    polling a real service give up on its first poll. A supplied clock also keeps such a test
    terminating, because the helper stops on its ``ceil(timeout / interval) + 2`` poll budget when
    the clock never advances.

Import cost and state
    Importing this module defines names only: it imports :mod:`pytest`, :mod:`time`,
    :mod:`dataclasses`, :mod:`typing` and, for annotations alone, ``Sleep`` and ``ClockLike``. The
    :mod:`foundry.assertions` import sits inside :meth:`FoundryPoll.eventually`, so registering the
    fixture during ``pytest_configure`` pulls none of the assertion helpers into the plugin's import
    graph (implicit requirement I5). There is no module-level state and no background thread.
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
CPython 3.11.
"""


@dataclass(frozen=True, slots=True)
class FoundryPoll:
    """The poller ``foundry_poll`` hands a test: one ``eventually`` call, two carried defaults.

    The object exists so a test can hold the polling primitive as a fixture instead of importing
    :func:`foundry.assertions.eventually` directly. Beyond that it is deliberately empty: the two
    fields are the per-call defaults, and :meth:`eventually` forwards everything to the helper.

    Frozen and slotted, because a test that wants a different sleeper or a frozen clock passes it to
    the call rather than reassigning a field, so one test can never change how a later one waits.

    Attributes:
        sleep: The wait used between polls when the call does not name one; :func:`time.sleep`, the
            behaviour AAP 0.4.3 publishes for the fixture.
        clock: The time source used to measure the deadline when the call does not name one, or
            ``None`` -- the default -- to measure on the monotonic clock.
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

        The arguments, the waiting and the failure are :func:`foundry.assertions.eventually`'s own;
        this method only supplies the defaults the instance carries. Each poll calls ``fn`` with no
        arguments, so a truthy return value ends the wait and is returned unchanged while a falsy
        one keeps polling, and an exception raised by ``fn`` is remembered as the last outcome.

        Args:
            fn: The zero-argument callable to poll, judged by truthiness alone.
            timeout: How long to keep polling, in seconds: required, keyword-only and at least
                zero, where ``0`` polls exactly once (AAP 0.4.3).
            interval: How long to wait between polls, in seconds: required, keyword-only and
                greater than zero.
            sleep: The wait used between polls; ``None`` uses the instance's :attr:`sleep`.
            clock: The time source the deadline is measured on, or ``None`` to use the instance's
                :attr:`clock` and, through it, the monotonic clock.

        Returns:
            The first truthy value ``fn`` returned, unchanged.

        Raises:
            AssertionError: When the deadline passes, when the poll budget is exhausted because a
                supplied clock never advanced, or when an argument is unusable. The message names
                the last value or exception with the poll count and the timeout, quoted redacted
                and bounded.
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

    Function scoped (AAP 0.4.3), so no poller state, no sleeper and no clock is shared between items
    or between xdist workers. The returned value waits for real by default, because a suite polling
    a live service has to.

    The fixture intentionally requests nothing -- in particular not ``foundry_clock`` -- so
    installing Foundry never changes how a suite waits and requesting a clock stays a decision each
    test makes for itself.

    Returns:
        A :class:`FoundryPoll` whose :attr:`~FoundryPoll.sleep` is :func:`time.sleep` and whose
        :attr:`~FoundryPoll.clock` is ``None``.
    """
    return FoundryPoll()
