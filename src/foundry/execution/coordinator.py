"""The in-framework retry driver: how often an item runs, and what each attempt left behind.

``plugin.py`` holds the only pytest hookspec implementation, so the ``firstresult``
``pytest_runtest_protocol`` delegation lives there and this is the plain Python it calls:
:func:`run_attempts` drives ``_pytest.runner.runtestprotocol`` once per attempt, so every attempt
runs setup, call and teardown, and ``execution.retries`` counts the *repeats* after the first
attempt (AAP 0.4.5, assumption A2): ``--foundry-retries 2`` allows three attempts. Exactly one set
of reports reaches the log: each attempt runs with ``log=False`` and only the final one re-emits,
while ``call_and_report`` still fires the ``makereport`` hook first, so every attempt is stamped
with Foundry's ``user_properties``. No delay is inserted between attempts -- backoff belongs to
:mod:`foundry.http.retries` -- so this module never sleeps, starts a thread or opens a socket, and
it catches neither ``KeyboardInterrupt`` nor pytest's ``Exit`` (AAP 0.4.7, AAP 0.14.3). A plumbing
fault is one ``WARNING`` that changes no outcome, and importing the module binds names only.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeVar

from foundry.core.ids import utc_now
from foundry.core.types import PHASES, AttemptRecord, Phase
from foundry.execution import quarantine, retry, timeout
from foundry.execution.session import SessionState, session_state
from foundry.execution.state import (
    ITEM_ARTIFACTS,
    ITEM_ATTEMPT_STARTED_MONOTONIC,
    ITEM_ATTEMPTS,
    ITEM_LOG_RING,
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

    from foundry.obs.artifacts import ArtifactRef, ArtifactWriter

__all__: Final[tuple[str, ...]] = (
    "AttemptLedger",
    "EMPTY_LEDGER",
    "capture_artifacts",
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

_CAPTURE_MESSAGE: Final[str] = (
    "per-test artifact capture faulted, so this test's artifacts are incomplete"
)
"""Logged when the item's writer or log ring could not be bound or written (AAP 0.1.2)."""

_MESSAGE_FAULT: Final[str] = (
    "the retry driver could not redact a failure message, so this attempt carries none"
)
"""Logged when scrubbing a failure message raised; the attempt is still recorded (AAP 0.1.2)."""

_TRACEBACK_MESSAGE: Final[str] = (
    "the failing test's traceback could not be rendered, so this test has no traceback.txt"
)
"""Logged when a failing report's long representation raised; the record keeps its outcome."""

_KeyT = TypeVar("_KeyT")


@dataclass(frozen=True, slots=True)
class AttemptLedger:
    """One item's attempts, oldest first: the only place attempt history lives (AAP 0.4.8).

    Frozen because the report hook, the ``user_properties`` mirror and the summed
    ``duration_ms`` all read one value none of them may alter, and :meth:`add` returns a new
    ledger; the stash keeps the plain ``list`` that crosses the worker boundary.
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

    ``item.stash[ITEM_ATTEMPTS]`` is the single home of attempt history (AAP 0.4.8) and holds
    only finished attempts, so ``plugin.py`` derives the current number from ``ledger.attempts``.
    """
    records = item.stash.get(ITEM_ATTEMPTS, None)
    return EMPTY_LEDGER if records is None else AttemptLedger.of(records)


def reset_item_state(item: pytest.Item) -> None:
    """Clear one item's per-attempt stash entries before its first attempt.

    Defensive rather than routine: a reused ``Item`` would otherwise inherit another test's attempt
    history, quarantine verdict and start times. Only the five keys this module owns are cleared.
    """
    _forget(item, ITEM_ATTEMPTS)
    _forget(item, ITEM_QUARANTINE)
    _forget(item, ITEM_STARTED_AT)
    _forget(item, ITEM_STARTED_MONOTONIC)
    _forget(item, ITEM_ATTEMPT_STARTED_MONOTONIC)


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
    a failing call is ``failed``; ``wasxfail`` splits xfail or xpass from skip or pass; an
    attempt with no report is ``not_run``; :func:`foundry.execution.session.assemble_payload`
    applies the same precedence.
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
    line = retry.first_line(retry.failure_text(report.longrepr))
    if not line:
        return None
    return redactor.text(line, limit=MAX_EXCERPT_BYTES)


def _failure_message(reports: Sequence[pytest.TestReport], redactor: Redactor) -> str | None:
    """Return the first failure's redacted message, isolating a redaction fault to one warning.

    :func:`first_failure_message` scrubs before the excerpt is kept, so a scrubber that raises
    would otherwise end the run rather than cost one attempt its label. The message is dropped
    rather than kept unscrubbed: a redaction fault must never become the leak the scrubber
    prevents (AAP 0.14.3).
    """
    try:
        return first_failure_message(reports, redactor)
    except Exception as error:
        # Failure-isolation rule: a redaction fault may not change an outcome or an exit code.
        events.emit_plumbing_failure(_COMPONENT, _MESSAGE_FAULT, exc=error)
        return None


def run_attempts(item: pytest.Item, nextitem: pytest.Item | None) -> bool:
    """Run ``item`` up to ``execution.retries`` times; the ``firstresult`` hook's whole body.

    What ``plugin.py``'s ``pytest_runtest_protocol`` delegation calls: pytest's own protocol runs
    once per attempt, every attempt lands in the item's ledger, the quarantine verdict comes from
    the final attempt's outcome, and one set of reports -- the final one -- is emitted. A plumbing
    fault costs one ``WARNING`` (AAP 0.1.2), and ``True`` stops the ``firstresult`` chain.
    """
    state = _read_state(item)
    if state is None:
        return _plain_pass(item, nextitem)
    _prepare_item(item)
    _start_capture(item, state)
    ihook = item.ihook
    ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    try:
        reports = _attempt_loop(item, nextitem, state)
        if not reports:
            # No attempt survived Foundry's own plumbing, so the item runs once more, unretried: the
            # outcome the test earns is then the outcome the run reports, unchanged (AAP 0.1.2).
            reports = _plain_run(item, nextitem)
    finally:
        _stop_capture(item)
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

    A fault raised out of the protocol is reported once and ends the loop with no reports, which
    is the caller's cue to run the item once, unretried (AAP 0.1.2). pytest's control flow is
    not a fault and is re-raised: ``Exit`` must reach pytest (AAP 0.4.7), and
    ``KeyboardInterrupt`` is a ``BaseException``.
    """
    # Imported here rather than at module scope: the runner is what the hook delegation enters
    # anyway, and AAP 0.4.8 keeps pytest off this module's runtime import surface.
    from _pytest.outcomes import Exit
    from _pytest.runner import runtestprotocol

    policy = state.retries
    reports: list[pytest.TestReport] = []
    for number in range(1, policy.max_attempts + 1):
        started = time.monotonic()
        _stamp_attempt_start(item, started)
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
                message=_failure_message(reports, state.redactor),
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


def _stamp_attempt_start(item: pytest.Item, started: float) -> None:
    """Record when the attempt in flight began, isolating a stash fault to one ``WARNING``.

    ``plugin.py`` measures the in-flight attempt's elapsed span from this stamp, so every attempt
    record covers only its own run and the sum ``assemble_payload`` takes is the item's wall clock
    across all attempts (AAP 0.4.6). A stash fault costs the timestamp and never the outcome.
    """
    try:
        item.stash[ITEM_ATTEMPT_STARTED_MONOTONIC] = started
    except Exception as error:
        # Failure-isolation rule: a stash fault costs a timestamp, never the test's outcome.
        events.emit_plumbing_failure(_COMPONENT, _CLOCK_MESSAGE, exc=error)


# --- Per-test artifact capture ---------------------------------------------


def _start_capture(item: pytest.Item, state: SessionState) -> None:
    """Bind this item's artifact writer and its bounded log ring before the first attempt.

    AAP 0.4.6 gives every collected test one directory under ``artifacts/<run_id>/`` and AAP 0.14.2
    bounds its log tail at ``logging.capture_lines``, so both are created here, once per item, and
    published on the typed item stash :func:`capture_artifacts` reads. The ring is attached for the
    whole protocol -- setup, every attempt and teardown -- so the tail answers "what did this test
    log", and the writer becomes the process's active one so the HTTP layer records the exchange
    (AAP 0.4.6, requirement I5). A fault is one ``WARNING`` and leaves the test with no artifacts
    rather than a new outcome, and the writer is bound even when the artifact directory could not
    be created: it shares the run's disabled state, so every write records nothing and a capture
    fault never becomes the test's own failure (AAP 0.4.7, AAP 0.1.2).
    """
    try:
        from foundry.obs.artifacts import set_active_writer
        from foundry.obs.capture import LogRing

        writer = state.writer.for_node(item.nodeid)
        item.stash[ITEM_ARTIFACTS] = writer
        ring = LogRing(capacity=state.capture_lines)
        if ring.capacity > 0:
            ring.attach()
        item.stash[ITEM_LOG_RING] = ring
        set_active_writer(writer)
    except Exception as error:
        # Failure-isolation rule: capture may not change a test's outcome (AAP 0.1.2).
        events.emit_plumbing_failure(_COMPONENT, _CAPTURE_MESSAGE, exc=error)


def _stop_capture(item: pytest.Item) -> None:
    """Detach the log ring and unbind the writer once the item's attempts are over.

    Detaching matters as much as attaching: a ring left on the ``foundry`` logger would collect the
    next test's records into this test's tail, and a writer left bound would let a later request
    land in this test's directory (AAP 0.4.8).
    """
    try:
        from foundry.obs.artifacts import set_active_writer

        set_active_writer(None)
        ring = item.stash.get(ITEM_LOG_RING, None)
        if ring is not None:
            ring.detach()
    except Exception as error:
        # Failure-isolation rule: capture may not change a test's outcome (AAP 0.1.2).
        events.emit_plumbing_failure(_COMPONENT, _CAPTURE_MESSAGE, exc=error)


def capture_artifacts(
    item: pytest.Item,
    reports: Sequence[pytest.TestReport],
    *,
    final: bool,
) -> tuple[tuple[ArtifactRef, ...], tuple[str, ...]]:
    """Write one test's artifacts on its last phase, returning its manifest and its log tail.

    ``plugin.py`` stamps every phase's report and the controller keeps the teardown phase's
    payload, so the files are written there: both captured streams, ``traceback.txt`` from the
    first failing phase, ``log_tail.log`` from the bounded ring, then ``manifest.json`` through
    :meth:`foundry.obs.artifacts.ArtifactWriter.finalize`, which leaves a passing test with only its
    manifest (AAP 0.4.5, AAP 0.4.6). The HTTP layer writes the request and response dumps itself, as
    the test makes its request. Both answers are empty for any other phase and for a test that
    captured nothing.
    """
    ring = item.stash.get(ITEM_LOG_RING, None)
    log_tail = () if ring is None else ring.tail()
    writer = item.stash.get(ITEM_ARTIFACTS, None)
    if writer is None:
        return (), log_tail
    if not final:
        return writer.manifest, log_tail
    try:
        _write_captured(reports, writer, log_tail)
    except Exception as error:
        # Failure-isolation rule: a capture fault may not change an outcome or an exit code.
        events.emit_plumbing_failure(_COMPONENT, _CAPTURE_MESSAGE, exc=error)
    return writer.manifest, log_tail


def _write_captured(
    reports: Sequence[pytest.TestReport], writer: ArtifactWriter, log_tail: Sequence[str]
) -> None:
    """Write one test's captured streams, traceback and log tail, then finalise its manifest.

    ``stdout.log`` and ``stderr.log`` are written for every test whether or not it wrote to that
    stream: AAP 0.4.6 publishes both names in the artifact layout and ``junit.xml`` references them
    as ``foundry.artifact.stdout`` and ``foundry.artifact.stderr``, so a stream the test never wrote
    is a zero-byte file rather than a missing one. A passing test still loses both to the prune its
    manifest triggers (AAP 0.4.5), and ``traceback.txt`` and ``log_tail.log`` stay conditional, an
    empty one claiming content the test never produced (0.4.7).
    """
    writer.write_text("stdout", _captured_stream(reports, "stdout"))
    writer.write_text("stderr", _captured_stream(reports, "stderr"))
    failure = _first_failure(reports)
    if failure is not None:
        rendered = _rendered_traceback(failure)
        if rendered:
            writer.write_text("traceback", rendered)
    writer.write_log_tail(log_tail)
    writer.finalize(passed=final_outcome(reports) == _PASSED)


def _captured_stream(reports: Sequence[pytest.TestReport], stream: str) -> str:
    """Return the text pytest captured on ``stream`` for one item, each section once, in order.

    pytest keeps one section list per item: ``Item.add_report_section`` appends every non-empty
    ``(when, key, content)`` triple as the phases run, and each phase's report re-sends the
    accumulated list under ``Captured <key> <when>`` titles rather than only its own phase's
    section. Only the last report is read, the teardown report every attempt produces already
    holding each phase of each attempt exactly once, in append order (AAP 0.4.6).
    """
    if not reports:
        return ""
    prefix = f"Captured {stream}"
    sections = reports[-1].sections
    return "".join(content for title, content in sections if title.startswith(prefix))


def _rendered_traceback(report: pytest.TestReport) -> str:
    """Return a failing report's whole rendered traceback, or ``""`` when it cannot be rendered.

    A long representation that raises is one ``WARNING`` and no ``traceback.txt``: the file is
    evidence for the result, never a reason to change its outcome or the run's exit code (AAP
    0.1.2).
    """
    longrepr: object = report.longrepr
    if longrepr is None:
        return ""
    try:
        return str(longrepr)
    except Exception as error:
        # Failure-isolation rule: an unrenderable traceback costs the file, never the outcome.
        events.emit_plumbing_failure(_COMPONENT, _TRACEBACK_MESSAGE, exc=error)
        return ""


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

    The fallback for a run whose state could not be read, so the outcome and the exit code reach the
    terminal as they would with Foundry out of the chain.
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


def _failure_label(reports: Sequence[pytest.TestReport]) -> str:
    """Return the diagnostic label describing how an attempt ended."""
    report = _first_failure(reports)
    return retry.label_for_text("" if report is None else retry.failure_text(report.longrepr))
