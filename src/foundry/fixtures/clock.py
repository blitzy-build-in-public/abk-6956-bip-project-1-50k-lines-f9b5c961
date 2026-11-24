"""The ``foundry_clock`` fixture: one frozen, deterministic clock per test.

``foundry_clock`` is one of Foundry's twelve public fixtures (AAP 0.4.3). It is function scoped
and returns a :class:`FoundryClock` frozen at :data:`DEFAULT_INSTANT`, so two runs of the same
test start from the same moment and neither of them consults the machine's clock.

The class publishes three operations and a deterministic representation:

* :meth:`FoundryClock.now` -- the current frozen instant, always timezone-aware UTC.
* :meth:`FoundryClock.advance` -- move the instant by a keyword-only delta and return the new
  one. Every component is optional and one call may combine several, so ``advance(minutes=2,
  hours=1)`` is a single 62-minute step. The keyword-only signature is deliberate: it makes
  ``sleep=lambda seconds: clock.advance(seconds=seconds)`` read explicitly, and an unknown
  keyword is Python's own ``TypeError`` rather than anything this module raises.
* :meth:`FoundryClock.set` -- replace the instant outright, for a test that starts at a
  business-relevant moment instead of the documented epoch.

Why frozen
    A test that asserts "this poller gave up after three attempts" or "this retry waited two
    seconds" must not depend on how quick the host was that afternoon. A frozen clock turns
    elapsed time into arithmetic: :meth:`FoundryClock.advance` doubles as the wait a fake
    sleeper performs, so a poller handed ``clock=clock`` and
    ``sleep=lambda s: clock.advance(seconds=s)`` finishes in microseconds while exercising
    exactly the deadline arithmetic a live run uses.

Not injected into ``foundry_poll``
    The poller keeps a real sleeper as its default and ``foundry_poll`` does not request this
    fixture (AAP 0.4.3), because a suite that polls a live service still has to wait for it.
    Determinism is opt-in: a test that wants it requests ``foundry_clock`` and passes the clock
    explicitly, as the example on :func:`foundry_clock` shows. Requesting the clock must never
    silently change how another fixture or another test waits.

No wall-clock read
    Nothing here asks the interpreter for the current time, and the :mod:`time` module is not
    imported at all (AAP 0.14.3, enforced by ``tests/unit/test_sleep_policy.py``): every instant
    this module can produce comes from :data:`DEFAULT_INSTANT` plus a delta a test asked for. A
    clock that read the system time would make ``results.json`` and ``report.html`` differ
    between two otherwise identical runs, which is what the determinism criterion forbids.

Instants are timezone-aware
    Every instant returned carries :data:`datetime.UTC`. An aware instant expressed in
    another zone is converted rather than rejected; a naive one is misuse and raises
    :class:`foundry.core.exceptions.FoundryError`, because the alternative -- assuming the
    host's local zone -- is the very dependence this fixture exists to remove.

Importing this module defines names only: the constant, the class, the fixture function and the
private helpers below. It performs no I/O, reads no environment variable, configures no logger
and parses no arguments (AAP 0.4.8), and it imports nothing beyond the standard library,
:mod:`pytest` and :mod:`foundry.core`, which keeps it inside the plugin's startup budget.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import pytest

from foundry.core.exceptions import FoundryError

if TYPE_CHECKING:
    from foundry.core.types import ClockLike

__all__ = ["DEFAULT_INSTANT", "FoundryClock", "foundry_clock"]

_PREFIX: Final[str] = "foundry.fixtures.clock"
"""Prefix of every message this module raises, naming the module a reader has to fix."""

DEFAULT_INSTANT: Final[datetime] = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
"""The fixed instant every :class:`FoundryClock` starts from.

A documented epoch rather than "now": two runs of the same test then begin at the same moment,
which is what makes an assertion about a timestamp reproducible. The value is immutable, so
sharing it as the constructor and fixture default is safe, and nothing in this module ever
rebinds it -- a caller moves its own clock with :meth:`FoundryClock.advance` or
:meth:`FoundryClock.set` instead.
"""


def _as_utc(instant: datetime, *, where: str) -> datetime:
    """Return ``instant`` as a timezone-aware UTC ``datetime``.

    An aware instant expressed in any other zone is converted, so :meth:`FoundryClock.now`
    yields UTC whatever a caller passed. A naive instant has no zone to convert from, and
    filling in the host's local one would reintroduce exactly the dependence this fixture
    removes, so it is rejected rather than guessed at.

    Args:
        instant: The instant to normalise.
        where: The caller-facing operation the instant came from, named in the failure message
            so a reader knows whether it is the constructor or :meth:`FoundryClock.set` at fault.

    Returns:
        The same moment expressed in UTC.

    Raises:
        FoundryError: When ``instant`` is naive, meaning its ``tzinfo`` is absent or reports no
            UTC offset.
    """
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise FoundryError(
            f"{_PREFIX}: {where} requires a timezone-aware datetime, got a naive one",
            hint="pass a datetime carrying tzinfo, for example datetime(2024, 1, 1, tzinfo=UTC)",
        )
    return instant.astimezone(UTC)


def _offset(
    *,
    seconds: float,
    minutes: float,
    hours: float,
    days: float,
    microseconds: float,
) -> timedelta:
    """Build the single offset one :meth:`FoundryClock.advance` call applies.

    The components are combined here, in one :class:`datetime.timedelta`, so the clock moves
    once and a caller can never observe a half-applied delta. Values :class:`datetime.timedelta`
    cannot represent are rejected with Foundry's own error instead of letting the standard
    library's ``ValueError`` or ``OverflowError`` escape this module (AAP 0.13.1).

    Args:
        seconds: Seconds to move, fractional values included.
        minutes: Minutes to move.
        hours: Hours to move.
        days: Days to move.
        microseconds: Microseconds to move.

    Returns:
        A :class:`datetime.timedelta` holding every component.

    Raises:
        FoundryError: When a component is not a finite number, or when the combined offset is
            too large for :class:`datetime.timedelta` to hold.
    """
    components = (
        ("seconds", seconds),
        ("minutes", minutes),
        ("hours", hours),
        ("days", days),
        ("microseconds", microseconds),
    )
    for name, value in components:
        if not math.isfinite(value):
            raise FoundryError(
                f"{_PREFIX}: advance() requires finite durations, got {name}={value!r}",
                hint="pass a real number of seconds, minutes, hours, days or microseconds",
            )
    try:
        return timedelta(
            days=days,
            seconds=seconds,
            microseconds=microseconds,
            minutes=minutes,
            hours=hours,
        )
    except OverflowError as error:
        raise FoundryError(f"{_PREFIX}: advance() offset is out of range: {error}") from error


class FoundryClock:
    """A frozen clock whose instant only the test that owns it moves.

    The class is Foundry's deterministic time source (AAP 0.4.3) and satisfies
    :class:`foundry.core.types.ClockLike` structurally, so it can be handed to
    :func:`foundry.assertions.eventually` as its ``clock`` without that module importing this
    one -- the layering contract (AAP 0.4.8) keeps the direction of that dependency out of the
    code and puts it in the protocol instead.

    The instant starts at the value passed to the constructor (:data:`DEFAULT_INSTANT` by
    default), :meth:`advance` moves it forward by an explicit delta and :meth:`set` replaces it.
    Those three operations and :meth:`now` are the whole contract: there is no attribute to
    assign, no implicit movement and no tolerance for a naive datetime, so a test that reads a
    timestamp twice sees the same value twice unless it asked for a change in between.

    One instance belongs to one test. The fixture builds a fresh clock per item, which is what
    keeps a test that advanced the clock from affecting any other test or any xdist worker.
    """

    __slots__ = ("_instant",)

    _instant: datetime

    def __init__(self, instant: datetime = DEFAULT_INSTANT) -> None:
        """Freeze the clock at ``instant``.

        Args:
            instant: The starting instant. It must be timezone-aware; an aware instant in
                another zone is converted to UTC, which is the form :meth:`now` returns.
                Defaults to :data:`DEFAULT_INSTANT`, the documented epoch ``foundry_clock``
                starts every test from.

        Raises:
            FoundryError: When ``instant`` is naive, so the clock gains no hidden dependency on
                the host's local zone.
        """
        self._instant = _as_utc(instant, where="the starting instant")

    def now(self) -> datetime:
        """Return the current frozen instant.

        This is the member :class:`foundry.core.types.ClockLike` declares, and the only reading a
        caller needs: it never consults the system clock, so a deadline computed from it is
        reproducible on any host.

        Returns:
            A timezone-aware ``datetime`` in UTC -- the instant the clock was created at, moved by
            every :meth:`advance` call since and replaced by every :meth:`set` call.
        """
        return self._instant

    def advance(
        self,
        *,
        seconds: float = 0.0,
        minutes: float = 0.0,
        hours: float = 0.0,
        days: float = 0.0,
        microseconds: float = 0.0,
    ) -> datetime:
        """Move the clock forward by the given delta and return the new instant.

        Every component is keyword-only and optional, and one call may combine several: the
        components are summed into a single :class:`datetime.timedelta` before it is applied, so
        ``advance(minutes=2, hours=1)`` is one 62-minute step rather than two visible ones.

        The keyword-only form is what makes this a usable sleeper replacement: a poller calls
        ``sleep=lambda seconds: clock.advance(seconds=seconds)`` and the argument name is spelled
        at the call site. Fractional ``seconds`` therefore work, since a sleeper receives a raw
        ``float``, and an unknown keyword stays Python's own ``TypeError``.

        Args:
            seconds: Seconds to move, fractional values included.
            minutes: Minutes to move.
            hours: Hours to move.
            days: Days to move.
            microseconds: Microseconds to move.

        Returns:
            The new frozen instant, as a timezone-aware ``datetime`` in UTC.

        Raises:
            FoundryError: When a component is not a finite number, or when the combined offset is
                too large for :class:`datetime.timedelta` to hold.
        """
        self._instant = self._instant + _offset(
            seconds=seconds,
            minutes=minutes,
            hours=hours,
            days=days,
            microseconds=microseconds,
        )
        return self._instant

    def set(self, instant: datetime) -> None:
        """Replace the frozen instant outright.

        Use it when a test cares about a particular moment -- the start of a billing period, an
        instant just before a deadline -- rather than about how much time has passed.

        Args:
            instant: The instant to freeze at. It must be timezone-aware; an aware instant in
                another zone is converted to UTC.

        Raises:
            FoundryError: When ``instant`` is naive, naming the missing timezone rather than
                assuming one.
        """
        self._instant = _as_utc(instant, where="set()")

    def __repr__(self) -> str:
        """Return a stable representation naming only the frozen instant.

        Nothing else goes in: the representation carries no run id, no host name and no reading of
        the wall clock, so a snapshot, a doctest or a failure message that quotes the clock keeps
        the same text between two runs.

        Returns:
            A string such as ``FoundryClock(instant=2024-01-01T00:00:00+00:00)``.
        """
        return f"FoundryClock(instant={self._instant.isoformat()})"


if TYPE_CHECKING:
    # Static proof that FoundryClock satisfies the ClockLike protocol the poller annotates its
    # ``clock`` argument with: this assignment stops type checking the moment the two signatures
    # diverge, and importing the protocol only for a type checker costs no runtime import.
    _CLOCK_LIKE_CONFORMANCE: ClockLike = FoundryClock()


@pytest.fixture
def foundry_clock() -> FoundryClock:
    """Return a fresh frozen clock for this test, starting at :data:`DEFAULT_INSTANT`.

    Function scoped (AAP 0.4.3), so advancing the clock in one test cannot move the instant
    another test sees, and no single clock is shared between items or between xdist workers.

    The clock is deliberately not wired into ``foundry_poll``: that fixture keeps a real sleeper
    so a suite still waits against a live service, and determinism stays something a test asks
    for. Pass the clock explicitly to make a wait instantaneous.

    Returns:
        A :class:`FoundryClock` whose :meth:`~FoundryClock.now` is :data:`DEFAULT_INSTANT`.

    Example:
        Poll without waiting, in well under a second of real time::

            from foundry.assertions.eventually import eventually

            def test_order_becomes_ready(foundry_clock: FoundryClock) -> None:
                answers = iter([False, False, True])
                ready = eventually(
                    lambda: next(answers),
                    timeout=1.0,
                    interval=0.1,
                    clock=foundry_clock,
                    sleep=lambda seconds: foundry_clock.advance(seconds=seconds),
                )
                assert ready is True
    """
    return FoundryClock()
