"""How many attempts a failing test earns: the ``execution.retries`` semantics, in one place.

``execution.retries`` -- ``0`` by default, accepted range ``0..10`` (AAP 0.4.5) -- counts the
*repeats after the first attempt*, so a resolved value of ``2`` is three attempts in total and
``attempts: 3`` in ``results.json`` (assumption A2, AAP 0.4.6). That arithmetic is published here
and nowhere else: :attr:`ExecutionRetries.max_attempts` is ``repeats + 1``, and the worked example
``--foundry-retries 2`` becoming ``attempts: 3`` is the shape the retry criterion observes.

Retries are Foundry's own rather than a peer plugin's (AAP 0.5.1)
    ``pytest-rerunfailures`` is deliberately excluded from the dependency set, because a repeat has
    to stay inside Foundry's own runtest protocol: it feeds the attempt ledger that reaches the
    controller, it decides the quarantine verdict and it captures the artifacts of the attempt that
    failed. A plugin re-running an item behind Foundry's back can offer none of the three.
    :func:`should_retry` is the whole decision, and :mod:`foundry.execution.coordinator` drives
    pytest's runtest protocol with it.

Only a failing call phase is retried
    :data:`RETRYABLE_PHASES` holds ``"call"`` alone. A ``setup`` or ``teardown`` failure is a
    fixture or finaliser fault, and a fixture is deterministic: repeating one multiplies the same
    fault, pays its setup cost again and buries the first traceback under an identical second one.
    pytest's own vocabulary agrees -- it reports a setup or teardown failure as an ``error`` rather
    than a ``failure`` -- which is why :func:`should_retry` accepts ``"failed"`` and nothing else.

Nothing sleeps here, and no clock is read
    AAP 0.4.5 publishes no ``execution.backoff`` key, and adding one would change the published
    configuration surface (AAP 0.4.9 forbids that), so there is no delay between test attempts:
    Foundry repeats the item immediately. Backoff belongs to :mod:`foundry.http.retries`, where
    ``http.retries.backoff_base`` and ``http.retries.backoff_max`` publish it and an injected
    :data:`foundry.core.types.Sleep` keeps every wait out of the suite's wall clock (AAP 0.14.3).
    This module therefore imports neither ``time`` nor ``datetime``: it decides, and the caller
    keeps the time.

One predicate vocabulary, one classification
    :mod:`foundry.http.retries` owns the retry predicates -- ``timeout``, ``connect``,
    ``status_5xx``, ``status_429`` -- and the classification of a fault or a status, and its own
    declaration names this module as that evaluation's consumer, because ``execution`` may import
    ``http`` and never the reverse (AAP 0.4.8). :func:`retry_label` delegates to
    :func:`foundry.http.retries.classify` rather than testing ``httpx`` exception types itself, so a
    label attached to an attempt means exactly what the HTTP engine means by it and this module
    imports no ``httpx`` at all.

    The label is diagnostic and never a gate. AAP 0.4.5 publishes no ``execution.retry_on`` key, so
    the only way to gate a test-level retry on the predicate vocabulary would be to widen it to
    cover an ordinary assertion failure -- and a suite whose assertions were the one thing not
    retried would be the opposite of what the key is for. What gates the decision is the phase, the
    outcome and the budget, in :func:`should_retry`; the label only tells how an attempt ended.

Layering and import discipline
    The imports below are :mod:`foundry.core.exceptions`, :mod:`foundry.core.limits`,
    :mod:`foundry.core.types`, :mod:`foundry.execution.state` and :mod:`foundry.http.retries`, all
    inside the AAP 0.4.8 allowance for this package; ``reporting``, ``cli`` and ``plugin`` are not
    reachable from here. Importing this module defines names only: no I/O, no environment read, no
    logger configuration and no module-level mutable state. The coordinator imports it lazily rather
    than ``plugin.py`` importing it at module scope, so the 150 ms startup budget (AAP 0.14.2) does
    not pay for it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from foundry.core.exceptions import ConfigError
from foundry.core.limits import (
    EXECUTION_RETRIES_DEFAULT,
    EXECUTION_RETRIES_MAX,
    EXECUTION_RETRIES_MIN,
    EXECUTION_RETRIES_RANGE,
    range_fault_message,
)
from foundry.core.types import Phase
from foundry.execution.state import AttemptPayload
from foundry.http.retries import classify

__all__: Final[tuple[str, ...]] = (
    "ExecutionRetries",
    "LABEL_ASSERTION",
    "LABEL_ERROR",
    "MAX_RETRIES",
    "MIN_RETRIES",
    "RETRIES_KEY",
    "RETRIES_RANGE_TEXT",
    "RETRYABLE_PHASES",
    "describe_attempts",
    "exhausted_message",
    "retry_label",
    "should_retry",
)

# --- The key, its bounds and the vocabulary of a label ---------------------

RETRIES_KEY: Final[str] = "execution.retries"
"""The dotted configuration key this module implements, and the key a fault names (AAP 0.4.5)."""

MIN_RETRIES: Final[int] = EXECUTION_RETRIES_MIN
"""Smallest accepted ``execution.retries``: the test runs once and is not repeated."""

MAX_RETRIES: Final[int] = EXECUTION_RETRIES_MAX
"""Largest accepted ``execution.retries``: ten repeats after the first attempt, eleven attempts."""

RETRIES_RANGE_TEXT: Final[str] = EXECUTION_RETRIES_RANGE
"""The accepted range as every message states it: ``0..10``.

Both numbers are defined once in :mod:`foundry.core.limits`, which ``foundry.execution`` may import
(AAP 0.4.8) and whose constants are usable as bounds; this module re-publishes them, and the range
text with them, under the names its callers use. ``tests/unit/test_config_constants.py`` pins all
three against the constraint text the key registry publishes for ``execution.retries``, so a copy
cannot drift from the published surface AAP 0.4.9 fixes.
"""

RETRYABLE_PHASES: Final[tuple[Phase, ...]] = ("call",)
"""The only phase whose failure is retried: the test body, never ``setup`` and never ``teardown``.

A fixture or finaliser fault is deterministic, so a repeat cannot turn it green -- it only
multiplies the fault, pays the fixture's setup again and hides the first traceback behind an
identical copy. pytest already distinguishes the two cases by reporting a ``setup`` or ``teardown``
failure as an ``error``, which is why :func:`should_retry` requires the ``"failed"`` outcome too.
"""

LABEL_ASSERTION: Final[str] = "assertion"
"""``AttemptRecord.retry_on`` for an attempt that failed an assertion, Foundry's own label.

Not a retry predicate: :data:`foundry.http.retries.RETRY_PREDICATES` is the closed vocabulary the
configuration key accepts (AAP 0.4.9), and a test-level retry is not gated on a predicate at all.
"""

LABEL_ERROR: Final[str] = "error"
"""``AttemptRecord.retry_on`` for an attempt that raised anything else, Foundry's own label.

Also not a retry predicate, for the reason :data:`LABEL_ASSERTION` gives: the label describes how
the attempt ended, and the decision is :func:`should_retry`'s.
"""

_RETRYABLE_OUTCOME: Final[str] = "failed"
"""The one :data:`foundry.execution.state.OutcomeValue` that is retried: a call-phase failure.

An ``"error"`` outcome is a setup or teardown failure and is never retried, and every other outcome
of the nine is either not a failure or not the test's own.
"""

_NO_ATTEMPTS: Final[str] = "no attempts"
"""What :func:`describe_attempts` returns for an empty attempt ledger."""

_MAX_OUTCOME_TOKEN: Final[int] = 32
"""Longest outcome token either message helper echoes, keeping a summary on one bounded line.

An attempt's outcome arrives from a payload that crossed the worker boundary, so its length is not
guaranteed by anything this module can see; the cap keeps a summary of ten attempts inside 200
characters and a single row of a report readable.
"""

_TRUNCATION_MARKER: Final[str] = "..."
"""What replaces the tail of an outcome token longer than :data:`_MAX_OUTCOME_TOKEN`."""


# --- The resolved budget ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionRetries:
    """The resolved ``execution.retries`` value: how many repeats a failing test earns.

    Frozen because the coordinator builds one from session configuration and every item shares it,
    and slotted because it holds one field and carries no state a later reader would have to account
    for. The value counts *repeats*, not attempts (assumption A2): :attr:`max_attempts` is the count
    the ledger and ``results.json`` use, so ``--foundry-retries 2`` and ``attempts: 3`` describe the
    same run.

    Attributes:
        repeats: Further attempts after the first, ``0..10`` (AAP 0.4.5). ``0``, the default, means
            the test runs once and a failure is final.
    """

    repeats: int = EXECUTION_RETRIES_DEFAULT

    def __post_init__(self) -> None:
        """Validate ``repeats`` against AAP 0.4.5, so no instance holds a value outside ``0..10``.

        Validation lives here rather than only in :meth:`from_config` because a policy can also be
        built by name -- a fixture override, a test, a future caller -- and a budget the retry loop
        reads unvalidated is the one thing that must not vary with the construction path. ``bool``
        is refused even though it subclasses ``int``: ``retries=True`` is a mistake, not one
        repeat.

        Raises:
            ConfigError: When ``repeats`` is not an ``int`` inside ``MIN_RETRIES..MAX_RETRIES``. The
                message is :func:`foundry.core.limits.range_fault_message`'s one wording, naming
                ``execution.retries``, the offending value and ``0..10``.
        """
        if (
            isinstance(self.repeats, bool)
            or not isinstance(self.repeats, int)
            or not MIN_RETRIES <= self.repeats <= MAX_RETRIES
        ):
            raise ConfigError(
                range_fault_message(RETRIES_KEY, self.repeats, RETRIES_RANGE_TEXT),
                key=RETRIES_KEY,
                hint=(
                    f"set {RETRIES_KEY} to the number of repeats after the first attempt, "
                    f"{MIN_RETRIES}..{MAX_RETRIES}"
                ),
            )

    @classmethod
    def from_config(cls, repeats: int) -> ExecutionRetries:
        """Return the budget for a resolved ``execution.retries`` value.

        The named seam the coordinator calls with the setting it read from the resolved
        configuration, so the key and its range are named in one place rather than at the call site.

        Args:
            repeats: The resolved ``execution.retries`` value: repeats after the first attempt,
                ``0..10`` (AAP 0.4.5). It is never coerced, so a float, a string or a ``bool`` is a
                fault rather than a budget.

        Returns:
            The validated budget, whose :attr:`max_attempts` is ``repeats + 1``.

        Raises:
            ConfigError: When ``repeats`` is not an ``int`` inside ``0..10``, naming
                ``execution.retries`` and the accepted range.
        """
        return cls(repeats=repeats)

    @property
    def max_attempts(self) -> int:
        """Return the total number of attempts, the first one included: ``repeats + 1``.

        Assumption A2 states the arithmetic the published surface depends on: ``execution.retries``
        counts repeats, so ``--foundry-retries 2`` is three attempts and every record of that test
        carries ``attempts: 3``. ``--foundry-retries 0`` is one attempt; no value means "run
        nothing".

        Returns:
            ``repeats + 1``, in ``1..11`` for the accepted range.
        """
        return self.repeats + 1

    @property
    def disabled(self) -> bool:
        """Return whether no repeat is allowed, which is the ``repeats == 0`` default.

        Returns:
            ``True`` when a failing test runs exactly once, the configured default.
        """
        return self.repeats == 0

    def remaining(self, attempts_used: int) -> int:
        """Return how many attempts are left after ``attempts_used``, never a negative count.

        Args:
            attempts_used: Attempts made so far, the first one included -- the convention
                ``AttemptRecord.number`` and :attr:`max_attempts` both use, so ``1`` right after the
                first attempt.

        Returns:
            ``max_attempts - attempts_used`` when that is positive, and ``0`` once the budget is
            spent or overrun. Clamping keeps an exhausted budget readable as ``0`` rather than as a
            negative number no caller would expect.
        """
        return max(0, self.max_attempts - attempts_used)

    def as_dict(self) -> dict[str, object]:
        """Return the budget as a JSON-serialisable mapping, for diagnostics and doctor output.

        Returns:
            The repeats, the derived attempt count and the disabled flag, every value a JSON scalar,
            in declaration order.
        """
        return {
            "repeats": self.repeats,
            "max_attempts": self.max_attempts,
            "disabled": self.disabled,
        }


# --- The decision ----------------------------------------------------------


def should_retry(
    *, phase: Phase, outcome: str, attempts_used: int, policy: ExecutionRetries
) -> bool:
    """Return whether a failed attempt at ``phase`` earns another one under ``policy``.

    The single retry decision in Foundry (AAP 0.7.1). The coordinator calls it after each attempt
    and repeats pytest's runtest protocol while the answer is ``True``, so a budget is spent exactly
    once per failing call phase and never on a fixture fault.

    The three conditions are checked in the order an operator would read them: the phase, because a
    fixture fault is not a candidate at all; the outcome, because a passing, skipped, xfailed or
    errored attempt is not a failure to repeat; and the budget last, because it is the only one that
    changes between attempts.

    Args:
        phase: The pytest phase the attempt's failure was attributed to.
        outcome: How the attempt ended, one of the plain strings of
            :data:`foundry.execution.state.OutcomeValue`. Only ``"failed"`` is retryable;
            ``"error"`` is a setup or teardown failure, and ``"passed"``, ``"skipped"``,
            ``"xfailed"``, ``"xpassed"``, ``"quarantined_failed"``, ``"quarantine_passed"`` and
            ``"not_run"`` are not a call-phase failure at all.
        attempts_used: Attempts made so far, the first one included, so ``1`` after the first.
        policy: The resolved retry budget.

    Returns:
        ``True`` only when ``phase`` is one of :data:`RETRYABLE_PHASES`, ``outcome`` is ``"failed"``
        and ``attempts_used`` is still below :attr:`ExecutionRetries.max_attempts`; ``False``
        otherwise, for every phase, outcome and budget the configuration can produce.
    """
    if phase not in RETRYABLE_PHASES:
        return False
    if outcome != _RETRYABLE_OUTCOME:
        return False
    return attempts_used < policy.max_attempts


def retry_label(excinfo: BaseException | None, *, status_code: int | None = None) -> str | None:
    """Return how an attempt ended, as the label ``AttemptRecord.retry_on`` carries.

    The label rides ``AttemptPayload.retry_on`` and the ``AttemptRecord`` a ``RetryExhaustedError``
    lists, so it names the ending in the vocabulary a reader already knows: the four retry
    predicates of :data:`foundry.http.retries.RETRY_PREDICATES` where the HTTP engine's
    classification applies, and Foundry's :data:`LABEL_ASSERTION` or :data:`LABEL_ERROR` for a
    test-level failure, which the predicate vocabulary deliberately does not cover.

    Classification is delegated to :func:`foundry.http.retries.classify` rather than repeated here,
    so that a timeout, a refused connection, a 5xx and a 429 are recognised by one implementation
    and a label can never disagree with the HTTP engine about what happened. This module imports no
    ``httpx`` type for that reason. The label is diagnostic only: nothing in Foundry's retry
    decision reads it (see :func:`should_retry`), and a label this module does not recognise never
    changes an outcome. Only an :class:`AssertionError` earns :data:`LABEL_ASSERTION`, so a
    ``pytest.fail`` call -- whose exception is a ``BaseException`` but not an ``AssertionError`` --
    is labelled :data:`LABEL_ERROR`; the record's ``phase`` is what says whether the test body or a
    fixture produced the failure, so a wider label set would answer a question the record already
    answers.

    Args:
        excinfo: The exception the attempt raised, or ``None`` when it raised nothing.
        status_code: A status the attempt returned, when the caller has one to attribute. Ignored
            when ``excinfo`` is given, because the status of a response that also faulted says
            nothing about the fault -- the precedence ``classify`` already applies.

    Returns:
        ``"timeout"``, ``"connect"``, ``"status_5xx"`` or ``"status_429"`` where the shared
        classification names a predicate; :data:`LABEL_ASSERTION` for an :class:`AssertionError`;
        :data:`LABEL_ERROR` for any other exception; and ``None`` when there is nothing to classify,
        which is a passing attempt, or a status that names no predicate such as a 200 or a 404.
    """
    if excinfo is not None:
        predicate = classify(excinfo, None)
        if predicate is not None:
            return predicate
        return LABEL_ASSERTION if isinstance(excinfo, AssertionError) else LABEL_ERROR
    if status_code is None:
        return None
    return classify(None, status_code)


# --- The messages two call sites share -------------------------------------


def _one_line(text: str) -> str:
    """Return ``text`` with every run of whitespace collapsed to one space, and trimmed.

    A node id and an attempt outcome both cross the worker boundary as text, so either can arrive
    holding a newline; a summary that is meant to be one line -- a failure message, a JUnit
    ``system-err`` entry, a row of the report -- must not be able to grow a second one.
    """
    return " ".join(text.split())


def _outcome_token(outcome: str) -> str:
    """Return ``outcome`` as one single-line token of at most :data:`_MAX_OUTCOME_TOKEN` characters.

    The cap is what bounds :func:`describe_attempts` whatever a payload carries, and the marker is
    what makes a shortened token visibly shortened rather than a plausible-looking outcome.
    """
    collapsed = _one_line(outcome)
    if len(collapsed) <= _MAX_OUTCOME_TOKEN:
        return collapsed
    keep = _MAX_OUTCOME_TOKEN - len(_TRUNCATION_MARKER)
    return collapsed[:keep] + _TRUNCATION_MARKER


def describe_attempts(payloads: Sequence[AttemptPayload]) -> str:
    """Return a one-line summary of how each attempt in ``payloads`` ended.

    The summary names the attempt count and each attempt's outcome in the order the attempts
    happened, and nothing else: no duration, no message, no request URL, header or body. That is
    what makes it safe inside a failure message, which a terminal, a CI log, ``results.json`` and
    ``report.html`` all carry, without passing it through the redaction engine first (AAP 0.3.2).
    It is deliberately chronological rather than sorted -- an attempt ledger is ordered, and sorting
    it would lose which attempt finally passed.

    Args:
        payloads: The attempts made, oldest first, as the coordinator recorded them.

    Returns:
        ``"3 attempts: failed, failed, passed"`` for the criterion-6 shape,
        ``"1 attempt: failed"`` for a single attempt, and ``"no attempts"`` for an empty sequence.
        Never holds a newline, and never exceeds one line of bounded tokens.
    """
    if not payloads:
        return _NO_ATTEMPTS
    outcomes = ", ".join(_outcome_token(payload.outcome) for payload in payloads)
    noun = "attempt" if len(payloads) == 1 else "attempts"
    return f"{len(payloads)} {noun}: {outcomes}"


def exhausted_message(nodeid: str, payloads: Sequence[AttemptPayload]) -> str:
    """Return the message reported when a test's retry budget is spent.

    The text names the test and the attempt ledger and nothing else: no hint, no path, no duration
    and no exception text, because the failing attempt's own traceback is already in the record and
    a second copy in Foundry's wording would only bury it.

    Args:
        nodeid: The test's pytest node id, unsanitised. It is the identity of the test, so it is
            kept whole rather than shortened; only whitespace is collapsed to keep one line.
        payloads: The attempts made, oldest first.

    Returns:
        ``"tests/test_cart.py::test_total: retries exhausted after 3 attempts: failed, failed,
        failed"`` for a spent budget, and a message naming the absence of an attempt for an empty
        ledger, which a caller reaching here has a fault to report rather than a budget to blame.
    """
    subject = _one_line(nodeid)
    if not payloads:
        return f"{subject}: retries exhausted with no attempt recorded"
    return f"{subject}: retries exhausted after {describe_attempts(payloads)}"
