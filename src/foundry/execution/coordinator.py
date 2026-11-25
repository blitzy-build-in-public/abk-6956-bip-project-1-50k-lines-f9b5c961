"""The in-framework retry driver: how often an item runs, and what each attempt left behind.

``plugin.py`` is the only module that implements a pytest hookspec, so the ``firstresult``
``pytest_runtest_protocol`` delegation lives there and this module is the plain Python it calls.
:func:`run_attempts` drives pytest's own ``_pytest.runner.runtestprotocol`` once per attempt, so
every attempt runs setup, call and teardown. ``execution.retries`` counts the *repeats* after the
first attempt (AAP 0.4.5, assumption A2), so ``--foundry-retries 2`` allows three attempts.

Exactly one set of reports reaches the log: each attempt runs with ``log=False`` and only the final
attempt's reports are re-emitted, while ``call_and_report`` still fires the ``makereport`` hook
first, so every attempt is stamped with Foundry's ``user_properties``. No delay is inserted between
attempts, because backoff belongs to :mod:`foundry.http.retries`, so this module never sleeps,
starts a thread or opens a socket (AAP 0.14.3). An interrupted run stays interrupted, because
``KeyboardInterrupt`` and pytest's ``Exit`` are never caught here (AAP 0.4.7). The bookkeeping
around each attempt is isolated one call site at a time, so a plumbing fault logs one ``WARNING``
and leaves the outcome and the exit code alone (AAP 0.1.2).

Elapsed time comes from :func:`time.monotonic` alone, and the clock enters through
:func:`foundry.core.ids.utc_now` and nowhere else. Importing this module defines names only
(AAP 0.4.8).
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

from foundry.core.ids import utc_now
from foundry.core.types import PHASES, AttemptRecord, Phase
from foundry.execution import quarantine, retry, timeout
from foundry.execution.session import SessionState, session_state
from foundry.execution.state import (
    ITEM_ATTEMPTS,
    ITEM_QUARANTINE,
    ITEM_STARTED_AT,
    ITEM_STARTED_MONOTONIC,
    AttemptPayload,
    OutcomeValue,
)
from foundry.obs import events
from foundry.obs.redaction import MAX_EXCERPT_BYTES, Redactor

if TYPE_CHECKING:
    import pytest

__all__: Final[tuple[str, ...]] = (
    "AttemptLedger",
    "EMPTY_LEDGER",
    "classify_reports",
    "final_outcome",
    "first_failure_message",
    "ledger_of",
    "reset_item_state",
    "run_attempts",
)

_COMPONENT: Final[str] = "execution.coordinator"
"""The component name every plumbing warning from this module carries, matching its dotted path."""

_MS_PER_SECOND: Final[float] = 1000.0
"""Converts :func:`time.monotonic` deltas, which are seconds, to the record's milliseconds."""

_FAILED: Final[str] = "failed"
"""pytest's own outcome token for a failing phase, as ``TestReport.outcome`` spells it."""

_PASSED: Final[str] = "passed"
"""pytest's own outcome token for a passing phase."""

_NOT_RUN: Final[OutcomeValue] = "not_run"
"""The outcome of an attempt that produced no report at all (AAP 0.4.6, assumption A12)."""

_FAILURE_OUTCOMES: Final[tuple[OutcomeValue, ...]] = ("failed", "error")
"""The two outcomes a label is reported for: a call failure and a fixture failure."""

_CALL_PHASE: Final[Phase] = "call"
"""The phase an attempt with no failing report is attributed to, for the retry decision alone."""

_LABEL_TIMEOUT: Final[str] = "timeout"
"""``AttemptRecord.retry_on`` for a test-level timeout, spelled as the retry predicate is.

Used only when :func:`foundry.execution.retry.retry_label` cannot name the ending, because a
pytest-timeout failure is pytest's own ``Failed`` and carries no fault the predicates classify.
"""

_TIMEOUT_ERROR_NAME: Final[str] = "ReadTimeout"
"""An ``httpx`` timeout exception, used to obtain the predicate spelling from ``retry_label``.

No exception is recoverable from a rendered timeout, so a representative fault is classified.
"""

_ERROR_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b([A-Z][A-Za-z0-9_]{2,}(?:Error|Exception|Timeout|Exit|Warning))\b"
)
"""An exception class name in a rendered failure: ``AssertionError``, ``ConnectError``, ``Failed``.

A report carries rendered text rather than the exception, and the capitalised exception-shaped token
is what keeps prose and paths out of the match.
"""

_MISSING_STATE_MESSAGE: Final[str] = (
    "the retry driver found no session state, so the item ran once as pytest would run it"
)
"""Logged when the run's state was never bootstrapped; the item still runs (AAP 0.1.2)."""

_PROTOCOL_MESSAGE: Final[str] = (
    "the runtest protocol faulted, so the item ran once as pytest would run it"
)
"""Logged when a fault escaped the protocol; the item is then retried by nobody, and still runs."""

_CLOCK_MESSAGE: Final[str] = (
    "the retry driver could not seed the item's clock, so its timings may be missing"
)
"""Logged when the per-item stash refused the clock and ledger reset (AAP 0.1.2)."""

_LEDGER_MESSAGE: Final[str] = (
    "the retry driver could not record an attempt, so the item's ledger is incomplete"
)
"""Logged when the attempt record could not be stored or logged; the attempt still counts as run."""

_QUARANTINE_MESSAGE: Final[str] = (
    "the retry driver could not take the quarantine decision, so the default verdict applies"
)
"""Logged when the quarantine verdict could not be decided or stored (AAP 0.1.2)."""

_KeyT = TypeVar("_KeyT")


@dataclass(frozen=True, slots=True)
class AttemptLedger:
    """One item's attempts, oldest first: the only place attempt history lives (AAP 0.4.8).

    Frozen because the report hook, the ``user_properties`` mirror and the summed ``duration_ms``
    all read one value that none of them may alter, and :meth:`add` returns a new ledger. The stash
    holds the plain ``list`` of records that crosses the worker boundary.
    """

    records: tuple[AttemptRecord, ...] = ()

    @classmethod
    def of(cls, records: Iterable[AttemptRecord]) -> AttemptLedger:
        """Return a ledger holding ``records``, oldest first, as a ``list`` or any iterable."""
        return cls(tuple(records))

    def add(self, record: AttemptRecord) -> AttemptLedger:
        """Return a new ledger with ``record`` appended, leaving this one untouched."""
        return AttemptLedger((*self.records, record))

    @property
    def attempts(self) -> int:
        """Return how many attempts the ledger holds, which is ``results.json``'s ``attempts``."""
        return len(self.records)

    @property
    def last(self) -> AttemptRecord | None:
        """Return the most recent attempt, or ``None`` when the ledger is empty."""
        return self.records[-1] if self.records else None

    def outcomes(self) -> tuple[str, ...]:
        """Return each attempt's outcome oldest first, never sorted: criterion 6's ending order."""
        return tuple(record.outcome for record in self.records)

    def total_elapsed_ms(self) -> float:
        """Return the summed elapsed milliseconds of every attempt, the record's ``duration_ms``."""
        return float(sum(record.elapsed_ms for record in self.records))

    def as_payloads(self) -> tuple[AttemptPayload, ...]:
        """Return the attempts in the wire shape :mod:`foundry.execution.state` owns."""
        return tuple(AttemptPayload.from_record(record) for record in self.records)

    def as_dict(self) -> dict[str, object]:
        """Return the ledger, its count and its total as a JSON-ready mapping for diagnostics."""
        return {
            "attempts": self.attempts,
            "outcomes": list(self.outcomes()),
            "total_elapsed_ms": self.total_elapsed_ms(),
            "records": [record.as_dict() for record in self.records],
        }


EMPTY_LEDGER: Final[AttemptLedger] = AttemptLedger()
"""The ledger of an item that has not been attempted yet: immutable, so it is safe as a default."""


def ledger_of(item: pytest.Item) -> AttemptLedger:
    """Return the attempts recorded for ``item`` so far, oldest first, or :data:`EMPTY_LEDGER`.

    ``item.stash[ITEM_ATTEMPTS]`` is the single home of attempt history (AAP 0.4.8), and it holds
    only the attempts that have already *finished*, so the report hook in ``plugin.py`` derives the
    current attempt's number as ``ledger.attempts + 1``.
    """
    records = item.stash.get(ITEM_ATTEMPTS, None)
    return EMPTY_LEDGER if records is None else AttemptLedger.of(records)


def reset_item_state(item: pytest.Item) -> None:
    """Clear one item's per-attempt stash entries before its first attempt.

    Defensive rather than routine: a reused ``Item`` would otherwise inherit another test's attempt
    history, quarantine verdict and start time. Only the four keys this module owns are cleared.
    """
    _forget(item, ITEM_ATTEMPTS)
    _forget(item, ITEM_QUARANTINE)
    _forget(item, ITEM_STARTED_AT)
    _forget(item, ITEM_STARTED_MONOTONIC)


def _forget(item: pytest.Item, key: pytest.StashKey[_KeyT]) -> None:
    """Remove ``key`` from the item's stash when it is present, and do nothing when it is not."""
    if key in item.stash:
        del item.stash[key]


def classify_reports(reports: Sequence[pytest.TestReport]) -> tuple[OutcomeValue, str | None]:
    """Return how one attempt ended and the label describing its ending.

    The single classification of an attempt, so a record's ``outcome`` and the ledger's cannot
    disagree. The label is ``None`` for an attempt that did not fail.
    """
    outcome = final_outcome(reports)
    if outcome not in _FAILURE_OUTCOMES:
        return outcome, None
    return outcome, _failure_label(reports)


def final_outcome(reports: Sequence[pytest.TestReport]) -> OutcomeValue:
    """Return the outcome one attempt's reports describe, before any quarantine verdict.

    A setup failure is an ``error``, and so is a teardown failure after a passing call (AAP 0.4.6);
    a failing call is ``failed``; ``wasxfail`` on the call report separates an xfail or xpass from a
    skip or pass; an attempt with no report is ``not_run``. The precedence is the one
    :func:`foundry.execution.session.assemble_payload` applies.
    """
    if not reports:
        return _NOT_RUN
    by_phase = {report.when: report for report in reports}
    setup = by_phase.get("setup")
    if setup is not None and setup.outcome == _FAILED:
        return "error"
    call = by_phase.get("call")
    if call is None:
        teardown = by_phase.get("teardown")
        if teardown is not None and teardown.outcome == _FAILED:
            return "error"
        return "skipped"
    wasxfail: object = getattr(call, "wasxfail", None)
    if wasxfail is not None:
        return "xpassed" if call.outcome == _PASSED else "xfailed"
    if call.outcome == _FAILED:
        return "failed"
    if call.outcome != _PASSED:
        return "skipped"
    teardown = by_phase.get("teardown")
    if teardown is not None and teardown.outcome == _FAILED:
        return "error"
    return "passed"


def first_failure_message(reports: Sequence[pytest.TestReport], redactor: Redactor) -> str | None:
    """Return the first failing phase's message, redacted and bounded, or ``None`` for a pass.

    The excerpt that rides ``AttemptRecord.message``: the first non-empty line of the failing
    report's rendered representation, redacted before it is cut to the 200-byte budget AAP 0.4.7
    uses, so a token echoed into a traceback cannot leave the process.
    """
    report = _first_failure(reports)
    if report is None:
        return None
    line = _first_line(_failure_text(report))
    if not line:
        return None
    return redactor.text(line, limit=MAX_EXCERPT_BYTES)


def run_attempts(item: pytest.Item, nextitem: pytest.Item | None) -> bool:
    """Run ``item`` up to ``execution.retries`` times; the ``firstresult`` hook's whole body.

    What ``plugin.py``'s ``pytest_runtest_protocol`` delegation calls. It runs pytest's own protocol
    once per attempt, records every attempt in the item's ledger, takes the quarantine verdict from
    the final attempt's outcome and emits exactly one set of reports, the final one.

    ``KeyboardInterrupt`` and pytest's ``Exit`` propagate untouched (AAP 0.4.7), and a fault in
    Foundry's own plumbing is one ``WARNING`` that changes neither the outcome nor the exit code
    (AAP 0.1.2). Returning ``True`` stops the ``firstresult`` chain.
    """
    state = _read_state(item)
    if state is None:
        return _plain_pass(item, nextitem)
    _prepare_item(item)
    ihook = item.ihook
    ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    reports = _attempt_loop(item, nextitem, state)
    if not reports:
        # No attempt survived Foundry's own plumbing, so the item runs once more, unretried: the
        # outcome the test earns is then the outcome the run reports, unchanged (AAP 0.1.2).
        reports = _plain_run(item, nextitem)
    _stamp_quarantine(item, state, reports)
    for report in reports:
        ihook.pytest_runtest_logreport(report=report)
    ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True


# --- The attempt loop -----------------------------------------------------


def _attempt_loop(
    item: pytest.Item, nextitem: pytest.Item | None, state: SessionState
) -> list[pytest.TestReport]:
    """Repeat the runtest protocol until the item passes, cannot be retried, or spends its budget.

    A fault raised out of the protocol is reported once and ends the loop with no reports, which is
    the caller's cue to run the item once, unretried (AAP 0.1.2). pytest's control flow is not a
    fault and is re-raised: ``Exit`` must reach pytest (AAP 0.4.7), and ``KeyboardInterrupt`` is a
    ``BaseException`` no handler here catches.
    """
    # Imported here rather than at module scope: the runner is what the hook delegation enters
    # anyway, and AAP 0.4.8 keeps pytest off this module's runtime import surface.
    from _pytest.outcomes import Exit
    from _pytest.runner import runtestprotocol

    policy = state.retries
    reports: list[pytest.TestReport] = []
    for number in range(1, policy.max_attempts + 1):
        started = time.monotonic()
        try:
            reports = runtestprotocol(item, nextitem=nextitem, log=False)
        except Exception as error:
            if isinstance(error, Exit):
                raise
            # Failure-isolation rule: Foundry's plumbing may not change a test's outcome.
            events.emit_plumbing_failure(_COMPONENT, _PROTOCOL_MESSAGE, exc=error)
            return []
        elapsed_ms = max(0.0, (time.monotonic() - started) * _MS_PER_SECOND)
        outcome, label = classify_reports(reports)
        _record_attempt(
            item,
            AttemptRecord(
                number=number,
                outcome=outcome,
                elapsed_ms=elapsed_ms,
                retry_on=label,
                message=first_failure_message(reports, state.redactor),
            ),
        )
        if not retry.should_retry(
            phase=_failing_phase(reports),
            outcome=outcome,
            attempts_used=number,
            policy=policy,
        ):
            break
        # The attempt is discarded, so the peer's timer for it is disarmed before the next attempt
        # arms its own; a cancel for an attempt that was never armed is a no-op in the peer.
        timeout.cancel_timer(item)
    return reports


def _read_state(item: pytest.Item) -> SessionState | None:
    """Return the run's state, or ``None`` once a bootstrap fault has been reported.

    State that was never bootstrapped is Foundry's fault and not the test's, so it is reported like
    every other plumbing fault (AAP 0.1.2) and the caller runs the item instead of failing it.
    """
    try:
        return session_state(item.config)
    except Exception as error:
        # Failure-isolation rule: Foundry's own plumbing may not change a test's outcome.
        events.emit_plumbing_failure(_COMPONENT, _MISSING_STATE_MESSAGE, exc=error)
        return None


def _prepare_item(item: pytest.Item) -> None:
    """Clear the previous attempt state and seed this item's clock, isolating any fault."""
    try:
        reset_item_state(item)
        item.stash.setdefault(ITEM_STARTED_MONOTONIC, time.monotonic())
        item.stash.setdefault(ITEM_STARTED_AT, utc_now())
    except Exception as error:
        # Failure-isolation rule: a stash fault costs a timestamp, never the test's outcome.
        events.emit_plumbing_failure(_COMPONENT, _CLOCK_MESSAGE, exc=error)


def _record_attempt(item: pytest.Item, record: AttemptRecord) -> None:
    """Append ``record`` to the item's ledger and log it, isolating either step."""
    try:
        item.stash[ITEM_ATTEMPTS] = list(ledger_of(item).add(record).records)
    except Exception as error:
        # Failure-isolation rule: an incomplete ledger may not change an attempt's outcome.
        events.emit_plumbing_failure(_COMPONENT, _LEDGER_MESSAGE, exc=error)
        return
    events.emit_attempt(nodeid=item.nodeid, attempt=record.number, outcome=record.outcome)


def _stamp_quarantine(
    item: pytest.Item, state: SessionState, reports: Sequence[pytest.TestReport]
) -> None:
    """Store the quarantine verdict the final attempt's outcome earns, isolating any fault."""
    try:
        decision = state.quarantine.decide(
            quarantined=quarantine.is_quarantined(_marker_names(item)),
            outcome=final_outcome(reports),
        )
        item.stash[ITEM_QUARANTINE] = decision
    except Exception as error:
        # Failure-isolation rule: without a verdict a record keeps its unquarantined outcome.
        events.emit_plumbing_failure(_COMPONENT, _QUARANTINE_MESSAGE, exc=error)


def _plain_pass(item: pytest.Item, nextitem: pytest.Item | None) -> bool:
    """Run the item once, as pytest's own protocol would, and return ``True``.

    The fallback for a run whose state could not be read, so the outcome reaches the terminal and
    the exit code as it would with Foundry out of the chain.
    """
    ihook = item.ihook
    ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    for report in _plain_run(item, nextitem):
        ihook.pytest_runtest_logreport(report=report)
    ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True


def _plain_run(item: pytest.Item, nextitem: pytest.Item | None) -> list[pytest.TestReport]:
    """Run the item's protocol once, as pytest's default ``pytest_runtest_protocol`` would.

    ``log=False`` because the caller emits the reports itself.
    """
    from _pytest.runner import runtestprotocol

    return runtestprotocol(item, nextitem=nextitem, log=False)


# --- Reading one attempt's reports ----------------------------------------


def _marker_names(item: pytest.Item) -> tuple[str, ...]:
    """Return the marker names on ``item``: registration belongs to ``plugin.py`` (AAP 0.4.8)."""
    return tuple(marker.name for marker in item.iter_markers())


def _first_failure(reports: Sequence[pytest.TestReport]) -> pytest.TestReport | None:
    """Return the earliest failing report in setup-call-teardown order, or ``None``."""
    by_phase = {report.when: report for report in reports}
    for phase in PHASES:
        report = by_phase.get(phase)
        if report is not None and report.outcome == _FAILED:
            return report
    return None


def _failing_phase(reports: Sequence[pytest.TestReport]) -> Phase:
    """Return the phase an attempt failed in, and ``call`` when no report names one.

    Only the position matters to the retry decision.
    """
    report = _first_failure(reports)
    if report is None:
        return _CALL_PHASE
    for phase in PHASES:
        if phase == report.when:
            return phase
    return _CALL_PHASE


def _failure_text(report: pytest.TestReport) -> str:
    """Return a report's rendered failure, preferring the crash line over the whole traceback.

    ``reprcrash.message`` is the one line pytest prints after the traceback. A ``longrepr`` that
    cannot be rendered is read as no message rather than raising (AAP 0.1.2).
    """
    longrepr: object = report.longrepr
    crash: object = getattr(longrepr, "reprcrash", None)
    message: object = getattr(crash, "message", None)
    if isinstance(message, str) and message.strip():
        return message
    if longrepr is None:
        return ""
    try:
        return str(longrepr)
    except Exception:
        # Failure-isolation rule: an unrenderable traceback yields no message, never an exception.
        return ""


def _first_line(text: str) -> str:
    """Return the first non-empty line of ``text`` with its whitespace collapsed, or ``""``."""
    for line in text.splitlines():
        collapsed = " ".join(line.split())
        if collapsed:
            return collapsed
    return ""


def _failure_label(reports: Sequence[pytest.TestReport]) -> str:
    """Return the diagnostic label describing how an attempt ended."""
    report = _first_failure(reports)
    return _label_for("" if report is None else _failure_text(report))


def _label_for(text: str) -> str:
    """Return the label a rendered failure names, from the vocabulary the retry engine shares.

    The label is diagnostic only, so reading it differently from the HTTP engine changes a word,
    never a verdict.
    """
    if timeout.TIMEOUT_MARKER in text:
        return retry.retry_label(_recovered_error(_TIMEOUT_ERROR_NAME)) or _LABEL_TIMEOUT
    for match in _ERROR_NAME_PATTERN.finditer(text):
        recovered = _recovered_error(match.group(1))
        if recovered is not None:
            return retry.retry_label(recovered) or retry.LABEL_ERROR
    return retry.LABEL_ERROR


def _recovered_error(name: str) -> BaseException | None:
    """Return a representative exception for a class name a failure names, or ``None``.

    ``retry.retry_label`` classifies an exception object while a report carries only rendered text,
    and ``httpx`` is imported here because only a failing attempt asks for a label.
    """
    if name == "AssertionError":
        return AssertionError()
    try:
        import httpx
    except ImportError:
        # httpx is a declared dependency; without it no predicate name can be recovered at all.
        return None
    candidate: object = getattr(httpx, name, None)
    if isinstance(candidate, type) and issubclass(candidate, Exception):
        error_type: type[Exception] = candidate
        return error_type(name)
    return None
