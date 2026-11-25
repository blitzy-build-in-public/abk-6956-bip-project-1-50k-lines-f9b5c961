"""How many attempts a failing test earns: the ``execution.retries`` semantics, in one place.

``execution.retries`` -- ``0`` by default, accepted range ``0..10`` (AAP 0.4.5) -- counts the
*repeats after the first attempt*, so a resolved value of ``2`` is ``attempts: 3`` in
``results.json`` (assumption A2); :attr:`ExecutionRetries.max_attempts` publishes that arithmetic.

Retries are Foundry's own rather than a peer plugin's (AAP 0.5.1), because a repeat has to stay
inside Foundry's own runtest protocol: it feeds the attempt ledger, decides the quarantine verdict
and captures the failed attempt's artifacts. Only a ``"call"``-phase ``"failed"`` outcome is
retried, since a fixture fault is deterministic and a repeat would only multiply it, and no delay is
inserted between attempts, so this module imports neither ``time`` nor ``datetime``. The label a
failure earns is diagnostic and never a gate: :func:`retry_label` delegates to
:func:`foundry.http.retries.classify`, and what gates a retry is the phase, the outcome and the
budget.
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
"""The accepted range as every message states it: ``0..10``."""

RETRYABLE_PHASES: Final[tuple[Phase, ...]] = ("call",)
"""The only phase whose failure is retried: the test body, never ``setup`` and never ``teardown``.

A fixture or finaliser fault is deterministic, so a repeat only multiplies it; pytest already
reports a ``setup`` or ``teardown`` failure as an ``error``, which :func:`should_retry` refuses.
"""

LABEL_ASSERTION: Final[str] = "assertion"
"""``AttemptRecord.retry_on`` for an attempt that failed an assertion, Foundry's own label.

Not a retry predicate: a test-level retry is not gated on a predicate at all.
"""

LABEL_ERROR: Final[str] = "error"
"""``AttemptRecord.retry_on`` for an attempt that raised anything else, Foundry's own label."""

_RETRYABLE_OUTCOME: Final[str] = "failed"
"""The one :data:`foundry.execution.state.OutcomeValue` that is retried: a call-phase failure."""

_NO_ATTEMPTS: Final[str] = "no attempts"
"""What :func:`describe_attempts` returns for an empty attempt ledger."""

_MAX_OUTCOME_TOKEN: Final[int] = 32
"""Longest outcome token either message helper echoes, keeping a summary on one bounded line.

The outcome arrives from a payload that crossed the worker boundary, so its length is not
guaranteed by anything this module can see.
"""

_TRUNCATION_MARKER: Final[str] = "..."
"""What replaces the tail of an outcome token longer than :data:`_MAX_OUTCOME_TOKEN`."""


# --- The resolved budget ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExecutionRetries:
    """The resolved ``execution.retries`` value: how many repeats a failing test earns.

    Frozen because the coordinator builds one from session configuration and every item shares it.
    ``repeats`` counts repeats, not attempts (assumption A2): :attr:`max_attempts` is the count the
    ledger and ``results.json`` use, so ``--foundry-retries 2`` is ``attempts: 3``.
    """

    repeats: int = EXECUTION_RETRIES_DEFAULT

    def __post_init__(self) -> None:
        """Validate ``repeats`` against AAP 0.4.5, so no instance holds a value outside ``0..10``.

        Validation lives here rather than only in :meth:`from_config` because a policy can also be
        built by name, and a budget the retry loop reads unvalidated must not vary with the
        construction path. ``bool`` is refused even though it subclasses ``int``.

        Raises:
            ConfigError: When ``repeats`` is not an ``int`` inside ``MIN_RETRIES..MAX_RETRIES``.
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
        configuration, so the key and its range are named in one place.

        Args:
            repeats: The resolved ``execution.retries`` value: repeats after the first attempt,
                ``0..10`` (AAP 0.4.5). It is never coerced, so a float, a string or a ``bool`` is a
                fault rather than a budget.

        Returns:
            The validated budget, whose :attr:`max_attempts` is ``repeats + 1``.

        Raises:
            ConfigError: When ``repeats`` is not an ``int`` inside ``0..10``.
        """
        return cls(repeats=repeats)

    @property
    def max_attempts(self) -> int:
        """Return the total number of attempts, the first one included: ``repeats + 1``.

        Assumption A2 states the arithmetic: ``--foundry-retries 2`` is three attempts and
        ``--foundry-retries 0`` is one, so no value means "run nothing".

        Returns:
            ``repeats + 1``, in ``1..11`` for the accepted range.
        """
        return self.repeats + 1

    @property
    def disabled(self) -> bool:
        """Return whether no repeat is allowed, which is the ``repeats == 0`` default."""
        return self.repeats == 0

    def remaining(self, attempts_used: int) -> int:
        """Return how many attempts are left after ``attempts_used``, never a negative count.

        Args:
            attempts_used: Attempts made so far, the first one included, so ``1`` right after the
                first attempt.

        Returns:
            ``max_attempts - attempts_used`` when that is positive, and ``0`` once it is spent.
        """
        return max(0, self.max_attempts - attempts_used)

    def as_dict(self) -> dict[str, object]:
        """Return the budget as a JSON-serialisable mapping, for diagnostics and doctor output.

        Returns:
            The repeats, the derived attempt count and the disabled flag, in declaration order.
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

    The single retry decision in Foundry (AAP 0.7.1). The three conditions are checked in the order
    an operator would read them: the phase, because a fixture fault is not a candidate at all; the
    outcome, because a passing, skipped, xfailed or errored attempt is not a failure to repeat; and
    the budget last, because it is the only one that changes between attempts.

    Args:
        phase: The pytest phase the attempt's failure was attributed to.
        outcome: How the attempt ended, one of the plain strings of
            :data:`foundry.execution.state.OutcomeValue`. Only ``"failed"`` is retryable;
            ``"error"`` is a setup or teardown failure.
        attempts_used: Attempts made so far, the first one included, so ``1`` after the first.
        policy: The resolved retry budget.

    Returns:
        ``True`` only when ``phase`` is one of :data:`RETRYABLE_PHASES`, ``outcome`` is ``"failed"``
        and ``attempts_used`` is still below :attr:`ExecutionRetries.max_attempts``.
    """
    if phase not in RETRYABLE_PHASES:
        return False
    if outcome != _RETRYABLE_OUTCOME:
        return False
    return attempts_used < policy.max_attempts


def retry_label(excinfo: BaseException | None, *, status_code: int | None = None) -> str | None:
    """Return how an attempt ended, as the label ``AttemptRecord.retry_on`` carries.

    The label names the ending in the vocabulary a reader already knows: the four retry predicates
    of :data:`foundry.http.retries.RETRY_PREDICATES` where the HTTP engine's classification applies,
    and Foundry's :data:`LABEL_ASSERTION` or :data:`LABEL_ERROR` for a test-level failure, which the
    predicate vocabulary deliberately does not cover. Classification is delegated to
    :func:`foundry.http.retries.classify` rather than repeated here, so a label can never disagree
    with the HTTP engine about what happened. The label is diagnostic only: nothing in Foundry's
    retry decision reads it, and a label this module does not recognise never changes an outcome.
    Only an :class:`AssertionError` earns :data:`LABEL_ASSERTION`, so a ``pytest.fail`` call is
    labelled :data:`LABEL_ERROR`.

    Args:
        excinfo: The exception the attempt raised, or ``None`` when it raised nothing.
        status_code: A status the attempt returned, when the caller has one to attribute. Ignored
            when ``excinfo`` is given, because the status of a response that also faulted says
            nothing about the fault.

    Returns:
        ``"timeout"``, ``"connect"``, ``"status_5xx"`` or ``"status_429"`` where the shared
        classification names a predicate; :data:`LABEL_ASSERTION` for an :class:`AssertionError`;
        :data:`LABEL_ERROR` for any other exception; and ``None`` when there is nothing to classify.
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
    """Return ``text`` with every run of whitespace collapsed to one space, and trimmed."""
    return " ".join(text.split())


def _outcome_token(outcome: str) -> str:
    """Return ``outcome`` as one single-line token of at most :data:`_MAX_OUTCOME_TOKEN` chars."""
    collapsed = _one_line(outcome)
    if len(collapsed) <= _MAX_OUTCOME_TOKEN:
        return collapsed
    keep = _MAX_OUTCOME_TOKEN - len(_TRUNCATION_MARKER)
    return collapsed[:keep] + _TRUNCATION_MARKER


def describe_attempts(payloads: Sequence[AttemptPayload]) -> str:
    """Return a one-line summary of how each attempt in ``payloads`` ended.

    The summary names the attempt count and each attempt's outcome in the order the attempts
    happened, and nothing else: no duration, no message, no request URL, header or body. That is
    what makes it safe inside a failure message without passing it through the redaction engine
    first (AAP 0.3.2). It is deliberately chronological rather than sorted, because sorting would
    lose which attempt finally passed.

    Args:
        payloads: The attempts made, oldest first, as the coordinator recorded them.

    Returns:
        ``"3 attempts: failed, failed, passed"`` for the criterion-6 shape,
        ``"1 attempt: failed"`` for a single attempt, and ``"no attempts"`` for an empty sequence.
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
    a second copy would only bury it.

    Args:
        nodeid: The test's pytest node id, unsanitised. It is the identity of the test, so only
            whitespace is collapsed to keep one line.
        payloads: The attempts made, oldest first.

    Returns:
        ``"tests/test_cart.py::test_total: retries exhausted after 3 attempts: failed, failed,
        failed"`` for a spent budget, and a message naming the absent attempt for an empty ledger.
    """
    subject = _one_line(nodeid)
    if not payloads:
        return f"{subject}: retries exhausted with no attempt recorded"
    return f"{subject}: retries exhausted after {describe_attempts(payloads)}"
