"""The quarantine registry: the marker vocabulary, the two modes, and the run's exit-code rule.

``execution.quarantine_mode`` (AAP 0.4.5) selects one of two behaviours for a test marked
``@pytest.mark.foundry_quarantine``:

``report`` -- the default. A marked test's failure is recorded as ``quarantined_failed``, listed in
the report's Quarantined section and mapped to a JUnit ``<skipped>`` element, and the run still
succeeds at exit code 0. The failure stays visible to a person while a known-flaky test cannot
break the build (AAP 0.4.7).

``strict`` -- the same record, but the failure counts against the run, so the run fails.

A marked test that passes is not a failure in either mode: it yields ``quarantine_passed`` with
exit code 0 and a note recommending that the marker be removed (assumption A11), because a marker
on a green test is stale state that would hide that test's next real failure.

The single rule
    :meth:`QuarantineRegistry.run_failed` is the whole exit-code rule: the run fails if and only if
    at least one decision carries ``fails_run``. Which outcome a record shows, whether the report
    lists it and whether JUnit writes a ``<skipped>`` or a ``<failure>`` all follow from the same
    decision, so the terminal, the JSON document, the JUnit XML and the HTML report cannot come to
    different conclusions about whether a run passed.

The marker namespace
    Foundry owns the ``foundry_`` marker prefix and registers exactly one marker,
    ``foundry_quarantine`` (AAP 0.4.2). :func:`is_quarantined` takes marker *names*, never a
    ``pytest.Item``: the plugin hook module is the only module allowed to read pytest's marker
    machinery, so this module receives the names that module derived. Any other marker, including a
    ``foundry_*`` marker a later release adds, leaves a test unquarantined.

Why the message constants are duplicated rather than shared
    :data:`QUARANTINED_SKIP_MESSAGE` and :data:`QUARANTINE_PASSED_NOTE` are fixed text (AAP 0.4.6)
    and the reporting package holds its own copy of both. This package may not import the reporting
    package (AAP 0.4.8), so the two copies cannot be collapsed into one shared constant without an
    upward import. They are kept byte-identical by tests instead, and changing either string here is
    a contract change under AAP 0.4.9 that must be mirrored in ``reporting/junit.py``'s copy.

Import-time and state discipline
    The module imports ``foundry.config.enums`` (standard library only), ``foundry.core.types`` and
    ``foundry.execution.state``, all permitted to this package by AAP 0.4.8. Importing it performs
    no I/O, reads no environment variable, configures no logger and mutates nothing: the registry is
    frozen, slotted value carrying its mode, every decision is a pure function of
    ``(markers, outcome, mode)``, and the messages and the removal note are constants. No clock and
    no randomness is involved, so two runs of the same suite reach the same decisions on every
    worker. This module raises nothing: an unrecognised mode falls back to ``report``, the
    conservative default, rather than failing a run over a defect in Foundry's own wiring.
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

The plugin hook module registers ``foundry_quarantine`` and nothing else, so an unregistered
``foundry_*`` marker is a hard error under the project's ``--strict-markers`` setting rather than a
silent no-op.
"""

QUARANTINE_MARKER: Final[str] = "foundry_quarantine"
"""The quarantine marker's exact spelling, as AAP 0.4.2 and the AAP's user example fix it.

A test carries it as ``@pytest.mark.foundry_quarantine``. The name is part of the published
surface (AAP 0.4.9): a later release may add markers beside it, never rename or remove this one.
"""

# --- The fixed message text (AAP 0.4.6) -----------------------------------

QUARANTINED_SKIP_MESSAGE: Final[str] = "foundry: quarantined failure"
"""The ``<skipped>`` message JUnit XML carries for a ``quarantined_failed`` record (AAP 0.4.6).

The text is what makes a CI system read a quarantined failure as a skip rather than a failure, so
it agrees with a run that exits 0 in ``report`` mode. It is duplicated in ``reporting/junit.py``
for the reason the module docstring states, and both copies are part of the compatibility promise.
"""

QUARANTINE_PASSED_NOTE: Final[str] = (
    "foundry: quarantined test passed; remove the foundry_quarantine marker"
)
"""The removal recommendation a ``quarantine_passed`` record carries (assumption A11).

It reaches the report's Quarantined section and the JUnit ``<system-out>`` note of the passing
testcase, and it is returned as the decision's ``note`` so a caller other than those two renderers
shows the same sentence. Duplicated in ``reporting/junit.py`` for the reason the module docstring
states.
"""


def is_quarantined(markers: Iterable[str]) -> bool:
    """Return whether ``markers`` names the quarantine marker.

    Args:
        markers: Marker *names* for one test, as the plugin hook module derives them from pytest's
            marker machinery. Order is irrelevant and duplicates are harmless. A ``foundry_*``
            marker other than :data:`QUARANTINE_MARKER` is ignored rather than treated as an error,
            and an entry that is not a name at all is skipped: this function reports a fact about a
            test's markers and never raises (AAP 0.1.2).

    Returns:
        ``True`` when :data:`QUARANTINE_MARKER` is among the names, ``False`` otherwise.
    """
    return any(isinstance(name, str) and name == QUARANTINE_MARKER for name in markers)


# --- The decision ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuarantineDecision:
    """What one test's quarantine marker does to that test's record and to the run.

    Frozen and slotted: the decision is taken once, stored in the item's stash and mirrored onto
    ``user_properties`` so it survives a worker boundary, then read by the exit-code composition
    and by every renderer. Nothing downstream may change it, and because one value produces the
    JSON record, the JUnit element and the HTML row, those three cannot disagree about a test.

    Every decision this module builds keeps the published invariants: ``listed`` is ``True`` only
    for the two quarantined outcomes, ``note`` is set only for ``quarantine_passed``, ``state`` is
    ``none`` whenever ``listed`` is ``False``, and ``fails_run`` is ``True`` for a marked failure in
    ``strict`` mode and for any failure carrying no marker.

    Attributes:
        state: The verdict the record carries (AAP 0.7.2): ``none`` when quarantine decided
            nothing, ``failed`` for a marked failure, ``passed`` for a marked test that passed.
        outcome: The record's outcome value (AAP 0.4.6). The incoming value is kept except where
            quarantine replaces a failure with ``quarantined_failed`` or a pass with
            ``quarantine_passed``.
        fails_run: Whether this record on its own makes the run fail.
            :meth:`QuarantineRegistry.run_failed` aggregates it, and it is the only thing that
            decides the exit code on quarantine's behalf.
        listed: Whether the record belongs in the report's Quarantined section and in the JUnit
            skip mapping, which is exactly the two quarantined outcomes.
        note: The removal recommendation carried by a passing marked test, ``None`` otherwise.
    """

    state: QuarantineState
    outcome: OutcomeValue
    fails_run: bool
    listed: bool
    note: str | None = None

    def as_payload(self) -> QuarantinePayload:
        """Return the verdict in the wire shape a record and ``user_properties`` carry.

        Returns:
            A :class:`foundry.execution.state.QuarantinePayload` holding :attr:`state`, which is the
            ``{"state": ...}`` object ``results.json`` publishes for every record (AAP 0.4.6).
        """
        return QuarantinePayload(state=self.state)

    def as_dict(self) -> dict[str, object]:
        """Return the whole decision as a JSON-ready mapping for diagnostics and terminal output.

        Distinct from :meth:`as_payload`, which is the narrow persisted form: this mapping also
        carries the outcome, the two flags and the note, which is what the terminal summary counts
        and what a test asserting a decision reads.

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
    pytest stash. It accumulates nothing -- the callers aggregate over the decisions it returns --
    so two items deciding on two workers share no state and a run's exit code never depends on the
    order its tests executed in (AAP 0.4.8).

    Passing text rather than a member is tolerated: ``QuarantineRegistry(mode="strict")`` resolves
    to :attr:`foundry.config.enums.QuarantineMode.STRICT`, so a value read from configuration or
    written in a test needs no cast. A value outside the enum falls back to
    :attr:`foundry.config.enums.QuarantineMode.REPORT`, the mode that fails the fewest runs, and
    is never raised over: a defect in Foundry's own wiring may not change a run's exit code
    (AAP 0.1.2).

    Attributes:
        mode: The active mode, always one of :class:`foundry.config.enums.QuarantineMode`'s members.
    """

    mode: QuarantineMode = QuarantineMode.REPORT

    def __post_init__(self) -> None:
        """Resolve :attr:`mode` to an enum member, falling back to ``report``."""
        object.__setattr__(self, "mode", _coerce_mode(self.mode))

    @property
    def is_strict(self) -> bool:
        """Whether a marked failure counts against the run.

        Returns:
            ``True`` in ``strict`` mode, ``False`` in ``report`` mode.
        """
        return self.mode is QuarantineMode.STRICT

    @property
    def label(self) -> str:
        """Return the registry's label for a terminal summary line.

        The session banner prints ``foundry: profile=<name>``, so the quarantine half of that line
        is ``quarantine=report`` or ``quarantine=strict``: deterministic, free of any path or node
        id, and identical on every worker.

        Returns:
            The active mode as ``quarantine=<mode>``.
        """
        return f"quarantine={self.mode}"

    def decide(self, *, quarantined: bool, outcome: OutcomeValue) -> QuarantineDecision:
        """Return the decision for one test, given whether it carries the marker and its outcome.

        The outcome and the flags follow one table (AAP 0.4.7):

        * No marker, outcome ``failed`` or ``error``: ``state=none``, outcome unchanged,
          ``fails_run=True``, not listed. A plain failure is pytest's business and this decision
          only agrees with it.
        * No marker, outcome ``passed``, ``skipped``, ``xfailed``, ``xpassed`` or ``not_run``:
          ``state=none``, outcome unchanged, ``fails_run=False``, not listed. ``not_run`` covers an
          item that never executed because the session stopped or a worker was lost (assumption
          A12); it is the absence of a result, not a failure.
        * Marker, outcome ``failed`` or ``error``: ``state=failed``, outcome
          ``quarantined_failed``, ``listed=True``, and ``fails_run`` is ``True`` in ``strict`` mode
          and ``False`` in ``report`` mode -- the one difference between the two modes.
        * Marker, outcome ``passed``: ``state=passed``, outcome ``quarantine_passed``,
          ``fails_run=False``, ``listed=True`` and :data:`QUARANTINE_PASSED_NOTE` as the note
          (assumption A11).
        * Marker, outcome ``skipped``, ``xfailed`` or ``xpassed``: not a quarantine event, so
          ``state=none``, the outcome unchanged and not listed. A skip is no evidence of flakiness,
          and ``xfail``/``xpass`` already carry semantics of their own that a second verdict would
          only blur.
        * An outcome that already names a quarantine verdict keeps the decision it records, whether
          or not the marker is present, which is what makes this method idempotent: deciding a
          record twice changes nothing on the second pass.
        * An outcome value outside the published nine is treated as a failure, because no value this
          release does not understand may hide a failure from the run.

        Args:
            quarantined: Whether the test carries :data:`QUARANTINE_MARKER`, as
                :func:`is_quarantined` reported it.
            outcome: The outcome value the run reached for the test, from
                :data:`foundry.execution.state.OutcomeValue`.

        Returns:
            The frozen decision for that test. The call depends on nothing but its arguments, so
            repeating it always returns an equal decision.
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
        """Return whether quarantine makes the run fail -- the single exit-code rule.

        Each decision already carries its mode's verdict, so this method consults no other state:
        the run fails if and only if at least one decision has ``fails_run``. That is what leaves a
        ``quarantined_failed`` in ``report`` mode at exit code 0 while the same record in ``strict``
        mode fails the run (AAP 0.4.7), and why no renderer needs its own rule for the exit code.

        Args:
            decisions: The decisions taken for the session's tests, in any order. The sequence is
                consumed once, so a generator is accepted.

        Returns:
            ``True`` when at least one decision fails the run, ``False`` when none does.
        """
        return any(decision.fails_run for decision in decisions)

    def counts(self, decisions: Iterable[QuarantineDecision]) -> Mapping[str, int]:
        """Return the quarantine tallies a terminal summary or a diagnostic reports.

        Every decision's ``state`` is counted, and the third tally narrows the marked failures to
        the ones the run counts as failures: every marked failure in ``strict`` mode, none in
        ``report`` mode. That is the quarantine part of the exit-code decision, so an operator reads
        the tallies and the outcome together without recomputing either.

        Args:
            decisions: The decisions taken for the session's tests, in any order.

        Returns:
            A mapping with the keys ``failed``, ``failing_in_strict`` and ``passed`` in that sorted
            order, so two runs render identical text.
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
    retyping them, and so a membership test over them has one definition site.

    Returns:
        ``("quarantined_failed", "quarantine_passed")``, in the order AAP 0.4.6 lists them.
    """
    return ("quarantined_failed", "quarantine_passed")


# --- Private helpers ------------------------------------------------------


def _coerce_mode(mode: object) -> QuarantineMode:
    """Return ``mode`` as an enum member, falling back to ``report`` when it is not one.

    The text is matched the way the configuration layer matches an ``execution.quarantine_mode``
    value -- case-insensitively, through :func:`foundry.config.enums.parse_enum` -- so a mode read
    from an environment variable, spelled in a project's own tooling or written in a test resolves
    to the same member without a cast.

    Args:
        mode: A member, the text of a member, or anything else that reached the constructor.

    Returns:
        The matching member, or :attr:`foundry.config.enums.QuarantineMode.REPORT` -- the
        conservative default that fails the fewest runs -- when no member matches.
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
    :meth:`QuarantineRegistry.decide` idempotent: a record decoded from the wire form and passed
    through the registry again keeps its outcome, its listing and its note.

    Args:
        outcome: Either ``quarantined_failed`` or ``quarantine_passed``.
        strict: Whether the active mode is ``strict``, which decides whether the marked failure
            fails the run.

    Returns:
        The decision the outcome records.
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
