"""Unit tests for ``foundry.execution.quarantine``: the marker, both modes and the exit-code rule.

One case per row of the table AAP 0.4.7 publishes and per name AAP 0.4.2, 0.4.6 and 0.4.9 fix: the
verdict a marked test gets in ``report`` and in ``strict`` mode, the ``quarantined_failed`` and
``quarantine_passed`` outcomes with A11's note asking for the marker's removal, the tallies that say
how many marked failures the run itself counts, and the marker vocabulary the report republishes.

Nothing here drives pytest machinery, sleeps, reads the wall clock or opens a socket (AAP 0.14.3):
every verdict is decided from a registry and an outcome the run already recorded. The coordinator's
per-test capture has its own module, ``tests.unit.test_execution_capture``, and the retry budget,
sharding, timeout and wire payloads stay in ``tests.unit.test_execution``.
"""

from __future__ import annotations

import pytest

from foundry.config.enums import QuarantineMode
from foundry.execution import quarantine, state


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
