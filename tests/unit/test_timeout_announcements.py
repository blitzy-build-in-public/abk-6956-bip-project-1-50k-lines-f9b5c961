"""``apply_timer`` and ``cancel_timer`` are per-item hookspec bodies, so a peer fault announced from
either would warn twice per test per attempt; each distinct fault is latched once per run, and a
cancel disarms the timer through the closure the peer stored on the item it armed."""

import collections
import logging
import signal
from collections.abc import Iterator
from typing import Final

import pytest

from foundry.execution import timeout

_LOGGER: Final[str] = "foundry"


@pytest.fixture(autouse=True)
def _fresh_announcements() -> Iterator[None]:
    timeout._announce_once.cache_clear()
    yield
    timeout._announce_once.cache_clear()


class _RefusingCancelItem:
    def __init__(self) -> None:
        self.cancel_timeout = self._refuse

    def _refuse(self) -> None:
        raise AttributeError("cancel_timeout")


class _ArmableItem:
    cancel_timeout: object


class _FakePeer:
    Settings = collections.namedtuple("S", "timeout method func_only disable_debugger_detection")

    def __init__(self) -> None:
        self.armed: list[tuple[object, object]] = []
        self.cancelled: list[object] = []

    def pytest_timeout_set_timer(self, item: object, settings: object) -> bool:
        self.armed.append((item, settings))
        return True

    def pytest_timeout_cancel_timer(self, item: object) -> bool:
        self.cancelled.append(item)
        return True


def test_each_distinct_fault_is_announced_once_across_many_item_calls(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = timeout.TimeoutPolicy(seconds=1.0)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        refused = [timeout.apply_timer(object(), object(), policy)]
        refused.append(timeout.cancel_timer(_RefusingCancelItem()))
        monkeypatch.setattr(timeout, "_load_timeout_module", lambda: None)
        missing = [timeout.apply_timer(object(), object(), policy)]
        missing.append(timeout.cancel_timer(object()))
    assert refused + missing == [False] * 4
    # A missing peer cannot swallow the announcement of a refused-settings fault, or the reverse.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "refused the derived timer settings" in warnings[0] and "AttributeError" in warnings[0]
    assert "pytest-timeout is not importable" in warnings[1]


def test_a_healthy_peer_is_armed_with_the_policys_limit_and_nothing_is_warned(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer, policy, item = _FakePeer(), timeout.TimeoutPolicy(seconds=7.5), object()
    monkeypatch.setattr(timeout, "_load_timeout_module", lambda: peer)
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert timeout.apply_timer(object(), item, policy) is True
        assert timeout.cancel_timer(item) is True
    assert [(armed, settings.timeout) for armed, settings in peer.armed] == [(item, policy.seconds)]
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_cancelling_disarms_the_timer_armed_before_the_peer_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item, peer = _ArmableItem(), _FakePeer()
    try:
        assert timeout.apply_timer(object(), item, timeout.TimeoutPolicy(seconds=60.0)) is True
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
        monkeypatch.setattr(timeout, "_load_timeout_module", lambda: peer)
        assert timeout.cancel_timer(item) is True
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
    assert peer.cancelled == []
