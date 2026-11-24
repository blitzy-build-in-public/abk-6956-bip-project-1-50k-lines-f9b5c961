"""The retry engine of ``foundry.http``: the policy, the predicates, the backoff and the loop.

``RetryRunner`` puts one logical request on the wire up to ``max_attempts`` times; the predicates,
the backoff formula and the exhausted-attempt records are AAP 0.7.3's.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

import httpx

from foundry.core.exceptions import ConfigError, RetryExhaustedError, TransportError
from foundry.core.limits import (
    BACKOFF_BASE_DEFAULT,
    BACKOFF_BASE_MAX,
    BACKOFF_BASE_MIN,
    BACKOFF_BASE_RANGE,
    BACKOFF_MAX_DEFAULT,
    BACKOFF_MAX_MAX,
    BACKOFF_MAX_MIN,
    BACKOFF_MAX_RANGE,
    MAX_ATTEMPTS_DEFAULT,
    MAX_ATTEMPTS_MAX,
    MAX_ATTEMPTS_MIN,
    MAX_ATTEMPTS_RANGE,
    range_fault_message,
)
from foundry.core.types import AttemptRecord, Sleep
from foundry.obs.redaction import Redactor, default_redactor

__all__ = [
    "DEFAULT_RETRY_ON",
    "MAX_ATTEMPTS",
    "MAX_BACKOFF_BASE",
    "MAX_BACKOFF_MAX",
    "MIN_ATTEMPTS",
    "MIN_BACKOFF_BASE",
    "MIN_BACKOFF_MAX",
    "OUTCOME_ERROR",
    "OUTCOME_RESPONSE",
    "PREDICATE_CONNECT",
    "PREDICATE_STATUS_429",
    "PREDICATE_STATUS_5XX",
    "PREDICATE_TIMEOUT",
    "RETRY_PREDICATES",
    "RetriedResponse",
    "RetryPolicy",
    "RetryRunner",
    "backoff",
    "classify",
    "normalise_predicates",
    "should_retry",
]

# --- Predicate vocabulary --------------------------------------------------

PREDICATE_TIMEOUT: Final[str] = "timeout"
"""Retry predicate for a request that outlived the configured timeout."""

PREDICATE_CONNECT: Final[str] = "connect"
"""Retry predicate for a connection that could not be established."""

PREDICATE_STATUS_5XX: Final[str] = "status_5xx"
"""Retry predicate for a response whose status is 500 or above."""

PREDICATE_STATUS_429: Final[str] = "status_429"
"""Retry predicate for a response whose status is exactly 429."""

RETRY_PREDICATES: Final[tuple[str, ...]] = (
    PREDICATE_TIMEOUT,
    PREDICATE_CONNECT,
    PREDICATE_STATUS_5XX,
    PREDICATE_STATUS_429,
)
"""Every accepted ``http.retries.retry_on`` element, in the order AAP 0.4.5 lists them.

The order is the one the configuration help text and every error message use, so the accepted
set reads the same everywhere. Nothing serialises the tuple, which makes it documentation
rather than output.
"""

DEFAULT_RETRY_ON: Final[tuple[str, ...]] = (
    PREDICATE_TIMEOUT,
    PREDICATE_CONNECT,
    PREDICATE_STATUS_5XX,
)
"""The default ``http.retries.retry_on`` (AAP 0.4.5).

``status_429`` is deliberately absent: a rate-limited peer is asking the client to stop, so a
project that wants Foundry to wait it out names the predicate explicitly.
"""

# The accepted range of each numeric key (AAP 0.4.5), exported because a caller that builds a policy
# by hand, and the diagnostics that render one, both need the bounds. The numbers are defined once,
# in foundry.core.limits: http may not import foundry.config (AAP 0.4.8), and a second copy here is
# what would let a bound drift from the published surface.
MIN_ATTEMPTS: Final[int] = MAX_ATTEMPTS_MIN
MAX_ATTEMPTS: Final[int] = MAX_ATTEMPTS_MAX
MIN_BACKOFF_BASE: Final[float] = BACKOFF_BASE_MIN
MAX_BACKOFF_BASE: Final[float] = BACKOFF_BASE_MAX
MIN_BACKOFF_MAX: Final[float] = BACKOFF_MAX_MIN
MAX_BACKOFF_MAX: Final[float] = BACKOFF_MAX_MAX

OUTCOME_RESPONSE: Final[str] = "response"
"""``AttemptRecord.outcome`` for the attempt whose response is being returned."""

OUTCOME_ERROR: Final[str] = "error"
"""``AttemptRecord.outcome`` for a transport fault the policy does not retry."""

# The dotted keys a range or vocabulary fault names, because the key is what an operator fixes.
_MAX_ATTEMPTS_KEY: Final[str] = "http.retries.max_attempts"
_BACKOFF_BASE_KEY: Final[str] = "http.retries.backoff_base"
_BACKOFF_MAX_KEY: Final[str] = "http.retries.backoff_max"
_RETRY_ON_KEY: Final[str] = "http.retries.retry_on"


# --- Predicate normalisation -----------------------------------------------


def _as_predicate(value: object) -> str:
    """Return ``value`` as a normalised predicate name, or ``""`` when it names nothing.

    Matching on the member's ``value`` lets a ``StrEnum`` member, a lower-case string and an
    upper-case environment value reach the same set without importing the member type
    (AAP 0.4.8), and nothing is raised here: a candidate carrying no usable text names no
    predicate, which :class:`RetryPolicy` then reports as an unknown element of
    ``http.retries.retry_on``.
    """
    name = getattr(value, "value", value)
    if isinstance(name, str):
        return name.strip().lower()
    if name is None:
        return ""
    try:
        return str(name).strip().lower()
    except (TypeError, ValueError):
        return ""


def normalise_predicates(values: Iterable[str | object]) -> frozenset[str]:
    """Return ``values`` as a frozen set of lower-case predicate names.

    Frozen because a policy holding one has to be hashable, and a set because the configured
    list's order and repeats carry no meaning; a lone ``str`` is one name rather than a sequence
    of characters. The vocabulary is not validated here -- :func:`should_retry` calls this on
    every attempt, so it must be total -- and :class:`RetryPolicy` is where an unknown name
    becomes a ``ConfigError`` (AAP 0.4.5).
    """
    candidates: Iterable[object] = (values,) if isinstance(values, str) else values
    try:
        iterator = iter(candidates)
    except TypeError:
        return frozenset()
    found: set[str] = set()
    for value in iterator:
        name = _as_predicate(value)
        if name:
            found.add(name)
    return frozenset(found)


# --- Classification and backoff --------------------------------------------


def classify(error: BaseException | None, status_code: int | None) -> str | None:
    """Return the retry predicate that describes a fault or a status.

    A timeout is tested before a connection failure because :class:`httpx.ConnectTimeout` is a
    :class:`httpx.TimeoutException` and not a :class:`httpx.ConnectError`; the other order would
    file a slow connection at the wrong predicate and against the wrong element of
    ``http.retries.retry_on``. Every other ``httpx`` transport fault -- a read that failed
    midway, a protocol violation -- classifies as ``None`` and is never retried, because
    retrying a broken protocol only repeats the breakage (AAP 0.7.3). ``error`` wins over
    ``status_code`` when both are given: the status of a response that also faulted says
    nothing about the fault.
    """
    if error is not None:
        if isinstance(error, httpx.TimeoutException):
            return PREDICATE_TIMEOUT
        if isinstance(error, httpx.ConnectError):
            return PREDICATE_CONNECT
        return None
    if status_code is None:
        return None
    if status_code >= 500:
        return PREDICATE_STATUS_5XX
    if status_code == 429:
        return PREDICATE_STATUS_429
    return None


def should_retry(outcome: str | None, predicate_set: Iterable[str]) -> bool:
    """Return whether ``outcome`` is one of the predicates in ``predicate_set``.

    Total by design: ``None``, ``""`` and a name outside :data:`RETRY_PREDICATES` are not
    retried rather than raising, because the decision is taken inside a request loop where an
    exception would replace the answer the caller asked for.
    """
    if outcome is None:
        return False
    name = _as_predicate(outcome)
    if not name:
        return False
    return name in normalise_predicates(predicate_set)


def backoff(attempt: int, *, base: float, maximum: float) -> float:
    """Return the wait that follows attempt ``attempt``.

    The growth is exponential from one base -- ``min(base * 2 ** (attempt - 1), maximum)`` for a
    1-based ``attempt`` (AAP 0.7.3) -- and it is written as a bounded doubling loop rather than a
    power, because ``2.0 ** 2000`` raises ``OverflowError`` while a loop returns the cap as soon
    as it reaches it. A count below 1 is treated as 1, and a non-positive or non-finite base or
    cap yields ``0.0``, so a disabled policy never hands the injected sleep a negative, ``NaN``
    or infinite delay.
    """
    if not math.isfinite(base) or not math.isfinite(maximum):
        return 0.0
    if base <= 0.0 or maximum <= 0.0:
        return 0.0
    delay = base
    for _ in range(attempt - 1 if attempt > 1 else 0):
        delay *= 2.0
        if delay >= maximum:
            return maximum
    return min(delay, maximum)


# --- Policy validation -----------------------------------------------------


def _require_int(value: object, key: str, minimum: int, maximum: int, *, constraint: str) -> int:
    """Return ``value`` as an ``int`` inside ``minimum..maximum``, or raise ``ConfigError``.

    A configuration model rejects an out-of-range value before a policy exists, so reaching this
    check means the policy came from a fixture override or a test; the error still names the
    dotted key, because that is what an operator would fix. ``bool`` is refused even though it
    subclasses ``int``: ``max_attempts=True`` is a mistake, not a one-attempt policy. The message
    is :func:`foundry.core.limits.range_fault_message`'s one wording, carrying ``constraint`` --
    the range as the published registry spells it -- rather than one re-composed from the bounds.

    Raises:
        ConfigError: When the value is not an ``int`` or lies outside the range, carrying the
            key and a message naming the key, the accepted range and the offending value.
    """
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(range_fault_message(key, value, constraint), key=key)
    return value


def _require_float(
    value: object, key: str, minimum: float, maximum: float, *, constraint: str
) -> float:
    """Return ``value`` as a finite ``float`` inside ``minimum..maximum``, or raise.

    ``NaN`` and the infinities are refused along with values outside the range: either would
    travel into :func:`backoff` and then into the injected sleep, and the failure would surface
    far from the configuration key that caused it. The message is
    :func:`foundry.core.limits.range_fault_message`'s one wording, carrying ``constraint`` -- the
    range as the published registry spells it.

    Raises:
        ConfigError: When the value is not a real number, is not finite or lies outside the
            range, carrying the key and a message naming the accepted range and the value.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(range_fault_message(key, value, constraint), key=key)
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ConfigError(range_fault_message(key, value, constraint), key=key)
    return number


# --- The policy ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """The resolved ``http.retries.*`` settings: how often to try, and how long to wait.

    The defaults and the ranges are AAP 0.4.5's. Frozen, because a session builds one policy from
    its resolved configuration and every request shares it; hashable, so a caller can key a cache
    on it; and ``retry_on`` is a ``frozenset``, so the configured list's order and repeats cannot
    change behaviour. ``max_attempts`` is the total count including the first, which is why
    ``--foundry-retries 2`` and ``max_attempts=3`` describe the same three attempts (assumption
    A2).

    Attributes:
        max_attempts: Total attempts per logical request, ``1..10``, default ``3``.
        backoff_base: Wait before the second attempt, ``0..10`` seconds, default ``0.2``.
        backoff_max: Cap on any single wait, ``0..60`` seconds, default ``2.0``.
        retry_on: The predicates that earn another attempt, defaulting to
            :data:`DEFAULT_RETRY_ON`.
    """

    max_attempts: int = MAX_ATTEMPTS_DEFAULT
    backoff_base: float = BACKOFF_BASE_DEFAULT
    backoff_max: float = BACKOFF_MAX_DEFAULT
    retry_on: frozenset[str] = frozenset(DEFAULT_RETRY_ON)

    def __post_init__(self) -> None:
        """Validate every field against AAP 0.4.5 and freeze ``retry_on``.

        ``object.__setattr__`` is how a frozen dataclass normalises its own fields: the numeric
        bounds become ``int`` and ``float`` even where a compatible numeric type was passed, and
        ``retry_on`` becomes a ``frozenset`` of lower-case names, so a policy built from the
        configuration enum's members equals one built from plain strings.

        Raises:
            ConfigError: When a numeric value is outside its range or is not a number, or when
                ``retry_on`` names a predicate outside :data:`RETRY_PREDICATES`. Every message
                names the dotted key it came from.
        """
        attempts = _require_int(
            self.max_attempts,
            _MAX_ATTEMPTS_KEY,
            MIN_ATTEMPTS,
            MAX_ATTEMPTS,
            constraint=MAX_ATTEMPTS_RANGE,
        )
        base = _require_float(
            self.backoff_base,
            _BACKOFF_BASE_KEY,
            MIN_BACKOFF_BASE,
            MAX_BACKOFF_BASE,
            constraint=BACKOFF_BASE_RANGE,
        )
        cap = _require_float(
            self.backoff_max,
            _BACKOFF_MAX_KEY,
            MIN_BACKOFF_MAX,
            MAX_BACKOFF_MAX,
            constraint=BACKOFF_MAX_RANGE,
        )
        object.__setattr__(self, "max_attempts", attempts)
        object.__setattr__(self, "backoff_base", base)
        object.__setattr__(self, "backoff_max", cap)
        names = normalise_predicates(self.retry_on)
        unknown = sorted(names.difference(RETRY_PREDICATES))
        if unknown:
            accepted = ", ".join(RETRY_PREDICATES)
            raise ConfigError(
                f"{_RETRY_ON_KEY} accepts only {accepted}, got {', '.join(unknown)}",
                key=_RETRY_ON_KEY,
            )
        object.__setattr__(self, "retry_on", names)

    def delay_for(self, attempt: int) -> float:
        """Return the wait that follows attempt ``attempt`` under this policy.

        Args:
            attempt: The 1-based number of the attempt that just failed.

        Returns:
            The seconds to wait, from :func:`backoff` with this policy's base and cap.
        """
        return backoff(attempt, base=self.backoff_base, maximum=self.backoff_max)

    def should_retry(self, outcome: str | None) -> bool:
        """Return whether ``outcome`` earns another attempt under this policy.

        Args:
            outcome: The predicate to test, as :func:`classify` returned it.

        Returns:
            ``True`` when ``outcome`` is one of this policy's ``retry_on`` names.
        """
        return should_retry(outcome, self.retry_on)

    def as_dict(self) -> dict[str, object]:
        """Return the policy as a mapping for diagnostics and log lines.

        ``retry_on`` is sorted, so two runs that resolved the same policy print the same line
        whatever order the configured list held. Nothing persists this mapping: ``foundry config
        show`` renders the resolved configuration model instead.

        Returns:
            The four fields, with ``retry_on`` a sorted list of names.
        """
        return {
            "max_attempts": self.max_attempts,
            "backoff_base": self.backoff_base,
            "backoff_max": self.backoff_max,
            "retry_on": sorted(self.retry_on),
        }


# --- The outcome of one logical request ------------------------------------


@dataclass(frozen=True, slots=True)
class RetriedResponse:
    """The response a logical request ended with, and the attempts it took to get there.

    ``attempts`` holds one record per attempt in the order they happened, which is what the
    ``attempts`` count in ``results.json`` and the failure panel in ``report.html`` are built
    from (AAP 0.4.7). Frozen because the record outlives the request: a caller may keep it while
    the next request runs, and reporting must not be able to alter capture.

    Attributes:
        response: The last response, whose status is whatever the peer sent -- including the
            status that was being retried when the attempts ran out.
        attempts: One record per attempt, in order, ending with the one that produced
            ``response``.
        elapsed_ms: Wall-clock milliseconds across every attempt, on the monotonic clock.
    """

    response: httpx.Response
    attempts: tuple[AttemptRecord, ...]
    elapsed_ms: float


def _elapsed_ms(started: float) -> float:
    """Return the milliseconds elapsed since ``started`` on the monotonic clock.

    Returns:
        The milliseconds since ``started``, never negative because the monotonic clock does not
        go backwards.
    """
    return (time.monotonic() - started) * 1000.0


# --- The loop --------------------------------------------------------------


class RetryRunner:
    """Issue one logical request, retrying the faults the policy names.

    One instance serves a whole session: it holds the policy, the injected sleep and the run's
    redactor, and nothing per request, so :meth:`execute` keeps its state on the stack. One
    ``send()`` call happens per attempt and never more, so ``record`` mode writes one cassette
    interaction per attempt and ``replay`` serves them in the order they were recorded, and a
    response is returned even when its status is the one being retried. ``httpx`` exceptions are
    the only ones caught; a :class:`foundry.core.exceptions.FoundryError` from the transport,
    ``CassetteMissError`` above all, propagates untouched (AAP 0.4.7).
    """

    _policy: RetryPolicy
    _redactor: Redactor
    _sleep: Sleep

    __slots__ = ("_policy", "_redactor", "_sleep")

    def __init__(
        self,
        policy: RetryPolicy,
        *,
        sleep: Sleep = time.sleep,
        redactor: Redactor | None = None,
    ) -> None:
        """Store the policy and the collaborators every attempt uses.

        Args:
            policy: The retry settings resolved from ``http.retries.*``.
            sleep: The callable that performs a wait, defaulting to :func:`time.sleep`. A test
                injects a recording fake, so a backoff is asserted rather than paid.
            redactor: The run's redaction engine, or ``None`` to build one carrying only the
                built-in deny-lists, which still redacts ``Authorization``, cookies and the
                usual token query parameters.
        """
        self._policy = policy
        self._sleep = sleep
        self._redactor = redactor if redactor is not None else default_redactor()

    def execute(
        self,
        send: Callable[[], httpx.Response],
        *,
        method: str,
        path: str,
    ) -> RetriedResponse:
        """Issue the request until an attempt produces a response or the policy gives up.

        Args:
            send: A zero-argument callable performing one attempt and returning its response.
                It is called exactly once per attempt, the last one included.
            method: The request method, named in every message this call raises.
            path: The request path, named in every message this call raises.

        Returns:
            The final response with one attempt record per attempt, including the response of a
            retried status when the attempts ran out.

        Raises:
            TransportError: When a transport fault is a kind the policy does not retry, or is one
                it does and no attempt remains; the message is redacted.
        """
        started = time.monotonic()
        records: list[AttemptRecord] = []
        total = self._policy.max_attempts
        for number in range(1, total + 1):
            attempt_started = time.monotonic()
            try:
                response = send()
            except httpx.HTTPError as exc:
                self._retry_or_raise(
                    exc,
                    records,
                    number=number,
                    started=attempt_started,
                    method=method,
                    path=path,
                )
                continue
            predicate = classify(None, response.status_code)
            message = f"status {response.status_code}"
            if predicate is not None and self._policy.should_retry(predicate) and number < total:
                records.append(self._record(number, predicate, attempt_started, predicate, message))
                self._sleep(self._policy.delay_for(number))
                continue
            records.append(self._record(number, OUTCOME_RESPONSE, attempt_started, None, message))
            return RetriedResponse(
                response=response,
                attempts=tuple(records),
                elapsed_ms=_elapsed_ms(started),
            )
        # Unreachable: the final iteration can neither continue nor fall through, because the
        # retry branch requires `number < total`. The statement keeps the function total for the
        # type checker without casting the tuple returned above.
        last = records[-1]
        raise RetryExhaustedError(
            self._message(method, path, last.outcome, last.message),
            method=method,
            path=path,
            cause=last.outcome,
            attempts=records,
        )

    def _retry_or_raise(
        self,
        exc: httpx.HTTPError,
        records: list[AttemptRecord],
        *,
        number: int,
        started: float,
        method: str,
        path: str,
    ) -> None:
        """Record a transport fault, then wait for the next attempt or raise.

        Args:
            exc: The ``httpx`` exception the attempt raised.
            records: The attempt records, appended to in place.
            number: The 1-based number of the attempt that failed.
            started: The monotonic reading taken when that attempt began.
            method: The request method.
            path: The request path.

        Returns:
            ``None`` once the backoff has been waited, which means another attempt is due.

        Raises:
            TransportError: When the fault is not retryable under the policy, or is retryable
                and no attempt remains. ``cause`` is the predicate for a retryable kind and the
                exception's class name otherwise, and the message is redacted.

        Every attempt ended by a retryable fault keeps that predicate in ``retry_on``, the last
        one included: the record says why the attempt failed, and the number of records is the
        number of attempts made (assumption A2).
        """
        predicate = classify(exc, None)
        detail = self._redactor.text(f"{type(exc).__name__}: {exc}")
        if predicate is None:
            # TransportError carries no attempt list, so this record never reaches a report; it is
            # appended to keep both branches shaped alike, and the redacted detail is what a reader
            # of the failure sees.
            records.append(self._record(number, OUTCOME_ERROR, started, None, detail))
            raise TransportError(
                self._message(method, path, type(exc).__name__, detail),
                method=method,
                path=path,
                cause=type(exc).__name__,
            ) from exc
        records.append(self._record(number, predicate, started, predicate, detail))
        if not self._policy.should_retry(predicate):
            raise TransportError(
                self._message(method, path, predicate, detail),
                method=method,
                path=path,
                cause=predicate,
            ) from exc
        if number >= self._policy.max_attempts:
            label = "attempt" if number == 1 else "attempts"
            raise RetryExhaustedError(
                self._message(method, path, f"{predicate} after {number} {label}", detail),
                method=method,
                path=path,
                cause=predicate,
                attempts=records,
            ) from exc
        self._sleep(self._policy.delay_for(number))

    def _record(
        self,
        number: int,
        outcome: str,
        started: float,
        retry_on: str | None,
        message: str | None,
    ) -> AttemptRecord:
        """Return the record of one finished attempt.

        Returns:
            An :class:`foundry.core.types.AttemptRecord` timed from ``started``, with ``retry_on``
            naming the predicate that earns the next attempt.
        """
        return AttemptRecord(
            number=number,
            outcome=outcome,
            elapsed_ms=_elapsed_ms(started),
            retry_on=retry_on,
            message=message,
        )

    def _message(self, method: str, path: str, cause: str, detail: str | None = None) -> str:
        """Return a redacted single-line description of a failed request.

        Returns:
            ``<method> <path>: <cause>``, followed by ``: <detail>`` when a detail was given. The
            whole line is scrubbed, because ``path`` is caller-supplied and a failing URL is
            exactly what a redaction engine exists to catch.
        """
        base = f"{method} {path}: {cause}"
        return self._redactor.text(base if detail is None else f"{base}: {detail}")
