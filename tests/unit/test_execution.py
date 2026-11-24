"""Unit tests for ``foundry.execution``: the retry budget, quarantine, sharding, timeout, the wire.

Five modules, each pinned to the contract AAP 0.4.5 to 0.4.8 give it: A2's retry arithmetic,
the exit-code rule of both quarantine modes with A11's ``quarantine_passed``, sharding's collection
comparison and the ``run_error`` a lost worker leaves, pytest-timeout's derived settings, and the
typed stash keys with the ``user_properties`` codec (implicit requirement I3).

No test here drives pytest machinery, sleeps, reads the wall clock or opens a socket (AAP 0.14.3),
and the one subprocess proves ``foundry.execution.state`` pulls in no heavy dependency (I5).
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Final

import pytest

from foundry.config.enums import QuarantineMode
from foundry.core.exceptions import ConfigError
from foundry.core.types import AttemptRecord, Phase
from foundry.execution import quarantine, retry, sharding, state, timeout
from foundry.obs import capture
from foundry.obs.redaction import TRUNCATION_MARKER as REDACTION_TRUNCATION_MARKER

# Fixed run id, profile, node id and timestamps, so no assertion reads the wall clock.
_RUN_ID: Final[str] = "20250102T030405-0abc12"
_PROFILE: Final[str] = "ci"
_NODEID: Final[str] = "tests/test_checkout.py::test_total[2]"
_STARTED_AT: Final[str] = "2025-01-02T03:04:05.000000+00:00"
_FINISHED_AT: Final[str] = "2025-01-02T03:04:06.500000+00:00"

_HEAVY_MODULES: Final[tuple[str, ...]] = (
    "sqlalchemy",
    "faker",
    "jsonschema",
    "jinja2",
    "rich",
    "httpx",
)
"""The libraries AAP 0.14.2's startup budget keeps out of ``plugin.py``'s import path."""

_IMPORT_PROBE: Final[str] = (
    "import sys, foundry.execution.state; "
    f"print(sorted(name for name in {_HEAVY_MODULES!r} if name in sys.modules))"
)
"""The child-process one-liner of implicit requirement I5: print what the import dragged in."""


class _Report:
    """The one attribute the ``user_properties`` codec reads from a ``pytest.TestReport``.

    The stand-in keeps the assertion on the wire format without a full runtest protocol.
    """

    def __init__(self, properties: list[tuple[str, object]] | None = None) -> None:
        self.user_properties: list[tuple[str, object]] = list(properties or [])


def _payload(
    *,
    outcome: state.OutcomeValue = "failed",
    attempts: int = 3,
    markers: tuple[str, ...] = ("slow", "foundry_quarantine"),
) -> state.TestPayload:
    """Return one test's wire payload with every field a worker can fill set."""
    return state.TestPayload(
        run_id=_RUN_ID,
        started_at=_STARTED_AT,
        finished_at=_FINISHED_AT,
        profile=_PROFILE,
        nodeid=_NODEID,
        outcome=outcome,
        attempts=attempts,
        duration_ms=1234.5,
        markers=markers,
        failures=(
            state.FailurePayload(
                phase="call",
                message="cart total was 1100, expected 1200",
                traceback_path="artifacts/traceback.txt",
            ),
        ),
        artifacts=(
            state.ArtifactPayload(kind="stdout", path="artifacts/stdout.log", bytes=42),
            state.ArtifactPayload(kind="log_tail", path="artifacts/log_tail.log", bytes=17),
        ),
        quarantine=state.QuarantinePayload(state="failed"),
        log_tail=("foundry: attempt 1 failed", "foundry: attempt 2 failed"),
        artifacts_disabled=False,
    )


def _attempts(*outcomes: str) -> tuple[state.AttemptPayload, ...]:
    """Return an attempt ledger for ``outcomes``, numbered from one, oldest first."""
    return tuple(
        state.AttemptPayload(number=number, outcome=outcome, elapsed_ms=float(number))
        for number, outcome in enumerate(outcomes, start=1)
    )


def _service() -> sharding.ShardingService:
    """Return a sharding service over fresh ledgers, for the run id the payloads use."""
    return sharding.ShardingService(
        nodes=sharding.NodeLedger(), errors=sharding.RunErrorLedger(), run_id=_RUN_ID
    )


@pytest.mark.parametrize(("repeats", "attempts"), [(0, 1), (2, 3), (10, 11)])
def test_max_attempts_counts_repeats_after_the_first(repeats: int, attempts: int) -> None:
    """``execution.retries`` counts repeats, so ``max_attempts`` is ``repeats + 1`` (A2)."""
    policy = retry.ExecutionRetries.from_config(repeats)
    assert policy.repeats == repeats
    assert policy.max_attempts == attempts
    assert policy.disabled is (repeats == 0)
    assert retry.ExecutionRetries.from_config(repeats).remaining(attempts) == 0


@pytest.mark.parametrize(("attempts_used", "remaining"), [(0, 3), (1, 2), (2, 1), (3, 0), (9, 0)])
def test_remaining_clamps_at_zero_once_the_budget_is_spent(
    attempts_used: int, remaining: int
) -> None:
    """A spent or overrun budget reads as ``0``, never as a negative count."""
    assert retry.ExecutionRetries.from_config(2).remaining(attempts_used) == remaining


@pytest.mark.parametrize("repeats", [-1, 11, True, 1.5, "2"])
def test_a_budget_outside_the_published_range_is_a_config_error(repeats: object) -> None:
    """AAP 0.4.5's range ``0..10`` holds on every construction route, not only on a config read."""
    with pytest.raises(ConfigError) as excinfo:
        retry.ExecutionRetries.from_config(repeats)  # type: ignore[arg-type]
    assert excinfo.value.key == retry.RETRIES_KEY
    assert retry.RETRIES_RANGE_TEXT in str(excinfo.value)
    assert excinfo.value.hint is not None
    assert retry.RETRIES_KEY in excinfo.value.hint


def test_the_budget_publishes_its_repeats_and_its_attempts() -> None:
    """The diagnostic mapping names both counts, so a reader cannot mistake one for the other."""
    assert retry.ExecutionRetries.from_config(2).as_dict() == {
        "repeats": 2,
        "max_attempts": 3,
        "disabled": False,
    }
    assert (retry.MIN_RETRIES, retry.MAX_RETRIES) == (0, 10)
    assert retry.RETRYABLE_PHASES == ("call",)


@pytest.mark.parametrize(
    ("phase", "outcome", "attempts_used", "expected"),
    [
        ("call", "failed", 1, True),
        ("call", "failed", 2, True),
        ("call", "failed", 3, False),
        ("call", "failed", 4, False),
        ("call", "passed", 1, False),
        ("call", "error", 1, False),
        ("call", "skipped", 1, False),
        ("setup", "failed", 1, False),
        ("teardown", "failed", 1, False),
    ],
)
def test_should_retry_stops_on_a_pass_a_fixture_fault_or_a_spent_budget(
    phase: Phase, outcome: str, attempts_used: int, expected: bool
) -> None:
    """The decision continues only on a retryable call-phase failure with budget left."""
    policy = retry.ExecutionRetries.from_config(2)
    decision = retry.should_retry(
        phase=phase, outcome=outcome, attempts_used=attempts_used, policy=policy
    )
    assert decision is expected


def test_the_label_names_how_an_attempt_ended() -> None:
    """A label is diagnostic: the HTTP vocabulary where it applies, Foundry's labels otherwise."""
    assert retry.retry_label(None) is None
    assert retry.retry_label(None, status_code=200) is None
    assert retry.retry_label(None, status_code=503) == "status_5xx"
    assert retry.retry_label(None, status_code=429) == "status_429"
    assert retry.retry_label(AssertionError("cart total")) == retry.LABEL_ASSERTION
    assert retry.retry_label(RuntimeError("database went away")) == retry.LABEL_ERROR


def test_the_attempt_summary_is_one_chronological_line() -> None:
    """The summary keeps attempt order and stays on one line, safe in a failure message."""
    summary = retry.describe_attempts(_attempts("failed", "failed", "passed"))
    assert summary == "3 attempts: failed, failed, passed"
    assert "\n" not in summary
    assert retry.describe_attempts(_attempts("failed")) == "1 attempt: failed"
    assert retry.describe_attempts(()) == "no attempts"
    message = retry.exhausted_message(_NODEID, _attempts("failed", "failed", "failed"))
    assert message.startswith(f"{_NODEID}: ")
    assert "retries exhausted after 3 attempts" in message


@pytest.mark.parametrize(
    ("mode", "quarantined", "outcome", "expected_state", "expected_outcome", "fails_run"),
    [
        ("report", True, "failed", "failed", "quarantined_failed", False),
        ("strict", True, "failed", "failed", "quarantined_failed", True),
        ("report", True, "error", "failed", "quarantined_failed", False),
        ("report", False, "failed", "none", "failed", True),
        ("strict", False, "failed", "none", "failed", True),
        ("report", True, "passed", "passed", "quarantine_passed", False),
        ("strict", True, "passed", "passed", "quarantine_passed", False),
        ("report", False, "passed", "none", "passed", False),
        ("report", True, "skipped", "none", "skipped", False),
        ("report", False, "not_run", "none", "not_run", False),
    ],
)
def test_decide_follows_the_published_table(
    mode: str,
    quarantined: bool,
    outcome: state.OutcomeValue,
    expected_state: str,
    expected_outcome: state.OutcomeValue,
    fails_run: bool,
) -> None:
    """Every row of AAP 0.4.7's table, with the state, the outcome and the exit-code verdict."""
    registry = quarantine.QuarantineRegistry(mode=mode)
    decision = registry.decide(quarantined=quarantined, outcome=outcome)
    assert decision.state == expected_state
    assert decision.outcome == expected_outcome
    assert decision.fails_run is fails_run
    assert decision.listed is (expected_state != "none")
    assert decision.as_payload() == state.QuarantinePayload(state=expected_state)


def test_report_mode_keeps_the_run_green_where_strict_mode_fails_it() -> None:
    """The two modes produce one record and two exit codes: the whole difference between them."""
    report = quarantine.QuarantineRegistry(mode=QuarantineMode.REPORT).decide(
        quarantined=True, outcome="failed"
    )
    strict = quarantine.QuarantineRegistry(mode=QuarantineMode.STRICT).decide(
        quarantined=True, outcome="failed"
    )
    assert report.outcome == strict.outcome == "quarantined_failed"
    assert report.fails_run is False
    assert strict.fails_run is True
    assert quarantine.QuarantineRegistry(mode="report").run_failed([report]) is False
    assert quarantine.QuarantineRegistry(mode="strict").run_failed([strict]) is True


def test_a_quarantined_pass_is_a_success_carrying_the_removal_note() -> None:
    """Assumption A11: a green marked test succeeds, and the note asks for the marker's removal."""
    decision = quarantine.QuarantineRegistry().decide(quarantined=True, outcome="passed")
    assert decision.outcome == "quarantine_passed"
    assert decision.fails_run is False
    assert quarantine.QuarantineRegistry().run_failed([decision]) is False
    assert decision.note == quarantine.QUARANTINE_PASSED_NOTE
    assert decision.note is not None
    assert "remove the foundry_quarantine marker" in decision.note
    assert decision.as_dict()["listed"] is True


@pytest.mark.parametrize("mode", [QuarantineMode.REPORT, QuarantineMode.STRICT])
def test_deciding_an_already_decided_outcome_changes_nothing(mode: QuarantineMode) -> None:
    """A record decoded from the wire and decided again keeps the verdict it already carries."""
    registry = quarantine.QuarantineRegistry(mode=mode)
    for outcome in quarantine.quarantined_outcomes():
        first = registry.decide(quarantined=True, outcome=outcome)
        assert registry.decide(quarantined=True, outcome=first.outcome) == first
        assert registry.decide(quarantined=False, outcome=first.outcome) == first


@pytest.mark.parametrize(("mode", "failing_in_strict"), [("report", 0), ("strict", 1)])
def test_the_counts_tally_the_verdicts_and_the_strict_share(
    mode: str, failing_in_strict: int
) -> None:
    """The tallies name how many marked failures the run itself counts as failures."""
    registry = quarantine.QuarantineRegistry(mode=mode)
    decisions = [
        registry.decide(quarantined=True, outcome="passed"),
        registry.decide(quarantined=True, outcome="failed"),
        registry.decide(quarantined=False, outcome="passed"),
    ]
    counts = dict(registry.counts(decisions))
    assert counts == {"failed": 1, "failing_in_strict": failing_in_strict, "passed": 1}


@pytest.mark.parametrize(
    ("markers", "expected"),
    [
        (["foundry_quarantine"], True),
        (["slow", "foundry_quarantine"], True),
        (["slow"], False),
        ([], False),
    ],
)
def test_is_quarantined_matches_the_marker_name_alone(markers: list[str], expected: bool) -> None:
    """Only the registered marker name counts; the namespace prefix alone does not."""
    assert quarantine.is_quarantined(markers) is expected
    assert quarantine.is_quarantined(iter(markers)) is expected


def test_a_mode_name_resolves_and_an_unknown_mode_falls_back_to_report() -> None:
    """A value read from configuration needs no cast, and a defect never fails a run."""
    assert quarantine.QuarantineRegistry(mode="strict").is_strict is True
    assert quarantine.QuarantineRegistry(mode="STRICT").mode is QuarantineMode.STRICT
    assert quarantine.QuarantineRegistry(mode="nonexistent").mode is QuarantineMode.REPORT
    assert quarantine.QuarantineRegistry(mode="strict").label == "quarantine=strict"


def test_the_marker_vocabulary_is_the_published_one() -> None:
    """The names AAP 0.4.2, 0.4.6 and 0.4.9 fix, spelled as the report republishes them."""
    assert quarantine.QUARANTINE_MARKER == "foundry_quarantine"
    assert quarantine.MARKER_NAMESPACE_PREFIX == "foundry_"
    assert quarantine.QUARANTINED_SKIP_MESSAGE == "foundry: quarantined failure"
    assert quarantine.quarantined_outcomes() == ("quarantined_failed", "quarantine_passed")


_ONE: Final[str] = "tests/test_a.py::test_one"
_TWO: Final[str] = "tests/test_b.py::test_two"
_THREE: Final[str] = "tests/test_c.py::test_three"
_FOUR: Final[str] = "tests/test_d.py::test_four"


def test_a_serial_run_compares_the_collection_with_itself() -> None:
    """No worker and no loss is a serial run: the controller ran every item itself."""
    report = _service().verify([_TWO, _ONE])
    assert report.ok is True
    assert report.has_errors is False
    assert report.serial_count == report.distributed_count == 2
    assert report.missing == report.duplicate == report.extra == report.worker_losses == ()


def test_a_partitioned_run_covers_the_collection_exactly() -> None:
    """Set equality is order-insensitive, and the per-worker counts are sorted by worker id."""
    service = _service()
    service.nodes.record("gw1", [_THREE])
    service.nodes.record("gw0", [_TWO, _ONE])
    report = service.verify([_THREE, _ONE, _TWO])
    assert report.ok is True
    assert report.worker_ids == ("gw0", "gw1")
    assert report.per_worker == (("gw0", 2), ("gw1", 1))
    assert report.distributed_count == 3
    assert report.as_dict()["missing"] == []
    assert "\n" not in report.summary()


def test_missing_and_extra_node_ids_are_named_and_sorted() -> None:
    """A node id one side lacks is reported by name, in sorted order, in each direction."""
    service = _service()
    service.nodes.record("gw0", [_FOUR, _ONE])
    report = service.verify([_THREE, _TWO, _ONE])
    assert report.missing == (_TWO, _THREE)
    assert report.extra == (_FOUR,)
    assert report.ok is False
    assert report.has_errors is False
    assert "missing=2" in report.summary()
    assert "extra=1" in report.summary()


def test_a_node_id_two_workers_ran_is_a_duplicate() -> None:
    """Distribution divergence inside the work queue shows up as a duplicate, not as a miss."""
    service = _service()
    service.nodes.record("gw0", [_ONE])
    service.nodes.record("gw1", [_ONE])
    report = service.verify([_ONE])
    assert report.duplicate == (_ONE,)
    assert report.ok is False


def test_a_worker_loss_is_a_run_error_naming_the_worker_and_no_test() -> None:
    """AAP 0.4.6's element: five fields, a worker id, and no node id and no outcome."""
    service = _service()
    payload = service.worker_down("gw1", "worker crashed: MemoryError")
    rendered = payload.as_dict()
    assert set(rendered) == {"record_type", "run_id", "recorded_at", "worker_id", "message"}
    assert rendered["record_type"] == "run_error"
    assert rendered["run_id"] == _RUN_ID
    assert rendered["worker_id"] == "gw1"
    assert "nodeid" not in rendered
    assert "outcome" not in rendered
    assert "MemoryError" in payload.message
    assert datetime.fromisoformat(payload.recorded_at) is not None
    assert json.loads(json.dumps(rendered)) == rendered
    assert service.errors.lost_workers() == ("gw1",)
    assert service.exit_code_contribution() == 1
    report = service.verify([])
    assert report.has_errors is True
    assert report.worker_losses == ("gw1",)
    assert report.ok is False


def test_a_repeated_loss_of_one_worker_is_recorded_once() -> None:
    """``pytest_testnodedown`` can fire twice for one crash; one lost worker stays one record."""
    service = _service()
    service.worker_down("gw1", "worker crashed")
    service.worker_down("gw1", "worker crashed")
    assert service.errors.count == 1
    assert service.worker_down("gw1", "worker exited").worker_id == "gw1"
    assert service.errors.count == 2
    assert service.errors.lost_workers() == ("gw1",)


def test_the_run_error_message_is_redacted_before_it_is_bounded() -> None:
    """A secret a worker echoed never reaches the record, and the message stays bounded."""
    sentinel = "tok_live_9f3hunter2"
    payload = _service().worker_down("gw0", f"Authorization: Bearer {sentinel} " + "x" * 5000)
    assert sentinel not in payload.message
    assert len(payload.message) < 5000
    assert len(payload.message.encode("utf-8")) <= (
        sharding.RUN_ERROR_MESSAGE_MAX_BYTES + len(REDACTION_TRUNCATION_MARKER)
    )
    assert payload.message.endswith(REDACTION_TRUNCATION_MARKER)


def test_missing_after_run_names_the_items_no_worker_reported() -> None:
    """A worker lost mid-run leaves collected items with no result, and they are named."""
    service = _service()
    service.verify([_ONE, _TWO, _THREE])
    assert service.missing_after_run([_TWO, _ONE]) == (_THREE,)
    assert service.missing_after_run([], expected=["b::2", "a::1"]) == ("a::1", "b::2")


def test_record_worker_output_reads_the_xdist_channel() -> None:
    """A worker that published nothing -- the lost-worker path -- records nothing, silently."""
    ledger = sharding.NodeLedger()
    published = {sharding.WORKER_OUTPUT_KEY: (_TWO, _ONE)}
    assert sharding.record_worker_output(ledger, "gw0", published) == (_ONE, _TWO)
    assert sharding.record_worker_output(ledger, "gw0", published) == (_ONE, _TWO)
    assert sharding.record_worker_output(ledger, "gw1", None) == ()
    assert sharding.record_worker_output(ledger, "gw2", {}) == ()
    single = {sharding.WORKER_OUTPUT_KEY: "tests/test_solo.py::test_one"}
    assert sharding.record_worker_output(ledger, "gw3", single) == ("tests/test_solo.py::test_one",)


@pytest.mark.parametrize("seconds", [0.001, 60.0, 3600.0])
def test_the_settings_carry_the_configured_seconds(seconds: float) -> None:
    """Every boundary of ``0 < t <= 3600`` reaches the timer as the configured value."""
    policy = timeout.TimeoutPolicy.from_config(seconds)
    assert policy.seconds == seconds
    assert policy.settings()["timeout"] == seconds
    assert policy.for_item(_NODEID) == seconds
    assert policy.message(_NODEID) == f"foundry: test exceeded execution.timeout of {seconds}s"
    assert _NODEID not in policy.message(_NODEID)


def test_the_derived_settings_are_a_fresh_pure_mapping() -> None:
    """Two derivations agree, and neither the caller's copy nor a mutation reaches the policy."""
    policy = timeout.TimeoutPolicy.from_config(60.0)
    first = policy.settings()
    second = policy.settings()
    assert first == second
    assert first is not second
    first["timeout"] = 1.0
    assert policy.settings()["timeout"] == 60.0
    assert timeout.TimeoutPolicy.from_config(60.0).settings() == second


def test_the_method_and_the_call_phase_flag_are_the_documented_ones() -> None:
    """The timer method is pytest-timeout's own default, and it covers the call phase only."""
    assert timeout.DEFAULT_TIMEOUT_METHOD == timeout.TIMEOUT_METHOD_SIGNAL == "signal"
    assert timeout.TIMEOUT_METHOD_THREAD == "thread"
    assert frozenset({"signal", "thread"}) == timeout.TIMEOUT_METHODS
    assert timeout.FUNC_ONLY is True
    settings = timeout.TimeoutPolicy.from_config(30.0).settings()
    assert settings["method"] == timeout.TIMEOUT_METHOD_SIGNAL
    assert settings["func_only"] is True
    assert timeout.TimeoutPolicy(seconds=30.0, method="thread").method == "thread"
    assert timeout.TimeoutPolicy(seconds=30.0, method="nonsense").method == (
        timeout.DEFAULT_TIMEOUT_METHOD
    )


def test_a_disabled_policy_arms_nothing() -> None:
    """``-p no:timeout`` is a disabled policy, which leaves pytest-timeout's own timer in place."""
    policy = timeout.TimeoutPolicy.disabled()
    assert policy.enabled is False
    assert policy.is_enabled is False
    assert policy.seconds == timeout.MIN_TIMEOUT_SECONDS
    assert policy.for_item(_NODEID) == timeout.MIN_TIMEOUT_SECONDS
    assert timeout.TimeoutPolicy.from_config(60.0).is_enabled is True


@pytest.mark.parametrize("seconds", [0.0, -1.0, 3600.5, float("nan"), float("inf"), "sixty"])
def test_a_limit_outside_the_published_range_is_a_config_error(seconds: object) -> None:
    """AAP 0.4.5's ``0 < t <= 3600``, named with the key and the accepted range in the message."""
    with pytest.raises(ConfigError) as excinfo:
        timeout.TimeoutPolicy.from_config(seconds)  # type: ignore[arg-type]
    assert excinfo.value.key == timeout.TIMEOUT_KEY
    assert timeout.TIMEOUT_RANGE_TEXT in str(excinfo.value)
    assert timeout.TIMEOUT_KEY == "execution.timeout"


def test_the_stash_keys_are_typed_distinct_and_disjoint() -> None:
    """One key per piece of state: no two modules can claim a slot, and none is a bare string."""
    keys = (*state.SESSION_STASH_KEYS, *state.ITEM_STASH_KEYS)
    assert len(keys) == 10
    assert len({id(key) for key in keys}) == len(keys)
    assert all(isinstance(key, pytest.StashKey) for key in keys)
    assert state.SESSION_STATE is not state.SESSION_CONFIG
    assert set(map(id, state.SESSION_STASH_KEYS)).isdisjoint(map(id, state.ITEM_STASH_KEYS))


def test_a_value_stored_through_one_key_is_unreadable_through_another() -> None:
    """A stash is keyed by the object itself, which is why two declarations would be two slots."""
    stash: pytest.Stash = pytest.Stash()
    stash[state.SESSION_STATE] = "session-state"
    assert stash[state.SESSION_STATE] == "session-state"
    with pytest.raises(KeyError):
        stash[state.ITEM_ATTEMPTS]


def test_the_payload_round_trips_through_user_properties() -> None:
    """Serialise, decode and compare: the guarantee that a worker's state reaches the controller."""
    payload = _payload()
    report = _Report()
    state.write_properties(report, payload)
    names = [name for name, _ in report.user_properties]
    assert names == sorted(names)
    assert all(state.is_foundry_property(name) for name in names)
    values = [value for _, value in report.user_properties]
    assert all(isinstance(value, (str, int, float, bool)) or value is None for value in values)
    json.dumps(report.user_properties)
    assert state.read_properties(report) == payload
    decoded = state.read_properties(report)
    assert decoded is not None
    assert decoded.failures == payload.failures
    assert decoded.artifacts == payload.artifacts
    assert decoded.quarantine == payload.quarantine
    assert decoded.log_tail == payload.log_tail
    # xdist decodes the pairs as two-element lists, so one document must decode either way.
    report.user_properties = json.loads(json.dumps(report.user_properties))
    assert state.read_properties(report) == payload


def test_write_properties_replaces_foundry_entries_and_keeps_foreign_ones() -> None:
    """A second stamp replaces Foundry's entries rather than appending a rival set of them."""
    report = _Report([("vendor.owner", "checkout")])
    state.write_properties(report, _payload())
    state.write_properties(report, _payload(outcome="passed", attempts=1))
    assert report.user_properties[0] == ("vendor.owner", "checkout")
    names = [name for name, _ in report.user_properties]
    assert len(names) == len(set(names))
    assert state.read_properties(report) == _payload(outcome="passed", attempts=1)


def test_read_properties_returns_none_for_a_missing_partial_or_corrupt_set() -> None:
    """The decoder is the tolerant inverse: a plumbing fault yields ``None``, never an exception."""
    assert state.read_properties(object()) is None
    assert state.read_properties(_Report()) is None
    assert state.read_properties(_Report([("foundry.run_id", _RUN_ID)])) is None
    assert state.read_properties(_Report([("foundry.artifacts", "{not json")])) is None
    not_a_list = _Report()
    not_a_list.user_properties = ("foundry.run_id", _RUN_ID)
    assert state.read_properties(not_a_list) is None
    corrupt = _Report()
    state.write_properties(corrupt, _payload())
    corrupt.user_properties = [
        ("foundry.attempts", 0) if name == "foundry.attempts" else (name, value)
        for name, value in corrupt.user_properties
    ]
    assert state.read_properties(corrupt) is None
    state.write_properties(object(), _payload())


def test_the_attempt_payload_mirrors_the_attempt_record() -> None:
    """``AttemptPayload`` is ``AttemptRecord`` on the wire: what the error and the report read."""
    record = AttemptRecord(
        number=2,
        outcome="failed",
        elapsed_ms=12.5,
        retry_on=retry.LABEL_ASSERTION,
        message="cart total",
    )
    wire = state.AttemptPayload.from_record(record)
    assert wire.as_dict() == record.as_dict()
    assert wire.to_record() == record
    decoded = state.AttemptPayload.from_dict(json.loads(state.dumps(wire.as_dict())))
    assert decoded == wire
    assert decoded is not None
    assert decoded.to_record() == record
    assert state.AttemptPayload.from_dict({"outcome": "failed"}) is None
    assert state.AttemptPayload.from_dict({**wire.as_dict(), "number": 0}) is None


def test_the_run_error_payload_round_trips() -> None:
    """The run-error element survives the same encode and decode a test payload does."""
    payload = _service().worker_down("gw2", "worker exited with status 1")
    decoded = state.RunErrorPayload.from_dict(json.loads(state.dumps(payload.as_dict())))
    assert decoded == payload
    assert state.RunErrorPayload.from_dict({**payload.as_dict(), "record_type": "result"}) is None


@pytest.mark.parametrize(
    ("limit", "lines", "expected"),
    [
        (2, ("l1", "l2", "l3"), ("l2", "l3")),
        (0, ("l1", "l2"), ()),
        (10_000_000, ("l1", "l2"), ("l1", "l2")),
    ],
)
def test_bounded_tail_keeps_the_newest_lines_within_both_bounds(
    limit: int, lines: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    """``logging.capture_lines`` bounds the tail in both directions, and the newest lines win."""
    assert state.bounded_tail(lines, limit) == expected


def test_bounded_tail_cuts_a_long_line_at_the_capture_cap() -> None:
    """One line is capped where the ring caps it, so a tail cannot balloon the wire payload."""
    line = "x" * (capture.MAX_TAIL_LINE_LENGTH * 2)
    tail = state.bounded_tail([line], 5)
    assert len(tail) == 1
    assert len(tail[0]) == capture.MAX_TAIL_LINE_LENGTH
    assert tail[0].endswith(capture.TRUNCATION_MARKER)


def test_the_payload_canonicalises_markers_and_publishes_the_property_names() -> None:
    """Every collection is sorted before it is written, so two workers serialise one shape."""
    payload = _payload(markers=("zeta", "alpha", "alpha"))
    assert payload.markers == ("alpha", "zeta")
    names = [name for name, _ in payload.as_properties()]
    assert names == sorted(names) == list(state.FOUNDRY_PROPERTY_NAMES)
    assert len(names) == 14
    assert state.PROPERTY_PREFIX == "foundry."
    assert "foundry.record_type" not in names
    assert state.OUTCOME_VALUES == (
        "passed",
        "failed",
        "skipped",
        "xfailed",
        "xpassed",
        "error",
        "quarantined_failed",
        "quarantine_passed",
        "not_run",
    )


def test_state_imports_without_a_heavy_dependency(
    repo_root: Path, foundry_python: str, subprocess_env: Callable[..., dict[str, str]]
) -> None:
    """``plugin.py`` imports ``state`` at module scope, so it may drag in nothing heavy (I5)."""
    completed = subprocess.run(
        [foundry_python, "-c", _IMPORT_PROBE],
        cwd=repo_root,
        env=subprocess_env({"PYTHONPATH": str(repo_root / "src")}),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"
