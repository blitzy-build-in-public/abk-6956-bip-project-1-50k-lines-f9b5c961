"""Two ``foundry_poll`` deadlines, each reached through an injected fake sleep.

AAP 0.8.1 places this module in the example project as two ``eventually`` tests: 2 of the suite's 24
collected tests. ``FoundryPoll.eventually`` polls a callable until it returns something truthy and
raises ``AssertionError`` naming the poll count once the deadline passes (AAP 0.4.3, AAP 0.7.3),
with ``foundry_clock`` injectable as its time source (AAP 0.4.3).

The deadline moves only because the fake sleeper moves it: the poller measures elapsed time on
the ``clock`` it is given and waits through the ``sleep`` it is given, so this sleeper advances
the frozen clock rather than waiting on it. ``_fake_sleep`` is the name the sleep policy exempts
from its 100 ms cap, and one ``_work.spend()`` call per test is the only wall clock spent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Final

import _work
import pytest

if TYPE_CHECKING:
    from foundry.fixtures.clock import FoundryClock
    from foundry.fixtures.polling import FoundryPoll

_TIMEOUT_GENEROUS: Final[float] = 5.0
"""A deadline the three-poll probe never reaches, so its success is not the timeout's doing."""

_TIMEOUT_TIGHT: Final[float] = 1.0
"""The deadline the always-falsy probe exhausts: ten waits of :data:`_INTERVAL`, exactly."""

_INTERVAL: Final[float] = 0.1
"""The poll interval, which is also the delay every recorded wait states."""

_SUCCEED_ON_CALL: Final[int] = 3
"""The call on which the transient probe starts answering with a truthy value."""


def _fake_sleep(clock: FoundryClock) -> tuple[Callable[[float], None], list[float]]:
    """Return a sleeper that records each delay and advances ``clock`` by it, plus that record.

    Each requested delay lands in the record in call order and moves the clock by as much, so the
    deadline arrives with no real time spent and a test can assert the interval and the poll count.
    """
    recorded: list[float] = []

    def sleep(seconds: float) -> None:
        """Record one wait and move the clock by it, spending no real time."""
        recorded.append(seconds)
        clock.advance(seconds=seconds)

    return sleep, recorded


def test_eventually_returns_after_transient_failures(
    foundry_poll: FoundryPoll, foundry_clock: FoundryClock
) -> None:
    """Return the first truthy value: the probe answers ``None`` twice, then ``ready``."""
    sleep, recorded = _fake_sleep(foundry_clock)
    started_at = foundry_clock.now()
    calls = 0

    def probe() -> str | None:
        nonlocal calls
        calls += 1
        return "ready" if calls >= _SUCCEED_ON_CALL else None

    result = foundry_poll.eventually(
        probe, timeout=_TIMEOUT_GENEROUS, interval=_INTERVAL, sleep=sleep, clock=foundry_clock
    )

    assert result == "ready", f"no truthy answer in {calls} polls"
    assert recorded == [_INTERVAL, _INTERVAL], "one wait per falsy poll, at the interval"
    assert foundry_clock.now() - started_at == timedelta(seconds=2 * _INTERVAL)
    assert _work.spend() >= 0


def test_eventually_gives_up_and_reports_polls(
    foundry_poll: FoundryPoll, foundry_clock: FoundryClock
) -> None:
    """Raise ``AssertionError`` naming the polls once the falsy probe exhausts the deadline."""
    sleep, recorded = _fake_sleep(foundry_clock)
    started_at = foundry_clock.now()

    def probe() -> bool:
        return False

    with pytest.raises(AssertionError) as excinfo:
        foundry_poll.eventually(
            probe, timeout=_TIMEOUT_TIGHT, interval=_INTERVAL, sleep=sleep, clock=foundry_clock
        )

    message = str(excinfo.value)
    assert type(excinfo.value) is AssertionError, f"a Foundry-specific type escaped: {message}"
    assert "poll" in message.lower() and any(ch.isdigit() for ch in message), message
    assert recorded, "the poller waited at least once before it gave up"
    assert foundry_clock.now() - started_at >= timedelta(seconds=_TIMEOUT_TIGHT)
    assert sum(recorded) == pytest.approx(_TIMEOUT_TIGHT, abs=1e-9)
    assert _work.spend() >= 0
