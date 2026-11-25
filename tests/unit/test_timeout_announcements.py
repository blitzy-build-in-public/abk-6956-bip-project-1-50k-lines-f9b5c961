"""Unit tests for the once-per-run announcement of a pytest-timeout peer fault.

Finding ``timeout-per-item-warning-flood`` (LOW): :func:`foundry.execution.timeout.apply_timer`
and :func:`foundry.execution.timeout.cancel_timer` are the bodies of pytest-timeout's two
per-item hookspecs, so each runs once per item. Announcing a peer fault from either of them
therefore wrote a ``WARNING`` for every item's set call and again for every item's cancel call --
two per test per attempt, for the life of the run. AAP 0.13.1 keeps ``WARNING`` for the
failure-isolation cases, and a peer that cannot be imported or refuses the derived settings is
one, so the level is right and the volume is the defect: the module now latches each distinct
fault through ``_announce_once``, the memo form of the once-per-run latch
:meth:`foundry.obs.artifacts.ArtifactWriter._warn_once` implements with an instance flag.

The memo is process-wide by design, which is exactly what makes the announcement happen once for
the run, so the autouse fixture below clears it around every test; a latched fault would otherwise
swallow the next test's announcement. Nothing here reads the clock, opens a socket or depends on
the order tests run in, so the module is safe under ``-n auto``.

The last case guards the other half of the same seam: ``cancel_timer`` must disarm the timer the
arm actually set, whatever the peer lookup answers at cancel time, because a surviving interval
timer fires into whatever the process runs next.
"""

from __future__ import annotations

import collections
import logging
import signal
from collections.abc import Iterator
from typing import Final

import pytest

from foundry.execution import timeout

_FOUNDRY_LOGGER: Final[str] = "foundry"
"""The one logger every Foundry module reports through (AAP 0.13.1 "Logging points")."""

_MISSING_PLUGIN_TEXT: Final[str] = "pytest-timeout is not importable"
"""Distinctive fragment of the missing-peer announcement."""

_REFUSED_SETTINGS_TEXT: Final[str] = "refused the derived timer settings"
"""Distinctive fragment of the refused-settings announcement."""

_PROBE_LIMIT_SECONDS: Final[float] = 60.0
"""The limit the leak probe arms, which is the default ``execution.timeout``.

Long on purpose: the probe is about whether a cancel clears the armed timer, never about the limit
itself, and a short one could fire into a loaded runner between the arm and the assertion.
"""


@pytest.fixture(autouse=True)
def _fresh_announcements() -> Iterator[None]:
    """Clear the once-per-run announcement memo around each test."""
    timeout._announce_once.cache_clear()
    yield
    timeout._announce_once.cache_clear()


class _RefusingCancelItem:
    """An item the installed pytest-timeout reaches and then fails on at its cancel seam.

    ``pytest_timeout_cancel_timer`` reads ``item.cancel_timeout`` and calls it when one is set, so
    the fault has to come from that callable: an item carrying no such attribute is a supported
    no-op in the peer, not a refusal.
    """

    def __init__(self) -> None:
        self.cancel_timeout = self._refuse

    def _refuse(self) -> None:
        """Raise what an incompatible release surfaces through the cancel seam."""
        raise AttributeError("cancel_timeout")


class _FakePeer:
    """A stand-in for the installed pytest-timeout that records what Foundry asks of it."""

    Settings = collections.namedtuple(
        "Settings", ("timeout", "method", "func_only", "disable_debugger_detection")
    )

    def __init__(self) -> None:
        self.armed: list[tuple[object, object]] = []
        self.cancelled: list[object] = []

    def pytest_timeout_set_timer(self, item: object, settings: object) -> bool:
        """Record the item and the settings the timer was armed with."""
        self.armed.append((item, settings))
        return True

    def pytest_timeout_cancel_timer(self, item: object) -> bool:
        """Record the item whose timer was cancelled."""
        self.cancelled.append(item)
        return True


class _ArmableItem:
    """A stand-in for a pytest item the real peer can arm, which ``object()`` cannot be.

    ``pytest_timeout_set_timer`` stores the closure that disarms the timer on the item as
    ``cancel_timeout``, so a probe of the arm/cancel balance needs an item that accepts the
    attribute.
    """


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the ``WARNING`` records caplog captured, in emission order."""
    return [record for record in caplog.records if record.levelno == logging.WARNING]


def test_refused_settings_announce_once_across_many_item_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Four per-item calls against a refusing peer log exactly one ``WARNING``."""
    policy = timeout.TimeoutPolicy(seconds=1.0)
    with caplog.at_level(logging.DEBUG, logger=_FOUNDRY_LOGGER):
        results = [
            timeout.apply_timer(object(), object(), policy),
            timeout.apply_timer(object(), object(), policy),
            timeout.cancel_timer(_RefusingCancelItem()),
            timeout.cancel_timer(_RefusingCancelItem()),
        ]
    assert results == [False, False, False, False]
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert _REFUSED_SETTINGS_TEXT in message
    assert "AttributeError" in message


def test_missing_peer_announces_once_across_many_item_calls(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A peer that cannot be imported logs exactly one ``WARNING`` for four per-item calls."""
    monkeypatch.setattr(timeout, "_load_timeout_module", lambda: None)
    policy = timeout.TimeoutPolicy(seconds=1.0)
    with caplog.at_level(logging.DEBUG, logger=_FOUNDRY_LOGGER):
        results = [
            timeout.apply_timer(object(), object(), policy),
            timeout.apply_timer(object(), object(), policy),
            timeout.cancel_timer(object()),
            timeout.cancel_timer(object()),
        ]
    assert results == [False, False, False, False]
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert _MISSING_PLUGIN_TEXT in warnings[0].getMessage()


def test_the_two_faults_are_latched_independently(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing peer cannot swallow the announcement of a refused-settings fault."""
    policy = timeout.TimeoutPolicy(seconds=1.0)
    with caplog.at_level(logging.DEBUG, logger=_FOUNDRY_LOGGER):
        with pytest.MonkeyPatch.context() as isolated:
            isolated.setattr(timeout, "_load_timeout_module", lambda: None)
            assert timeout.apply_timer(object(), object(), policy) is False
            assert timeout.cancel_timer(object()) is False
        assert timeout.apply_timer(object(), object(), policy) is False
        assert timeout.cancel_timer(_RefusingCancelItem()) is False
    warnings = _warnings(caplog)
    assert len(warnings) == 2
    assert _MISSING_PLUGIN_TEXT in warnings[0].getMessage()
    assert _REFUSED_SETTINGS_TEXT in warnings[1].getMessage()


def test_isolation_holds_and_a_healthy_peer_still_arms(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy peer is armed with the policy's limit, and nothing is warned about."""
    peer = _FakePeer()
    monkeypatch.setattr(timeout, "_load_timeout_module", lambda: peer)
    policy = timeout.TimeoutPolicy(seconds=7.5)
    item = object()
    with caplog.at_level(logging.DEBUG, logger=_FOUNDRY_LOGGER):
        assert timeout.apply_timer(object(), item, policy) is True
        assert timeout.cancel_timer(item) is True
    assert [armed_item for armed_item, _settings in peer.armed] == [item]
    assert peer.armed[0][1].timeout == policy.seconds
    assert peer.cancelled == [item]
    assert _warnings(caplog) == []


def test_cancelling_disarms_the_timer_armed_before_the_peer_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel disarms the armed timer even when the peer lookup now answers another module.

    Regression for the per-item timer leak: :func:`foundry.execution.timeout.apply_timer` armed the
    real ``ITIMER_REAL`` and stored the peer's disarm closure on the item, so a
    :func:`foundry.execution.timeout.cancel_timer` that re-resolved the peer and cancelled through
    *that* module left the real timer armed. It then fired into whatever the process ran next, which
    inside an xdist worker interrupted the worker's own loop and killed the worker mid-session.
    """
    policy = timeout.TimeoutPolicy(seconds=_PROBE_LIMIT_SECONDS)
    item = _ArmableItem()
    peer = _FakePeer()
    try:
        assert timeout.apply_timer(object(), item, policy) is True
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
        monkeypatch.setattr(timeout, "_load_timeout_module", lambda: peer)
        assert timeout.cancel_timer(item) is True
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    assert peer.cancelled == []
