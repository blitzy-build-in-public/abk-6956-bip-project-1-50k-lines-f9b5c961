"""Unit tests for ``foundry.execution``: retries, sharding, timeout and the state wire."""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Final

import pytest

from foundry.core.exceptions import ConfigError
from foundry.core.types import AttemptRecord
from foundry.execution import retry, sharding, state, timeout
from foundry.obs import capture
from foundry.obs.redaction import TRUNCATION_MARKER
from foundry.plugin import _in_flight

_RUN_ID: Final[str] = "20250102T030405-0abc12"
_NODEID: Final[str] = "tests/test_checkout.py::test_total[2]"
# Shared with tests.unit.test_execution_capture, which imports all three of these.
_PROFILE: Final[str] = "ci"
_ONE, _TWO, _THREE, _FOUR = (f"tests/test_{n}.py::test_{n}" for n in "abcd")


def _payload(
    *,
    outcome: state.OutcomeValue = "failed",
    attempts: int = 3,
    markers: tuple[str, ...] = ("slow", "foundry_quarantine"),
) -> state.TestPayload:
    return state.TestPayload(
        run_id=_RUN_ID,
        started_at="2025-01-02T03:04:05.000000+00:00",
        finished_at="2025-01-02T03:04:06.500000+00:00",
        profile=_PROFILE,
        nodeid=_NODEID,
        outcome=outcome,
        attempts=attempts,
        duration_ms=1234.5,
        markers=markers,
        failures=(state.FailurePayload(phase="call", message="cart", traceback_path="a/tb.txt"),),
        artifacts=(state.ArtifactPayload(kind="stdout", path="artifacts/stdout.log", bytes=42),),
        quarantine=state.QuarantinePayload(state="failed"),
        log_tail=("foundry: attempt 1 failed", "foundry: attempt 2 failed"),
    )


def _attempts(*outcomes: str) -> tuple[state.AttemptPayload, ...]:
    return tuple(state.AttemptPayload(n, o, float(n)) for n, o in enumerate(outcomes, 1))


def _service() -> sharding.ShardingService:
    return sharding.ShardingService(sharding.NodeLedger(), sharding.RunErrorLedger(), _RUN_ID)


@pytest.mark.parametrize(("repeats", "attempts"), [(0, 1), (2, 3), (10, 11)])
def test_max_attempts_counts_repeats_after_the_first(repeats: int, attempts: int) -> None:
    published = retry.ExecutionRetries.from_config(repeats).as_dict()
    assert published == {"repeats": repeats, "max_attempts": attempts, "disabled": repeats == 0}
    assert (retry.MIN_RETRIES, retry.MAX_RETRIES, retry.RETRYABLE_PHASES) == (0, 10, ("call",))


@pytest.mark.parametrize("repeats", [-1, 11, True, 1.5, "2"])
def test_a_budget_outside_the_published_range_is_a_config_error(repeats: object) -> None:
    with pytest.raises(ConfigError) as raised:
        retry.ExecutionRetries.from_config(repeats)  # type: ignore[arg-type]
    assert raised.value.key == retry.RETRIES_KEY and retry.RETRIES_RANGE_TEXT in str(raised.value)
    assert retry.RETRIES_KEY in (raised.value.hint or "")


@pytest.mark.parametrize(
    "row",
    [
        ("call", "failed", 1, True),
        ("call", "failed", 2, True),
        ("call", "failed", 3, False),
        ("call", "passed", 1, False),
        ("call", "error", 1, False),
        ("call", "skipped", 1, False),
        ("setup", "failed", 1, False),
        ("teardown", "failed", 1, False),
    ],
)
def test_should_retry_follows_the_published_rules(row: tuple[object, ...]) -> None:
    phase, outcome, used, expected = row
    policy = retry.ExecutionRetries.from_config(2)
    assert [policy.remaining(spent) for spent in (0, 1, 2, 3, 9)] == [3, 2, 1, 0, 0]
    decision = retry.should_retry(phase=phase, outcome=outcome, attempts_used=used, policy=policy)
    assert decision is expected
    assert retry.retry_label(AssertionError("cart")) == retry.LABEL_ASSERTION
    assert retry.describe_attempts(_attempts("failed", "passed")) == "2 attempts: failed, passed"
    message = retry.exhausted_message(_NODEID, _attempts("failed", "failed", "failed"))
    assert message.startswith(f"{_NODEID}: ") and "retries exhausted after 3 attempts" in message


def test_a_serial_and_a_partitioned_run_reconcile_and_a_lost_worker_is_a_run_error() -> None:
    serial = _service().verify([_TWO, _ONE])
    assert (serial.ok, serial.has_errors, serial.serial_count) == (True, False, 2)
    service = _service()
    service.nodes.record("gw0", [_TWO, _ONE, _FOUR])
    report = service.verify([_THREE, _ONE, _TWO])
    assert (report.ok, report.worker_ids, report.per_worker) == (False, ("gw0",), (("gw0", 3),))
    assert (report.missing, report.extra) == ((_THREE,), (_FOUR,))
    published = {sharding.WORKER_OUTPUT_KEY: (_TWO, _ONE)}
    assert sharding.record_worker_output(sharding.NodeLedger(), "gw0", published) == (_ONE, _TWO)
    lost = _service()
    rendered = lost.worker_down("gw1", "worker crashed: MemoryError").as_dict()
    assert set(rendered) == {"record_type", "run_id", "recorded_at", "worker_id", "message"}
    assert (rendered["record_type"], rendered["worker_id"]) == ("run_error", "gw1")
    assert "nodeid" not in rendered and "outcome" not in rendered
    assert datetime.fromisoformat(str(rendered["recorded_at"])) is not None
    assert (lost.errors.lost_workers(), lost.exit_code_contribution()) == (("gw1",), 1)
    assert lost.verify([]).worker_losses == ("gw1",)
    assert lost.missing_after_run([_ONE], expected=[_ONE, _TWO]) == (_TWO,)
    sentinel = "tok_live_9f3hunter2"
    secret = lost.worker_down("gw2", f"Authorization: Bearer {sentinel} " + "x" * 5000)
    assert sentinel not in secret.message and secret.message.endswith(TRUNCATION_MARKER)
    budget = sharding.RUN_ERROR_MESSAGE_MAX_BYTES + len(TRUNCATION_MARKER)
    assert len(secret.message.encode("utf-8")) <= budget


@pytest.mark.parametrize("seconds", [0.001, 60.0, 3600.0])
def test_the_settings_carry_the_seconds_and_publish_a_fresh_mapping(seconds: float) -> None:
    policy = timeout.TimeoutPolicy.from_config(seconds)
    assert (policy.seconds, policy.settings()["timeout"]) == (seconds, seconds)
    assert policy.for_item(_NODEID) == seconds
    assert policy.message(_NODEID) == f"foundry: test exceeded execution.timeout of {seconds}s"
    assert _NODEID not in policy.message(_NODEID)
    sixty = timeout.TimeoutPolicy.from_config(60.0)
    first, second = sixty.settings(), sixty.settings()
    assert first == second and first is not second
    first["timeout"] = 1.0
    assert second["timeout"] == 60.0 and sixty.settings()["timeout"] == 60.0
    assert (second["method"], second["func_only"]) == (timeout.TIMEOUT_METHOD_SIGNAL, True)
    assert frozenset({"signal", timeout.TIMEOUT_METHOD_THREAD}) == timeout.TIMEOUT_METHODS
    disabled = timeout.TimeoutPolicy.disabled()
    assert (disabled.enabled, disabled.is_enabled) == (False, False)
    assert disabled.seconds == disabled.for_item(_NODEID) == timeout.MIN_TIMEOUT_SECONDS
    assert sixty.is_enabled is True


@pytest.mark.parametrize("seconds", [0.0, -1.0, 3600.5, float("nan"), float("inf"), "sixty"])
def test_a_limit_outside_the_published_range_is_a_config_error(seconds: object) -> None:
    with pytest.raises(ConfigError) as raised:
        timeout.TimeoutPolicy.from_config(seconds)  # type: ignore[arg-type]
    assert raised.value.key == timeout.TIMEOUT_KEY == "execution.timeout"
    assert timeout.TIMEOUT_RANGE_TEXT in str(raised.value)


def test_the_stash_keys_are_typed_and_distinct_and_the_payload_round_trips() -> None:
    keys = (*state.SESSION_STASH_KEYS, *state.ITEM_STASH_KEYS)
    assert len(keys) == len({id(key) for key in keys}) == 11
    assert all(isinstance(key, pytest.StashKey) for key in keys)
    assert set(map(id, state.SESSION_STASH_KEYS)).isdisjoint(map(id, state.ITEM_STASH_KEYS))
    stash: pytest.Stash = pytest.Stash()
    stash[state.SESSION_STATE] = "session-state"
    assert stash[state.SESSION_STATE] == "session-state"
    with pytest.raises(KeyError):
        stash[state.ITEM_ATTEMPTS]
    payload = _payload()
    report = SimpleNamespace(user_properties=[])
    state.write_properties(report, payload)
    names = [name for name, _ in report.user_properties]
    assert names == sorted(names) and all(state.is_foundry_property(n) for n in names)
    assert state.read_properties(report) == payload
    # xdist decodes pairs as two-element lists, and a second stamp replaces Foundry's entries.
    report.user_properties = json.loads(json.dumps(report.user_properties))
    assert state.read_properties(report) == payload
    state.write_properties(report, _payload(outcome="passed", attempts=1))
    assert state.read_properties(report) == _payload(outcome="passed", attempts=1)
    not_a_list = SimpleNamespace(user_properties=("foundry.run_id", _RUN_ID))
    corrupt = SimpleNamespace(user_properties=[])
    state.write_properties(corrupt, _payload())
    corrupt.user_properties = [
        ("foundry.attempts", 0) if name == "foundry.attempts" else (name, value)
        for name, value in corrupt.user_properties
    ]
    broken = (
        object(),
        SimpleNamespace(user_properties=[]),
        SimpleNamespace(user_properties=[("foundry.run_id", _RUN_ID)]),
        SimpleNamespace(user_properties=[("foundry.artifacts", "{not json")]),
        not_a_list,
        corrupt,
    )
    assert [state.read_properties(one) for one in broken] == [None] * len(broken)
    state.write_properties(object(), _payload())
    record = AttemptRecord(number=2, outcome="failed", elapsed_ms=12.5, message="cart")
    wire = state.AttemptPayload.from_record(record)
    assert wire.as_dict() == record.as_dict() and wire.to_record() == record
    assert state.AttemptPayload.from_dict(json.loads(state.dumps(wire.as_dict()))) == wire
    assert state.AttemptPayload.from_dict({"outcome": "failed"}) is None
    assert state.AttemptPayload.from_dict({**wire.as_dict(), "number": 0}) is None
    loss = _service().worker_down("gw2", "worker exited with status 1")
    assert state.RunErrorPayload.from_dict(json.loads(state.dumps(loss.as_dict()))) == loss
    assert state.RunErrorPayload.from_dict({**loss.as_dict(), "record_type": "result"}) is None
    canonical = _payload(markers=("zeta", "alpha", "alpha"))
    assert canonical.markers == ("alpha", "zeta")
    names = [name for name, _ in canonical.as_properties()]
    assert names == sorted(names) == list(state.FOUNDRY_PROPERTY_NAMES)
    assert (len(names), state.PROPERTY_PREFIX) == (14, "foundry.")
    assert " ".join(state.OUTCOME_VALUES) == (
        "passed failed skipped xfailed xpassed error quarantined_failed quarantine_passed not_run"
    )


@pytest.mark.parametrize(
    "row", [(1, 1_000.25, (2, 250.0)), (0, None, (1, 500.0)), (2, None, (3, 0.0))]
)
def test_the_in_flight_attempt_measures_its_own_span(
    row: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior, attempt_start, expected = row
    stash = {state.ITEM_STARTED_MONOTONIC: 1_000.0}
    stash[state.ITEM_ATTEMPTS] = [AttemptRecord(n, "failed", 0.0) for n in range(prior)]
    if attempt_start is not None:
        stash[state.ITEM_ATTEMPT_STARTED_MONOTONIC] = attempt_start
    monkeypatch.setattr(time, "monotonic", lambda: 1_000.5)
    in_flight = _in_flight(SimpleNamespace(stash=stash), "passed")
    assert (in_flight.number, pytest.approx(in_flight.elapsed_ms)) == expected


@pytest.mark.parametrize(
    "row",
    [(2, ("l1", "l2", "l3"), ("l2", "l3")), (0, ("l1", "l2"), ()), (9, ("l1", "l2"), ("l1", "l2"))],
)
def test_bounded_tail_keeps_the_newest_lines_within_both_bounds(row: tuple) -> None:
    limit, lines, expected = row
    assert state.bounded_tail(lines, limit) == expected
    tail = state.bounded_tail(["x" * (capture.MAX_TAIL_LINE_LENGTH * 2)], 5)
    assert len(tail) == 1 and len(tail[0]) == capture.MAX_TAIL_LINE_LENGTH
    assert tail[0].endswith(capture.TRUNCATION_MARKER)
