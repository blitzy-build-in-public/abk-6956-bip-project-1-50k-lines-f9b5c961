"""The quarantine registry: the marker vocabulary, the two modes and the run's exit-code rule.

``execution.quarantine_mode`` (AAP 0.4.5) selects what a marked test's failure does: ``report``, the
default, records it as ``quarantined_failed``, lists it and still exits 0, while ``strict`` counts
the same record against the run; a marked pass is ``quarantine_passed`` with exit 0 and a removal
note in either mode (AAP 0.4.7, assumption A11). :meth:`QuarantineRegistry.run_failed` is the whole
exit-code rule, so the terminal, the JSON document, the JUnit XML and the HTML report cannot
disagree about whether a run passed. The two fixed messages (AAP 0.4.6) are duplicated in
``reporting/junit.py``, which this package may not import (AAP 0.4.8); tests keep them identical.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from foundry.config.enums import QuarantineMode, parse_enum
from foundry.core.types import QuarantineState
from foundry.execution.state import OUTCOME_VALUES, OutcomeValue, QuarantinePayload

__all__ = [
    "MARKER_NAMESPACE_PREFIX",
    "QUARANTINED_SKIP_MESSAGE",
    "QUARANTINE_MARKER",
    "QUARANTINE_PASSED_NOTE",
    "QuarantineDecision",
    "QuarantineRegistry",
    "is_quarantined",
    "quarantined_outcomes",
]

# --- The marker vocabulary (AAP 0.4.2) ------------------------------------

MARKER_NAMESPACE_PREFIX: Final[str] = "foundry_"
"""The marker-name prefix Foundry owns, and the only prefix it may register names under.

An unregistered ``foundry_*`` marker is a hard error under ``--strict-markers`` rather than a no-op.
"""

QUARANTINE_MARKER: Final[str] = "foundry_quarantine"
"""The marker's exact spelling (AAP 0.4.2), carried as ``@pytest.mark.foundry_quarantine``.

The name is part of the published surface (AAP 0.4.9): never renamed or removed.
"""

# --- The fixed message text (AAP 0.4.6) -----------------------------------

QUARANTINED_SKIP_MESSAGE: Final[str] = "foundry: quarantined failure"
"""The ``<skipped>`` message JUnit XML carries for a ``quarantined_failed`` record (AAP 0.4.6).

The text makes a CI system read a quarantined failure as a skip, agreeing with a run that exits 0 in
``report`` mode; ``reporting/junit.py`` holds a byte-identical copy that tests keep in step.
"""

QUARANTINE_PASSED_NOTE: Final[str] = (
    "foundry: quarantined test passed; remove the foundry_quarantine marker"
)
"""The removal recommendation a ``quarantine_passed`` record carries (assumption A11).

It is the decision's ``note``, and it reaches the report's Quarantined section and the JUnit
``<system-out>`` note.
"""


def is_quarantined(markers: Iterable[str]) -> bool:
    """Return whether ``markers`` names the quarantine marker.

    Args:
        markers: Marker *names* for one test, as the plugin hook module derives them from pytest's
            marker machinery. Order is irrelevant, another ``foundry_*`` marker is ignored and an
            entry that is not a name is skipped: this reports a fact and never raises (AAP 0.1.2).

    Returns: ``True`` when :data:`QUARANTINE_MARKER` is among the names.
    """
    return any(isinstance(name, str) and name == QUARANTINE_MARKER for name in markers)


# --- The decision ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuarantineDecision:
    """What one test's quarantine marker does to that test's record and to the run.

    Frozen and slotted: the decision is taken once, stored in the item's stash and mirrored onto
    ``user_properties`` so it survives a worker boundary, then read by the exit-code composition and
    by every renderer, which is why those cannot disagree about a test. Every decision this module
    builds keeps the published invariants: ``listed`` only for the two quarantined outcomes,
    ``note`` only for ``quarantine_passed``, ``state`` ``none`` whenever ``listed`` is ``False``,
    and ``fails_run`` for a marked failure in ``strict`` mode and for any failure with no marker.
    """

    state: QuarantineState
    outcome: OutcomeValue
    fails_run: bool
    listed: bool
    note: str | None = None

    def as_payload(self) -> QuarantinePayload:
        """Return the verdict in the wire shape a record and ``user_properties`` carry.

        Returns:
            A :class:`foundry.execution.state.QuarantinePayload` holding :attr:`state`.
        """
        return QuarantinePayload(state=self.state)

    def as_dict(self) -> dict[str, object]:
        """Return the whole decision as a JSON-ready mapping for diagnostics and terminal output.

        Distinct from :meth:`as_payload`, which is the narrow persisted form: this mapping also
        carries the outcome, the two flags and the note.

        Returns:
            The five fields in declaration order, every value a JSON scalar.
        """
        return {
            "state": self.state,
            "outcome": self.outcome,
            "fails_run": self.fails_run,
            "listed": self.listed,
            "note": self.note,
        }


# --- The registry ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuarantineRegistry:
    """The session's quarantine mode, and the decisions taken under it.

    Constructed once per session from the resolved ``execution.quarantine_mode`` value and stored in
    pytest stash. It accumulates nothing -- callers aggregate over the decisions it returns -- so
    two items on two workers share no state and a run's exit code never depends on execution order
    (AAP 0.4.8). Passing text rather than a member is tolerated, so ``mode="strict"`` resolves to
    :attr:`foundry.config.enums.QuarantineMode.STRICT`, and a value outside the enum falls back to
    :attr:`foundry.config.enums.QuarantineMode.REPORT`, the mode that fails the fewest runs: a
    defect in Foundry's own wiring may not change a run's exit code (AAP 0.1.2).
    (AAP 0.1.2).
    """

    mode: QuarantineMode = QuarantineMode.REPORT

    def __post_init__(self) -> None:
        """Resolve :attr:`mode` to an enum member, falling back to ``report``."""
        object.__setattr__(self, "mode", _coerce_mode(self.mode))

    @property
    def is_strict(self) -> bool:
        """Whether a marked failure counts against the run."""
        return self.mode is QuarantineMode.STRICT

    @property
    def label(self) -> str:
        """Return the registry's label for a terminal summary line.

        The session banner prints ``foundry: profile=<name>``, so the quarantine half of that line
        is ``quarantine=report`` or ``quarantine=strict``: deterministic and identical on every
        worker.

        Returns:
            The active mode as ``quarantine=<mode>``.
        """
        return f"quarantine={self.mode}"

    def decide(self, *, quarantined: bool, outcome: OutcomeValue) -> QuarantineDecision:
        """Return the decision for one test, given whether it carries the marker and its outcome.

        The outcome and the flags follow one table (AAP 0.4.7). A failure carrying no marker keeps
        its outcome and fails the run; a marked failure becomes ``quarantined_failed`` and fails the
        run only in ``strict`` mode; a marked pass becomes ``quarantine_passed`` with
        :data:`QUARANTINE_PASSED_NOTE` and fails nothing (assumption A11); a pass, skip, xfail,
        xpass or ``not_run`` is listed nowhere, ``not_run`` being the absence of a result rather
        than a failure (assumption A12). An outcome that already names a quarantine verdict keeps
        the decision it records, making this idempotent; a value outside the published nine counts
        as a failure.
        """
        if _is_quarantine_outcome(outcome):
            return _already_decided(outcome, strict=self.is_strict)
        if _is_failure_outcome(outcome):
            if not quarantined:
                return QuarantineDecision(
                    state="none", outcome=outcome, fails_run=True, listed=False
                )
            return QuarantineDecision(
                state="failed", outcome="quarantined_failed", fails_run=self.is_strict, listed=True
            )
        if outcome == "passed":
            if not quarantined:
                return QuarantineDecision(
                    state="none", outcome=outcome, fails_run=False, listed=False
                )
            return QuarantineDecision(
                state="passed",
                outcome="quarantine_passed",
                fails_run=False,
                listed=True,
                note=QUARANTINE_PASSED_NOTE,
            )
        if outcome in OUTCOME_VALUES:
            return QuarantineDecision(state="none", outcome=outcome, fails_run=False, listed=False)
        return QuarantineDecision(state="none", outcome=outcome, fails_run=True, listed=False)

    def run_failed(self, decisions: Iterable[QuarantineDecision]) -> bool:
        """Report whether quarantine makes the run fail -- the single exit-code rule.

        The run fails if and only if one decision carries ``fails_run`` (AAP 0.4.7), and
        ``decisions`` is consumed once, so a generator is accepted.
        """
        return any(decision.fails_run for decision in decisions)

    def counts(self, decisions: Iterable[QuarantineDecision]) -> Mapping[str, int]:
        """Return the ``failed``, ``failing_in_strict`` and ``passed`` tallies, in that order.

        Every decision's ``state`` is counted, and the second tally narrows the marked failures to
        the ones the run counts: every marked failure in ``strict`` mode, none in ``report`` mode.
        """
        failed = 0
        passed = 0
        failing_in_strict = 0
        for decision in decisions:
            if decision.state == "failed":
                failed += 1
                if decision.fails_run:
                    failing_in_strict += 1
            elif decision.state == "passed":
                passed += 1
        return {"failed": failed, "failing_in_strict": failing_in_strict, "passed": passed}


def quarantined_outcomes() -> tuple[OutcomeValue, ...]:
    """Return the two outcome values a quarantine decision can produce.

    Exposed so the exit-code composition and the tests can name the quarantine outcomes without
    retyping them.

    Returns:
        ``("quarantined_failed", "quarantine_passed")``, in the order AAP 0.4.6 lists them.
    """
    return ("quarantined_failed", "quarantine_passed")


# --- Private helpers ------------------------------------------------------


def _coerce_mode(mode: object) -> QuarantineMode:
    """Return ``mode`` as an enum member, falling back to ``report`` when it is not one.

    The text is matched the way the configuration layer matches an ``execution.quarantine_mode``
    value -- case-insensitively, through :func:`foundry.config.enums.parse_enum` -- and ``report``
    is the fallback because it is the conservative default that fails the fewest runs.
    """
    if isinstance(mode, QuarantineMode):
        return mode
    parsed = parse_enum(QuarantineMode, mode if isinstance(mode, str) else str(mode))
    return QuarantineMode.REPORT if parsed is None else parsed


def _is_failure_outcome(outcome: OutcomeValue) -> bool:
    """Return whether ``outcome`` reports a failure.

    ``failed`` is one, and so is ``error`` -- a failure outside the call phase, in setup or in
    teardown (AAP 0.4.6).
    """
    return outcome in ("failed", "error")


def _is_quarantine_outcome(outcome: OutcomeValue) -> bool:
    """Return whether ``outcome`` already names a quarantine verdict."""
    return outcome in quarantined_outcomes()


def _already_decided(outcome: OutcomeValue, *, strict: bool) -> QuarantineDecision:
    """Return the decision an outcome that already names a quarantine verdict keeps.

    Rebuilding the decision from the outcome alone is what makes
    :meth:`QuarantineRegistry.decide` idempotent. ``strict`` decides whether the marked failure
    fails the run.
    """
    if outcome == "quarantine_passed":
        return QuarantineDecision(
            state="passed",
            outcome="quarantine_passed",
            fails_run=False,
            listed=True,
            note=QUARANTINE_PASSED_NOTE,
        )
    return QuarantineDecision(
        state="failed", outcome="quarantined_failed", fails_run=strict, listed=True
    )
