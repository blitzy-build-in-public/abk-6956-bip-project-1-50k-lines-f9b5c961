"""The ``foundry_clock`` fixture: one frozen, deterministic clock per test.

``foundry_clock`` is function scoped and returns a :class:`FoundryClock` frozen at
:data:`DEFAULT_INSTANT`, so two runs of the same test start from the same moment. The class
publishes three operations: :meth:`FoundryClock.now` returns the current instant, timezone-aware
UTC; :meth:`FoundryClock.advance` moves it by a keyword-only delta and returns the new one,
combining every component into a single step, so ``advance(minutes=2, hours=1)`` is one 62-minute
move; and :meth:`FoundryClock.set` replaces it outright.
Why frozen
    A test asserting "this poller gave up after three attempts" must not depend on how quick the
    host
    was: ``advance`` doubles as the wait a fake sleeper performs, so a poller handed ``clock=clock``
    and ``sleep=lambda s: clock.advance(seconds=s)`` finishes in microseconds while exercising the
    deadline arithmetic a live run uses.
Not injected into ``foundry_poll``
    The poller keeps a real sleeper as its default, because a suite polling a live service still has
    to wait; determinism is opt-in.
No wall-clock read, aware instants only
    Nothing here reads the interpreter's clock and :mod:`time` is not imported (enforced by
    ``tests/unit/test_sleep_policy.py``). Every instant returned carries :data:`datetime.UTC`; an
    aware instant in another zone is converted, and a naive one raises
    :class:`foundry.core.exceptions.FoundryError`, because assuming the host's local zone is the
    dependence this fixture removes.
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

A documented epoch rather than "now", so two runs of the same test begin at the same moment. The
value is immutable, so sharing it as the constructor and fixture default is safe.
"""


def _as_utc(instant: datetime, *, where: str) -> datetime:
    """Return ``instant`` as a timezone-aware UTC ``datetime``.

    An aware instant in another zone is converted; a naive one is rejected, because filling
    in the host's local zone would reintroduce the dependence this fixture removes.

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

    The components are combined into one :class:`datetime.timedelta`, so the clock moves once and a
    caller can never observe a half-applied delta. Values the type cannot represent are rejected
    with Foundry's own error rather than letting a ``ValueError`` or ``OverflowError`` escape.

    Raises:
        FoundryError: When a component is not a finite number, or when the combined offset is too
            large for :class:`datetime.timedelta` to hold.
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

    The class is Foundry's deterministic time source and satisfies
    :class:`foundry.core.types.ClockLike` structurally, so it can be handed to
    :func:`foundry.assertions.eventually` as its ``clock`` without that module importing this one.
    The instant starts at the constructor's value (:data:`DEFAULT_INSTANT` by default),
    :meth:`advance` moves it forward by an explicit delta and :meth:`set` replaces it. Those three
    operations are the whole contract: no attribute to assign, no implicit movement and no tolerance
    for a naive datetime, so one instance belongs to one test and a test that advanced the clock
    cannot affect any other test or xdist worker.
    """

    __slots__ = ("_instant",)

    _instant: datetime

    def __init__(self, instant: datetime = DEFAULT_INSTANT) -> None:
        """Freeze the clock at ``instant``.

        Raises:
            FoundryError: When ``instant`` is naive, so the clock gains no hidden dependency on the
                host's local zone.
        """
        self._instant = _as_utc(instant, where="the starting instant")

    def now(self) -> datetime:
        """Return the current frozen instant.

        It never consults the system clock, so a deadline computed from it is reproducible anywhere.

        Returns:
            A timezone-aware ``datetime`` in UTC: the instant the clock was created at, moved by
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

        Every component is keyword-only and optional, and one call may combine several: they are
        summed into a single :class:`datetime.timedelta` before it is applied. The keyword-only form
        makes this a usable sleeper replacement, so a poller calls
        ``sleep=lambda seconds: clock.advance(seconds=seconds)`` and fractional ``seconds`` work.

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

        Raises:
            FoundryError: When ``instant`` is naive, naming the missing timezone rather than
                assuming one.
        """
        self._instant = _as_utc(instant, where="set()")

    def __repr__(self) -> str:
        """Return a stable representation naming only the frozen instant.

        No run id, no host name and no wall-clock reading, so a snapshot or a failure message
        quoting the clock keeps the same text between two runs, for example
        ``FoundryClock(instant=2024-01-01T00:00:00+00:00)``.
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

    Function scoped, so advancing the clock in one test cannot move the instant another test sees,
    and no single clock is shared between items or between xdist workers. The clock is deliberately
    not wired into ``foundry_poll``: that fixture keeps a real sleeper so a suite still waits
    against a live service, and determinism stays something a test asks for.

    Returns:
        A :class:`FoundryClock` whose :meth:`~FoundryClock.now` is :data:`DEFAULT_INSTANT`.
    """
    return FoundryClock()
